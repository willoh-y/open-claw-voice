{
  description = "OpenClaw Voice - Voice chat with OpenClaw";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};

        python = pkgs.python312;
        pythonPackages = python.pkgs;

        # Runtime dependencies needed for the application
        runtimeDeps = with pkgs; [
          pipewire      # pw-record, pw-play
          ffmpeg        # ffplay (for Edge TTS)
          quickshell    # UI overlay
        ];

        # Build the main application package
        open-claw-voice = pkgs.stdenv.mkDerivation {
          pname = "open-claw-voice";
          version = "0.1.0";

          src = ./.;

          nativeBuildInputs = [ pkgs.makeWrapper ];

          buildInputs = [
            (python.withPackages (ps: with ps; [
              numpy
              websockets
              aiohttp
              python-dotenv
              edge-tts
              pyyaml
            ]))
          ];

          # No build step needed
          dontBuild = true;

          installPhase = ''
            runHook preInstall

            # Install Python modules
            mkdir -p $out/lib/open-claw-voice
            cp open_claw_voice.py $out/lib/open-claw-voice/
            cp config.py $out/lib/open-claw-voice/

            # Install QML UI file
            mkdir -p $out/share/open-claw-voice
            cp shell.qml $out/share/open-claw-voice/

            # Install example config and env files
            cp config.yaml $out/share/open-claw-voice/config.yaml.example
            cp .env.example $out/share/open-claw-voice/

            # Create wrapper script
            mkdir -p $out/bin
            makeWrapper ${python.withPackages (ps: with ps; [
              numpy
              websockets
              aiohttp
              python-dotenv
              edge-tts
              pyyaml
            ])}/bin/python $out/bin/open-claw-voice \
              --add-flags "$out/lib/open-claw-voice/open_claw_voice.py" \
              --prefix PATH : ${pkgs.lib.makeBinPath runtimeDeps} \
              --set OPEN_CLAW_VOICE_QML_PATH "$out/share/open-claw-voice/shell.qml"

            runHook postInstall
          '';

          meta = with pkgs.lib; {
            description = "Voice chat interface for OpenClaw";
            license = licenses.mit;
            platforms = platforms.linux;
            mainProgram = "open-claw-voice";
          };
        };

      in {
        # Main package
        packages = {
          default = open-claw-voice;
          open-claw-voice = open-claw-voice;
        };

        # Development shell
        devShells.default = pkgs.mkShell {
          buildInputs = runtimeDeps ++ [
            (python.withPackages (ps: with ps; [
              numpy
              websockets
              aiohttp
              python-dotenv
              edge-tts
              pyyaml
            ]))
          ];

          shellHook = ''
            echo "OpenClaw Voice development environment"
            echo ""
            echo "Run: ./open_claw_voice.py --help"
            echo ""
            echo "First time setup:"
            echo "  1. Copy .env.example to .env and fill in your API keys"
            echo "  2. Edit config.yaml to customize settings"
            echo ""
            echo "Or for production setup:"
            echo "  mkdir -p ~/.config/open-claw-voice"
            echo "  cp .env.example ~/.config/open-claw-voice/.env"
            echo "  cp config.yaml ~/.config/open-claw-voice/config.yaml"
            echo "  # Edit the files with your settings"
            echo ""

            # Set dev mode so local files are used
            export OPEN_CLAW_VOICE_DEV=1
          '';
        };
      }
    ) // {
      # NixOS module (system-independent)
      nixosModules.default = { config, lib, pkgs, ... }:
        with lib;
        let
          cfg = config.services.open-claw-voice;
          # Get the package for the current system
          pkg = self.packages.${pkgs.system}.default;
        in {
          options.services.open-claw-voice = {
            enable = mkEnableOption "OpenClaw Voice service";

            package = mkOption {
              type = types.package;
              default = pkg;
              defaultText = literalExpression "pkgs.open-claw-voice";
              description = "The open-claw-voice package to use.";
            };

            user = mkOption {
              type = types.str;
              description = "User to run the service as.";
            };

            configFile = mkOption {
              type = types.nullOr types.path;
              default = null;
              description = ''
                Path to config.yaml file.
                If null, will use ~/.config/open-claw-voice/config.yaml
              '';
            };

            environmentFile = mkOption {
              type = types.nullOr types.path;
              default = null;
              description = ''
                Path to file containing environment variables (API keys).
                This file should contain lines like:
                  STT_API_KEY=your_key_here
                  TTS_API_KEY=your_key_here
                  OPENCLAW_TOKEN=your_token_here

                If null, the application will look for .env in the XDG config directory.

                For secret management with agenix:
                  services.open-claw-voice.environmentFile = config.age.secrets.open-claw-voice.path;

                For secret management with sops-nix:
                  services.open-claw-voice.environmentFile = config.sops.secrets.open-claw-voice.path;
              '';
            };
          };

          config = mkIf cfg.enable {
            # Add package to system packages
            environment.systemPackages = [ cfg.package ];

            # Create systemd user service
            systemd.user.services.open-claw-voice = {
              description = "OpenClaw Voice - Voice chat with OpenClaw";
              wantedBy = [ "default.target" ];
              after = [ "pipewire.service" "pipewire-pulse.service" ];
              requires = [ "pipewire.service" ];

              serviceConfig = {
                Type = "simple";
                ExecStart =
                  let
                    args = lib.concatStringsSep " " (
                      [ "--no-log-file" ]
                      ++ lib.optional (cfg.configFile != null) "--config ${cfg.configFile}"
                    );
                  in "${cfg.package}/bin/open-claw-voice ${args}";

                Restart = "on-failure";
                RestartSec = "5s";

                # Allow SIGUSR1 for toggling active/dormant state
                # Users can run: systemctl --user kill -s SIGUSR1 open-claw-voice
                KillMode = "mixed";
                KillSignal = "SIGTERM";
              };

              # Environment file for API keys (if specified)
              environment = {
                # Ensure XDG directories are available
                XDG_RUNTIME_DIR = "/run/user/%U";
              };
            } // lib.optionalAttrs (cfg.environmentFile != null) {
              serviceConfig.EnvironmentFile = cfg.environmentFile;
            };
          };
        };
    };
}
