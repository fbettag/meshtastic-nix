{
  description = "Declarative Meshtastic configuration for NixOS and nix-darwin";
  inputs.nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";
  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "aarch64-darwin"
        "aarch64-linux"
        "x86_64-linux"
      ];
      forSystems = nixpkgs.lib.genAttrs systems;
    in
    {
      packages = forSystems (system: {
        default = nixpkgs.legacyPackages.${system}.callPackage ./package.nix { };
      });
      apps = forSystems (system: {
        default = {
          type = "app";
          program = "${self.packages.${system}.default}/bin/meshtastic-nix";
        };
      });
      formatter = forSystems (system: nixpkgs.legacyPackages.${system}.nixfmt-tree);
      checks = forSystems (system: {
        package = self.packages.${system}.default;
        modules =
          let
            pkgs = nixpkgs.legacyPackages.${system};
            sample = {
              services.meshtastic = {
                enable = true;
                devices.test = {
                  host = "192.0.2.1";
                  expectedNodeId = "!12345678";
                  autoApply = true;
                  secretsFile = "/run/secrets/mesh";
                  settings.config.device.role = "CLIENT";
                };
              };
            };
            linux = nixpkgs.lib.nixosSystem {
              system = "x86_64-linux";
              modules = [
                self.nixosModules.default
                sample
              ];
            };
            darwin = nixpkgs.lib.evalModules {
              specialArgs = { inherit pkgs; };
              modules = [
                self.darwinModules.default
                sample
                ({ lib, ... }: {
                  options.environment.systemPackages = lib.mkOption { type = lib.types.listOf lib.types.package; };
                  options.environment.etc = lib.mkOption { type = lib.types.attrs; };
                  options.launchd.daemons = lib.mkOption { type = lib.types.attrs; };
                  options.assertions = lib.mkOption { type = lib.types.listOf lib.types.attrs; };
                })
              ];
            };
          in
          assert linux.config.systemd.services.meshtastic-test.serviceConfig.Type == "oneshot";
          assert
            linux.config.systemd.services.meshtastic-test.serviceConfig.LoadCredential
            == [ "settings:/run/secrets/mesh" ];
          assert darwin.config.launchd.daemons.meshtastic-test.serviceConfig.RunAtLoad;
          pkgs.runCommand "meshtastic-modules" { } "touch $out";
      });
      nixosModules.default = import ./modules/nixos.nix;
      darwinModules.default = import ./modules/darwin.nix;
    };
}
