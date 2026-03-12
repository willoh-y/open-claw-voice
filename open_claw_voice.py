#!/usr/bin/env python3
"""
OpenClaw Voice - Voice chat with OpenClaw

Flow:
1. Start in dormant mode (not listening)
2. SIGUSR1 toggles active/dormant state
3. When active: stream mic audio to Deepgram STT
4. On speech end (endpointing), send transcript to OpenClaw
5. Stream OpenClaw response to ElevenLabs TTS
6. Play audio, allow interrupts to go back to listening
"""

import asyncio
import json
import logging
import os
import signal
import sys
from enum import Enum
from pathlib import Path
from typing import Optional

import aiohttp  # type: ignore
import numpy as np  # type: ignore
import websockets  # type: ignore
from dotenv import load_dotenv  # type: ignore

from config import Config, parse_args

# API keys - loaded after environment setup in main()
STT_API_KEY: Optional[str] = None
TTS_API_KEY: Optional[str] = None
OPENCLAW_TOKEN: Optional[str] = None


def is_development_mode() -> bool:
    """Check if running in development mode (local directory)."""
    # Explicit dev mode flag
    if os.getenv("OPEN_CLAW_VOICE_DEV"):
        return True
    # If QML path is set, we're in packaged mode
    if os.getenv("OPEN_CLAW_VOICE_QML_PATH"):
        return False
    # Default to dev mode (running script directly)
    return True


def get_state_file() -> Path:
    """Get state file path (runtime, ephemeral state for UI)."""
    if is_development_mode():
        return Path(__file__).parent / "state.txt"

    # Production: use XDG runtime directory
    runtime_dir = os.getenv("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    state_dir = Path(runtime_dir) / "open-claw-voice"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / "state.txt"


def get_log_file() -> Path:
    """Get log file path (persistent state)."""
    if is_development_mode():
        return Path(__file__).parent / "open_claw_voice.log"

    # Production: use XDG state directory
    state_home = os.getenv("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))
    log_dir = Path(state_home) / "open-claw-voice"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / "open_claw_voice.log"


def get_qml_file() -> Path:
    """Get QML UI file path."""
    # Explicit environment variable (set by Nix wrapper)
    if qml_path := os.getenv("OPEN_CLAW_VOICE_QML_PATH"):
        return Path(qml_path)

    # Development: use script directory
    return Path(__file__).parent / "shell.qml"


def load_environment(env_file: Optional[Path] = None) -> None:
    """
    Load environment variables from .env file.

    Priority order:
    1. Explicit env_file path (if provided via --env-file)
    2. XDG config directory: $XDG_CONFIG_HOME/open-claw-voice/.env
    3. Home config directory: ~/.config/open-claw-voice/.env
    4. Current directory: ./.env (for development)

    If no .env file is found, environment variables must be set externally
    (e.g., via systemd EnvironmentFile or shell exports).
    """
    logger = logging.getLogger(__name__)

    if env_file and env_file.exists():
        load_dotenv(env_file)
        logger.info(f"Loaded environment from: {env_file}")
        return

    # Try XDG config directory
    xdg_config = os.getenv("XDG_CONFIG_HOME", str(Path.home() / ".config"))
    xdg_env = Path(xdg_config) / "open-claw-voice" / ".env"

    # Try standard config directory (in case XDG_CONFIG_HOME is set to something else)
    home_env = Path.home() / ".config" / "open-claw-voice" / ".env"

    # Try script directory (development)
    script_env = Path(__file__).parent / ".env"

    # Try current directory (development fallback)
    local_env = Path.cwd() / ".env"

    for env_path in [xdg_env, home_env, script_env, local_env]:
        if env_path.exists():
            load_dotenv(env_path)
            logger.info(f"Loaded environment from: {env_path}")
            return

    logger.info("No .env file found, expecting environment variables to be set externally")


def setup_logging(use_log_file: bool = True) -> None:
    """Set up logging configuration."""
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]

    if use_log_file:
        log_file = get_log_file()
        handlers.append(logging.FileHandler(log_file))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=handlers,
    )


# State file for Quickshell UI (computed lazily)
STATE_FILE: Path  # Set during startup
LOG_FILE: Path  # Set during startup

