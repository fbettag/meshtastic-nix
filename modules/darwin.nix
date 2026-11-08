{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.services.meshtastic;
in
{
  imports = [ ./options.nix ];
  config = lib.mkIf cfg.enable {
    launchd.daemons = lib.mapAttrs' (
      name: device:
      let
        profile = import ./profile.nix { inherit pkgs name device; };
      in
      lib.nameValuePair "meshtastic-${name}" {
        serviceConfig = {
          Label = "org.nixos.meshtastic-${name}";
          ProgramArguments = [
            "${cfg.package}/bin/meshtastic-nix"
            "apply"
            "--config"
            "${profile}"
            "--state-dir"
            "/var/lib/meshtastic/${name}"
          ]
          ++ lib.optionals (device.secretsFile != null) [
            "--secrets"
            device.secretsFile
          ];
          RunAtLoad = device.autoApply;
          # launchd watches atomic secret replacement, including sops symlinks.
          WatchPaths = lib.optionals (device.autoApply && device.secretsFile != null) [ device.secretsFile ];
          ProcessType = "Background";
          UserName = "root";
          Umask = 63;
        };
      }
    ) cfg.devices;
  };
}
