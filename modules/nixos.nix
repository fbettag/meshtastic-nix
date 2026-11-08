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
    systemd.services = lib.mapAttrs' (
      name: device:
      let
        profile = import ./profile.nix { inherit pkgs name device; };
      in
      lib.nameValuePair "meshtastic-${name}" {
        description = "Reconcile Meshtastic ${name}";
        wants = [ "network-online.target" ];
        after = [ "network-online.target" ];
        wantedBy = lib.optional device.autoApply "multi-user.target";
        restartTriggers = [ profile ];
        serviceConfig = {
          Type = "oneshot";
          RemainAfterExit = true;
          UMask = "0077";
          StateDirectory = "meshtastic/${name}";
          StateDirectoryMode = "0700";
          LoadCredential = lib.optional (device.secretsFile != null) "settings:${device.secretsFile}";
          ExecStart =
            "${cfg.package}/bin/meshtastic-nix apply --config ${profile} --state-dir /var/lib/meshtastic/${name}"
            + lib.optionalString (device.secretsFile != null) " --secrets %d/settings";
          TimeoutStartSec = 240;
          NoNewPrivileges = true;
          ProtectSystem = "strict";
          ProtectHome = true;
          PrivateTmp = true;
          RestrictAddressFamilies = [
            "AF_INET"
            "AF_INET6"
            "AF_UNIX"
          ];
        };
      }
    ) cfg.devices;
  };
}