# System prompt for voice agent
SYSTEM_PROMPT = """You are a helpful voice assistant. Keep your responses concise and conversational since the user is listening, not reading.

Guidelines:
- Be brief and direct. Aim for 1-3 sentences when possible.
- Avoid lists, bullet points, code blocks, or markdown formatting - speak naturally.
- Don't use special characters or symbols that don't translate well to speech.
- If a longer explanation is needed, break it into digestible chunks and offer to continue.
- Use natural speech patterns and contractions (e.g., "I'll", "you're", "that's").
- Be warm but efficient - respect the user's time.

Here is the users question:
"""


class State(Enum):
    DORMANT = "dormant"  # Service running but not listening
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    ERROR = "error"


# Logger will be configured in main() via setup_logging()
logger = logging.getLogger(__name__)


class OpenClawVoice:
    def __init__(self, config: Config):
        self.config = config
        self.state = State.DORMANT
        self.running = False
        self.active = False  # Whether we're actively listening
        self.current_transcript = ""
        self.tts_task: Optional[asyncio.Task] = None
        self.interrupt_event = asyncio.Event()
        self.speech_detected_event = asyncio.Event()
        self.toggle_event = asyncio.Event()  # Signaled by SIGUSR1
        self.quickshell_proc: Optional[asyncio.subprocess.Process] = None
        self._http_session: Optional[aiohttp.ClientSession] = None

    async def get_http_session(self) -> aiohttp.ClientSession:
        """Get or create the shared HTTP session."""
        if self._http_session is None or self._http_session.closed:
            self._http_session = aiohttp.ClientSession()
        return self._http_session

    async def cleanup(self) -> None:
        """Clean up resources."""
        if self._http_session and not self._http_session.closed:
            await self._http_session.close()

    def toggle_active(self):
        """Toggle between dormant and active states. Called by SIGUSR1 handler."""
        if self.active:
            logger.info("SIGUSR1: Deactivating (entering dormant mode)")
            self.active = False
            self.interrupt_event.set()  # Stop any current activity
        else:
            logger.info("SIGUSR1: Activating (starting to listen)")
            self.active = True
        self.toggle_event.set()

    def apply_noise_gate(self, audio_chunk: bytes) -> bytes:
        """
        Apply noise gate to audio chunk. Returns silence if RMS is below threshold.

        Args:
            audio_chunk: Raw 16-bit PCM audio bytes

        Returns:
            Original chunk if above threshold, silence if below
        """
        if len(audio_chunk) == 0:
            return audio_chunk

        # Convert bytes to numpy array of int16
        audio_array = np.frombuffer(audio_chunk, dtype=np.int16)

        # Calculate RMS (root mean square) amplitude
        rms = np.sqrt(np.mean(audio_array.astype(np.float32) ** 2))

        # If below threshold, return silence
        if rms < self.config.audio.gate_threshold:
            return bytes(len(audio_chunk))  # All zeros = silence

        return audio_chunk

    async def set_state(self, state: State) -> None:
        """Update state and write to file for Quickshell (non-blocking)."""
        self.state = state
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, STATE_FILE.write_text, state.value)
        except Exception as e:
            logger.error(f"Failed to write state file: {e}")
        logger.info(f"State: {state.value}")

    async def stream_microphone(self) -> asyncio.subprocess.Process:
        """Start pw-record subprocess for mic input."""
        proc = await asyncio.create_subprocess_exec(
            "pw-record",
            "--target", self.config.audio.mic_device,
            "--rate", str(self.config.audio.sample_rate),
            "--channels", str(self.config.audio.channels),
            "--format", "s16",
            "-",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        return proc

    async def run_stt(self) -> Optional[str]:
        """
        Stream audio to Deepgram and return final transcript.
        Returns None if interrupted or no speech detected.
        """
        cfg = self.config
        url = (
            f"wss://api.deepgram.com/v1/listen"
            f"?encoding=linear16"
            f"&sample_rate={cfg.audio.sample_rate}"
            f"&channels={cfg.audio.channels}"
            f"&model={cfg.stt.model}"
            f"&language={cfg.stt.language}"
            f"&endpointing={cfg.stt.endpointing_ms}"
            f"&interim_results=true"
            f"&utterance_end_ms={cfg.stt.endpointing_ms}"
            f"&vad_events=true"
        )

        transcript_parts = []
        final_transcript = ""
        speech_started = False

        mic_proc = await self.stream_microphone()
        
        try:
            async with websockets.connect(
                url,
                additional_headers={"Authorization": f"Token {STT_API_KEY}"},
            ) as ws:
                
                async def send_audio():
                    """Send audio chunks to Deepgram with noise gating."""
                    try:
                        while self.running and not self.interrupt_event.is_set():
                            chunk = await mic_proc.stdout.read(4096)
                            if not chunk:
                                break
                            # Apply noise gate before sending
                            gated_chunk = self.apply_noise_gate(chunk)
                            await ws.send(gated_chunk)
                    except Exception as e:
                        logger.debug(f"Audio send stopped: {e}")
                    finally:
                        # Signal end of audio
                        try:
                            await ws.send(json.dumps({"type": "CloseStream"}))
                        except (asyncio.CancelledError, websockets.exceptions.ConnectionClosed, Exception):
                            pass

                async def receive_transcripts():
                    """Receive and process Deepgram responses."""
                    nonlocal final_transcript, speech_started
                    listening_timeout = cfg.stt.listening_timeout_seconds
                    speech_started_time: Optional[float] = None

                    try:
                        while True:
                            if self.interrupt_event.is_set():
                                break

                            # Check listening timeout (time since SpeechStarted with no utterance)
                            if speech_started_time is not None:
                                elapsed = asyncio.get_event_loop().time() - speech_started_time
                                if elapsed >= listening_timeout:
                                    logger.info(f"Listening timeout after {elapsed:.1f}s with no utterance")
                                    return

                            # Receive with short timeout so we can check for listening timeout
                            try:
                                msg = await asyncio.wait_for(ws.recv(), timeout=0.5)
                            except asyncio.TimeoutError:
                                continue

                            data = json.loads(msg)
                            msg_type = data.get("type")

                            # Handle VAD speech start
                            if msg_type == "SpeechStarted":
                                if not speech_started:
                                    speech_started = True
                                    speech_started_time = asyncio.get_event_loop().time()
                                    self.speech_detected_event.set()
                                    await self.set_state(State.LISTENING)
                                    logger.info("Speech started")

                            # Handle transcription results
                            elif msg_type == "Results":
                                channel = data.get("channel", {})
                                alternatives = channel.get("alternatives", [])

                                if alternatives:
                                    transcript = alternatives[0].get("transcript", "")
                                    is_final = data.get("is_final", False)
                                    speech_final = data.get("speech_final", False)

                                    if transcript:
                                        if is_final:
                                            transcript_parts.append(transcript)
                                            logger.debug(f"Final segment: {transcript}")
                                        else:
                                            logger.debug(f"Interim: {transcript}")

                                    # Speech final means user stopped talking
                                    if speech_final:
                                        final_transcript = " ".join(transcript_parts)
                                        logger.info(f"Speech ended: {final_transcript}")
                                        return

                            # Handle utterance end (backup for speech_final)
                            elif msg_type == "UtteranceEnd":
                                if transcript_parts:
                                    final_transcript = " ".join(transcript_parts)
                                    logger.info(f"Utterance end: {final_transcript}")
                                    return

                    except websockets.exceptions.ConnectionClosed:
                        pass
                    except Exception as e:
                        logger.error(f"Receive error: {e}")

                # Run send and receive concurrently
                send_task = asyncio.create_task(send_audio())
                receive_task = asyncio.create_task(receive_transcripts())
                
                # Wait for receive to complete (speech ended) or interrupt
                done, pending = await asyncio.wait(
                    [receive_task],
                    timeout=cfg.stt.max_session_seconds,
                )
                
                # Cancel send task
                send_task.cancel()
                try:
                    await send_task
                except asyncio.CancelledError:
                    pass
                    
        except Exception as e:
            logger.error(f"STT error: {e}")
            return None
        finally:
            # Clean up mic process
            mic_proc.terminate()
            try:
                await asyncio.wait_for(mic_proc.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                mic_proc.kill()

        return final_transcript if final_transcript.strip() else None

    async def query_openclaw(self, text: str) -> Optional[str]:
        """Send text to OpenClaw and get response."""
        await self.set_state(State.THINKING)
        cfg = self.config.llm

        try:
            session = await self.get_http_session()
            async with session.post(
                f"{cfg.url}/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENCLAW_TOKEN}",
                    "Content-Type": "application/json",
                    "x-openclaw-session-key": cfg.session_id,
                },
                json={
                    "model": "openclaw",
                    "messages": [
                        {"role": "user", "content": f"{SYSTEM_PROMPT}\n{text}"},
                    ],
                },
                timeout=aiohttp.ClientTimeout(total=cfg.timeout_seconds),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    content = data["choices"][0]["message"]["content"]
                    logger.info(f"OpenClaw response: {content[:100]}...")
                    return content
                else:
                    error = await resp.text()
                    logger.error(f"OpenClaw error {resp.status}: {error}")
                    return None
        except aiohttp.ClientError as e:
            logger.error(f"OpenClaw request failed: {e}")
            return None
        except Exception as e:
            logger.error(f"OpenClaw request failed: {e}")
            return None

    async def speak_text(self, text: str) -> None:
        """Stream text to TTS and play audio."""
        await self.set_state(State.SPEAKING)

        if self.config.tts.provider == "edge":
            await self.speak_text_edge(text)
        else:
            await self.speak_text_elevenlabs(text)

    async def speak_text_edge(self, text: str) -> None:
        """Use Edge TTS (free, no API key)."""
        import edge_tts  # type: ignore
        import tempfile

        # Create temp file for audio
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            temp_path = f.name

        try:
            # Generate audio
            communicate = edge_tts.Communicate(text, self.config.tts.edge.voice)
            await communicate.save(temp_path)

            if self.interrupt_event.is_set():
                return

            # Play with ffplay
            player_proc = await asyncio.create_subprocess_exec(
                "ffplay",
                "-nodisp",
                "-autoexit",
                "-loglevel", "error",
                temp_path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )

            # Wait for playback, checking for interrupts
            while player_proc.returncode is None:
                if self.interrupt_event.is_set():
                    logger.info("TTS interrupted")
                    player_proc.terminate()
                    break
                try:
                    await asyncio.wait_for(player_proc.wait(), timeout=0.1)
                except asyncio.TimeoutError:
                    pass

        except Exception as e:
            logger.error(f"Edge TTS error: {e}")
        finally:
            # Clean up temp file
            try:
                os.unlink(temp_path)
            except OSError:
                pass

    async def speak_text_elevenlabs(self, text: str) -> None:
        """Stream text to ElevenLabs TTS and play audio."""
        cfg = self.config.tts.elevenlabs
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{cfg.voice_id}/stream"

        # Start audio player process
        player_proc = await asyncio.create_subprocess_exec(
            "pw-play",
            "--rate", "44100",
            "--channels", "1",
            "--format", "s16",
            "-",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

        try:
            session = await self.get_http_session()
            async with session.post(
                url,
                headers={
                    "xi-api-key": TTS_API_KEY,
                    "Content-Type": "application/json",
                },
                json={
                    "text": text,
                    "model_id": cfg.model,
                    "voice_settings": {
                        "stability": cfg.stability,
                        "similarity_boost": cfg.similarity_boost,
                    },
                    "output_format": "pcm_44100",
                },
            ) as resp:
                    if resp.status != 200:
                        error = await resp.text()
                        logger.error(f"TTS error {resp.status}: {error}")
                        return
                    
                    # Stream audio chunks to player
                    chunk_count = 0
                    async for chunk in resp.content.iter_chunked(4096):
                        if self.interrupt_event.is_set():
                            logger.info(f"TTS interrupted after {chunk_count} chunks")
                            break
                        
                        if player_proc.stdin:
                            player_proc.stdin.write(chunk)
                            await player_proc.stdin.drain()
                        chunk_count += 1
                    
                    logger.info(f"TTS stream finished: {chunk_count} chunks received")
                    
                    # Close stdin to signal EOF, then wait for player to finish
                    if player_proc.stdin:
                        player_proc.stdin.close()
                    
                    # Wait for playback to complete, checking for interrupts
                    while player_proc.returncode is None:
                        if self.interrupt_event.is_set():
                            logger.info("TTS playback interrupted")
                            player_proc.terminate()
                            break
                        try:
                            await asyncio.wait_for(player_proc.wait(), timeout=0.1)
                        except asyncio.TimeoutError:
                            pass
                    
                    logger.info("TTS playback finished")
                    return  # Clean exit, skip finally termination
                    
        except Exception as e:
            logger.error(f"TTS error: {e}")
        finally:
            # Clean up player (only runs on error/interrupt paths)
            if player_proc.stdin and not player_proc.stdin.is_closing():
                player_proc.stdin.close()
            if player_proc.returncode is None:
                player_proc.terminate()
                try:
                    await asyncio.wait_for(player_proc.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    player_proc.kill()

    async def listen_for_interrupt(self) -> None:
        """
        Monitor for speech during TTS playback to trigger interrupt.
        This runs a lightweight STT connection just for VAD.
        """
        cfg = self.config
        url = (
            f"wss://api.deepgram.com/v1/listen"
            f"?encoding=linear16"
            f"&sample_rate={cfg.audio.sample_rate}"
            f"&channels={cfg.audio.channels}"
            f"&model={cfg.stt.model}"
            f"&vad_events=true"
        )
        
        mic_proc = await self.stream_microphone()
        
        try:
            async with websockets.connect(
                url,
                additional_headers={"Authorization": f"Token {STT_API_KEY}"},
            ) as ws:
                
                async def send_audio():
                    try:
                        while not self.interrupt_event.is_set():
                            chunk = await mic_proc.stdout.read(4096)
                            if not chunk:
                                break
                            # Apply noise gate before sending
                            gated_chunk = self.apply_noise_gate(chunk)
                            await ws.send(gated_chunk)
                    except (asyncio.CancelledError, websockets.exceptions.ConnectionClosed):
                        pass
                    except Exception as e:
                        logger.debug(f"Interrupt audio send stopped: {e}")

                async def check_vad():
                    try:
                        speech_start_time = None
                        sustained_threshold = cfg.interrupt.sustained_threshold_seconds

                        async def process_messages():
                            nonlocal speech_start_time
                            async for msg in ws:
                                if self.interrupt_event.is_set():
                                    return
                                data = json.loads(msg)
                                msg_type = data.get("type")

                                if msg_type == "SpeechStarted":
                                    speech_start_time = asyncio.get_event_loop().time()
                                    logger.debug("Potential interrupt: speech started")

                                elif msg_type == "SpeechEnded":
                                    # Speech ended before threshold - reset
                                    if speech_start_time is not None:
                                        duration = asyncio.get_event_loop().time() - speech_start_time
                                        logger.debug(f"Speech ended after {duration:.2f}s (below threshold)")
                                    speech_start_time = None

                        async def check_duration():
                            nonlocal speech_start_time
                            while not self.interrupt_event.is_set():
                                await asyncio.sleep(0.05)  # Check every 50ms
                                if speech_start_time is not None:
                                    duration = asyncio.get_event_loop().time() - speech_start_time
                                    if duration >= sustained_threshold:
                                        logger.info(f"Interrupt: sustained speech detected ({duration:.2f}s)")
                                        self.interrupt_event.set()
                                        return

                        # Run message processing and duration checking concurrently
                        msg_task = asyncio.create_task(process_messages())
                        duration_task = asyncio.create_task(check_duration())

                        await asyncio.wait(
                            [msg_task, duration_task],
                            return_when=asyncio.FIRST_COMPLETED,
                        )

                        msg_task.cancel()
                        duration_task.cancel()
                    except (asyncio.CancelledError, websockets.exceptions.ConnectionClosed):
                        pass
                    except Exception as e:
                        logger.debug(f"Interrupt VAD check stopped: {e}")

                send_task = asyncio.create_task(send_audio())
                vad_task = asyncio.create_task(check_vad())
                
                # Wait for interrupt or TTS to finish
                await asyncio.wait(
                    [vad_task],
                    timeout=300,
                )
                
                send_task.cancel()
                try:
                    await send_task
                except asyncio.CancelledError:
                    pass
                    
        except Exception as e:
            logger.debug(f"Interrupt listener error: {e}")
        finally:
            mic_proc.terminate()
            try:
                await asyncio.wait_for(mic_proc.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                mic_proc.kill()

    async def conversation_turn(self) -> None:
        """Run one turn of conversation."""
        # Reset events
        self.interrupt_event.clear()
        self.speech_detected_event.clear()

        # 1. Listen for speech
        await self.set_state(State.IDLE)
        transcript = await self.run_stt()

        if not transcript:
            logger.info("No speech detected, continuing to listen")
            return

        # 2. Query OpenClaw
        response = await self.query_openclaw(transcript)

        if not response:
            await self.set_state(State.ERROR)
            await asyncio.sleep(2)  # Show error briefly
            return

        # 3. Speak response with interrupt monitoring
        # Start TTS and interrupt listener concurrently
        interrupt_delay = self.config.interrupt.delay_seconds

        async def delayed_interrupt_listener():
            # Small delay before listening for interrupts to avoid false triggers
            # from TTS audio startup or residual mic noise
            await asyncio.sleep(interrupt_delay)
            await self.listen_for_interrupt()

        tts_task = asyncio.create_task(self.speak_text(response))
        interrupt_task = asyncio.create_task(delayed_interrupt_listener())

        try:
            # Wait for TTS to complete or be interrupted
            await tts_task
        finally:
            # Stop interrupt listener
            self.interrupt_event.set()
            interrupt_task.cancel()
            try:
                await interrupt_task
            except asyncio.CancelledError:
                pass

    async def run(self) -> None:
        """Main loop."""
        self.running = True
        cfg = self.config
        logger.info("OpenClaw Voice starting...")
        logger.info(f"Config: mic_device={cfg.audio.mic_device}")
        logger.info(f"Config: stt_model={cfg.stt.model}, endpointing={cfg.stt.endpointing_ms}ms")
        logger.info(f"Config: tts_provider={cfg.tts.provider}")
        logger.info(f"Config: gate_threshold={cfg.audio.gate_threshold} RMS")
        logger.info(f"Config: llm_url={cfg.llm.url}")

        # Start Quickshell overlay as subprocess
        shell_qml = get_qml_file()
        logger.info(f"Using QML file: {shell_qml}")
        self.quickshell_proc = await asyncio.create_subprocess_exec(
            "quickshell", "-p", str(shell_qml),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        logger.info(f"Started Quickshell (PID: {self.quickshell_proc.pid})")

        await self.set_state(State.DORMANT)

        try:
            while self.running:
                # Wait in dormant state until activated
                if not self.active:
                    await self.set_state(State.DORMANT)
                    self.toggle_event.clear()
                    logger.info("Dormant - waiting for SIGUSR1 to activate")
                    await self.toggle_event.wait()
                    if not self.active:
                        continue

                # Active mode - run conversation turns
                self.interrupt_event.clear()
                try:
                    await self.conversation_turn()
                except Exception as e:
                    logger.error(f"Conversation error: {e}")
                    await self.set_state(State.ERROR)
                    await asyncio.sleep(2)
        except asyncio.CancelledError:
            logger.info("OpenClaw Voice shutting down...")
        finally:
            await self.set_state(State.DORMANT)
            await self.cleanup()
            self.running = False
            # Clean up Quickshell
            if self.quickshell_proc and self.quickshell_proc.returncode is None:
                logger.info("Terminating Quickshell")
                self.quickshell_proc.terminate()
                try:
                    await asyncio.wait_for(self.quickshell_proc.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    self.quickshell_proc.kill()

    def stop(self):
        """Stop the main loop."""
        self.running = False
        self.interrupt_event.set()


async def main():
    global STT_API_KEY, TTS_API_KEY, OPENCLAW_TOKEN, STATE_FILE, LOG_FILE

    args = parse_args()

    # Set up logging first (before any log messages)
    setup_logging(use_log_file=not args.no_log_file)

    # Initialize global path variables
    STATE_FILE = get_state_file()
    LOG_FILE = get_log_file()

    # Load environment variables from .env file
    load_environment(args.env_file)

    # Now load API keys from environment
    STT_API_KEY = os.getenv("STT_API_KEY")
    TTS_API_KEY = os.getenv("TTS_API_KEY")
    OPENCLAW_TOKEN = os.getenv("OPENCLAW_TOKEN")

    config = Config.load(args.config)
    logger.info(f"Loaded config from: {args.config or 'default'}")

    voice = OpenClawVoice(config)

    # Handle graceful shutdown
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, voice.stop)

    # Handle SIGUSR1 for toggling active/dormant state
    loop.add_signal_handler(signal.SIGUSR1, voice.toggle_active)

    await voice.run()


if __name__ == "__main__":
    asyncio.run(main())
