{
  description = "commerce-agents development shell";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-24.11";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin" ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          packages = [ pkgs.python311 pkgs.git pkgs.coreutils ];

          shellHook = ''
            root="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
            venv="$root/.venv"
            stamp="$venv/.requirements.sha256"

            if [ ! -x "$venv/bin/python" ]; then
              python -m venv "$venv"
              rm -f "$stamp"
            fi
            . "$venv/bin/activate"

            want="$(cat "$root/requirements.txt" "$root/requirements-dev.txt" | sha256sum | cut -d' ' -f1)"
            if [ "$(cat "$stamp" 2>/dev/null)" != "$want" ]; then
              echo "Installing requirements into .venv"
              if pip install -r "$root/requirements.txt" -r "$root/requirements-dev.txt"; then
                echo "$want" > "$stamp"
              else
                echo "pip install failed; it will run again on the next shell" >&2
              fi
            fi
          '';
        };
      });
    };
}
