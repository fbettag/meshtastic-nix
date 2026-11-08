{
  pkgs,
  name,
  device,
}:
pkgs.writeText "meshtastic-${name}.json" (
  builtins.toJSON {
    inherit (device)
      host
      port
      expectedNodeId
      hardwareModel
      firmwareVersion
      settings
      ;
  }
)
