"""Configuration loader for OpenClaw Voice."""

import argparse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml


@dataclass
class AudioConfig:
    sample_rate: int = 16000
    channels: int = 1
    mic_device: str = ""
    gate_threshold: int = 200


@dataclass
class STTConfig:
    model: str = "nova-3"
    language: str = "en"
    endpointing_ms: int = 1500
    max_session_seconds: int = 300
    listening_timeout_seconds: float = 10.0  # Timeout after SpeechStarted if no utterance ends


@dataclass
class ElevenLabsConfig:
    voice_id: str = "lLgB6ZeIe84FSJa9pO1a"
    model: str = "eleven_flash_v2_5"
    stability: float = 0.5
    similarity_boost: float = 0.75


@dataclass
class EdgeConfig:
    voice: str = "en-US-ChristopherNeural"


@dataclass
class TTSConfig:
    provider: str = "elevenlabs"
    elevenlabs: ElevenLabsConfig = field(default_factory=ElevenLabsConfig)
    edge: EdgeConfig = field(default_factory=EdgeConfig)


@dataclass
class LLMConfig:
    url: str = "http://127.0.0.1:18789"
    session_id: str = "agent:main:main"
    timeout_seconds: int = 120


@dataclass
class InterruptConfig:
    delay_seconds: float = 0.5
    sustained_threshold_seconds: float = 0.3


@dataclass
class Config:
    audio: AudioConfig = field(default_factory=AudioConfig)
    stt: STTConfig = field(default_factory=STTConfig)
    tts: TTSConfig = field(default_factory=TTSConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    interrupt: InterruptConfig = field(default_factory=InterruptConfig)

    @classmethod
    def from_dict(cls, data: dict) -> "Config":
        """Create Config from a dictionary, using defaults for missing values."""
        audio_data = data.get("audio", {})
        stt_data = data.get("stt", {})
        tts_data = data.get("tts", {})
        llm_data = data.get("llm", {})
        interrupt_data = data.get("interrupt", {})

        # Handle nested TTS config
        elevenlabs_data = tts_data.get("elevenlabs", {})
        edge_data = tts_data.get("edge", {})

        return cls(
            audio=AudioConfig(**audio_data),
            stt=STTConfig(**stt_data),
            tts=TTSConfig(
                provider=tts_data.get("provider", "elevenlabs"),
                elevenlabs=ElevenLabsConfig(**elevenlabs_data),
                edge=EdgeConfig(**edge_data),
            ),
            llm=LLMConfig(**llm_data),
            interrupt=InterruptConfig(**interrupt_data),
        )

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "Config":
        """Load configuration from YAML file."""
        if path is None:
            path = Path.home() / ".config" / "open-claw-voice" / "config.yaml"

        if not path.exists():
            return cls()

        with open(path) as f:
            data = yaml.safe_load(f) or {}

        return cls.from_dict(data)


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="OpenClaw Voice - Voice chat with OpenClaw")
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to config file (default: ~/.config/open-claw-voice/config.yaml)",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        dest="env_file",
        help="Path to .env file with API keys (default: ~/.config/open-claw-voice/.env)",
    )
    parser.add_argument(
        "--no-log-file",
        action="store_true",
        dest="no_log_file",
        help="Don't write to log file, use stdout only (for systemd services)",
    )
    return parser.parse_args()
