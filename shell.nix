{
  pkgs ? import <nixpkgs> { },
}:

pkgs.mkShell {
  buildInputs = with pkgs; [
    pipewire # pw-record, pw-play
    ffmpeg # ffplay (for Edge TTS fallback)
    quickshell # UI overlay
    (python3.withPackages (
      ps: with ps; [
        numpy
        websockets
        aiohttp
        python-dotenv
        edge-tts
        pyyaml
        webrtcvad
        setuptools
      ]
    ))
  ];
}
