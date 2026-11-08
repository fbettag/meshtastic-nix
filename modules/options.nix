{
  config,
  lib,
  pkgs,
  ...
}:
let
  inherit (lib) mkOption types;
  cfg = config.services.meshtastic;
  hasSecret =
    value:
    if builtins.isAttrs value then
      lib.any (
        key:
        builtins.elem key [
          "psk"
          "wifi_psk"
          "fixed_pin"
          "admin_key"
          "private_key"
        ]
        || lib.hasInfix "password" key
        || hasSecret value.${key}
      ) (builtins.attrNames value)
    else if builtins.isList value then
      lib.any hasSecret value
    else
      false;
in
{
  options.services.meshtastic = {
    enable = lib.mkEnableOption "declarative Meshtastic management";
    package = mkOption {
      type = types.package;
      default = pkgs.callPackage ../package.nix { };
      description = "Meshtastic reconciler package.";
    };
    devices = mkOption {
      default = { };
      type = types.attrsOf (
        types.submodule {
          options = {
            host = mkOption {
              type = types.str;
              description = "LAN hostname or address.";
            };
            port = mkOption {
              type = types.port;
              default = 4403;
            };
            expectedNodeId = mkOption { type = types.strMatching "![0-9a-fA-F]{8}"; };
            hardwareModel = mkOption {
              type = types.nullOr types.str;
              default = null;
            };
            firmwareVersion = mkOption {
              type = types.nullOr types.str;
              default = null;
              description = "Optional exact version guard. This module never flashes firmware.";
            };
            autoApply = mkOption {
              type = types.bool;
              default = false;
              description = "Apply at boot and when the profile or runtime secrets change.";
            };
            settings = mkOption {
              type = types.attrs;
              default = { };
              description = "Managed owner, config, moduleConfig and channel fields, using protobuf snake_case names. Omitted fields are preserved.";
            };
            secretsFile = mkOption {
              type = types.nullOr types.str;
              default = null;
              description = "Absolute runtime path to a JSON settings overlay. Never use a Nix path literal for secrets.";
            };
          };
        }
      );
    };
  };
  config = lib.mkIf cfg.enable {
    assertions = lib.mapAttrsToList (name: device: {
      assertion =
        !hasSecret device.settings
        && builtins.match "[a-zA-Z0-9][a-zA-Z0-9_-]*" name != null
        && (
          device.secretsFile == null
          || (lib.hasPrefix "/" device.secretsFile && !lib.hasPrefix "/nix/store/" device.secretsFile)
        );
      message = "Meshtastic device ${name} needs a safe unit name, no literal secret fields and a runtime secrets path outside the Nix store.";
    }) cfg.devices;
    environment.systemPackages = [ cfg.package ];
    environment.etc = lib.mapAttrs' (
      name: device:
      lib.nameValuePair "meshtastic/${name}.json" {
        source = import ./profile.nix { inherit pkgs name device; };
      }
    ) cfg.devices;
  };
}
