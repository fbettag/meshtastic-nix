# meshtastic-nix

Declarative Meshtastic configuration over the local TCP API, packaged as a Nix
flake with NixOS and nix-darwin modules. A phone is not required.

The reconciler manages **only the fields you declare**. It preserves omitted
fields, other channels, device identity keys and the node database. Every write
requires the expected node ID, creates a private local backup, applies a settings
transaction, and checks the result through a fresh connection. Reapplying a
matching profile performs no writes. This is configuration management, not
firmware flashing or a complete factory image.

## NixOS

```nix
{
  inputs.meshtastic-nix.url = "git+ssh://git@github.com/fbettag/meshtastic-nix.git?ref=main";
  inputs.meshtastic-nix.inputs.nixpkgs.follows = "nixpkgs";
  # In the host's module list:
  # inputs.meshtastic-nix.nixosModules.default
}
```

```nix
services.meshtastic = {
  enable = true;
  devices.office = {
    host = "mesh-office.example.net";
    expectedNodeId = "!12345678";
    hardwareModel = "HELTEC_V3";
    autoApply = true;
    # Optional exact guard, not a firmware updater:
    # firmwareVersion = "2.7.26.54e0d8d";
    secretsFile = config.sops.secrets.meshtastic-settings.path;
    settings = {
      owner = { long_name = "Office"; short_name = "OFF"; };
      config = {
        device.role = "CLIENT";
        network.wifi_enabled = true;
        lora = { region = "EU_868"; use_preset = true; modem_preset = "LONG_FAST"; };
      };
      channels."1" = { role = "SECONDARY"; settings.name = "private"; };
    };
  };
};
```

`meshtastic-office.service` runs at boot and restarts on profile changes.
For sops-nix secrets, set `restartUnits = [ "meshtastic-office.service" ]` so
secret changes also reconcile the device. The service copies secrets using
systemd credentials and stores backups in `/var/lib/meshtastic/office/` with
directory mode `0700` and file mode `0600`. Backups contain secrets.

With `autoApply = false` the unit is available for manual start but is not
enabled at boot. An unreachable device fails its unit; it does not interrupt
the other devices or retry indefinitely. Inspect `journalctl -u
meshtastic-office` and restart the unit when the device is back online.
Public profiles are available under `/etc/meshtastic/<name>.json` for CLI use.

For nix-darwin, import `darwinModules.default` and use the same options. A root
LaunchDaemon runs at load for `autoApply` devices and watches the runtime secrets
path. Profile changes replace its launchd definition. Neither module provisions
the initial WLAN connection: first enable TCP access over USB or another client.

## Runtime secrets

`secretsFile` is an absolute **string** path to a runtime JSON settings overlay.
Do not use a Nix path literal or `builtins.readFile` for plaintext secrets. The
overlay has the same shape as `settings` and cannot replace the connection or
node ID guard. For example, an encrypted sops secret can decrypt to this JSON:

```json
{
  "config": { "network": { "wifi_psk": "your-wlan-password" } },
  "channels": { "1": { "settings": { "psk": "your-base64-channel-key" } } }
}
```

Passwords, channel keys and Bluetooth PINs are rejected in public settings.
All output reports field paths, never values. SDK logging is disabled to avoid
logging configuration secrets. Changing WLAN credentials can disconnect a
device; read-back then fails until the new connection is reachable.

## CLI

The CLI accepts a JSON profile with `host`, `expectedNodeId`, optional `port`
(default `4403`), `hardwareModel`, `firmwareVersion`, and `settings`.

```sh
nix run . -- diff --config profile.json --secrets /run/secrets/mesh --state-dir ./private-state
nix run . -- check --config profile.json --secrets /run/secrets/mesh --state-dir ./private-state
nix run . -- apply --config profile.json --secrets /run/secrets/mesh --state-dir ./private-state
```

`diff` is read-only and exits zero on a successful comparison. `check` exits 2
for drift, 0 when matching. `apply` exits zero only after read-back matches.
Operational errors exit 1. Unknown fields and invalid enum values fail before
connecting. Fields use protobuf `snake_case`; channels are keyed by indices
`"0"` through `"7"`. Null values are rejected. Repeated fields, when declared,
are replaced; omitted fields and channels remain unchanged. Identity keys
cannot be managed. Owner management is limited to long and short names.
The pinned SDK cannot write the `statusmessage` and `tak` modules; these are
rejected before connecting.

Use one controller per device. A local advisory lock prevents concurrent runs
using the same state directory, including read-only comparisons. The API has
no distributed lock and the SDK's local transaction writes do not provide
per-field acknowledgments. The fresh read-back is therefore the success check.
Partial failures can leave settings changed; inspect the backup and reconcile
again. A NixOS rollback changes the declared profile and can reapply it, but
does not undo radio writes atomically or restore firmware. No automatic firmware
download, flashing, reset or node-database clearing is performed.

The LAN API is not exposed by these modules. Keep it on a trusted LAN and avoid
other clients writing concurrently. API behavior is based on the official
[Meshtastic Python API](https://github.com/meshtastic/python) and
[TCP CLI documentation](https://meshtastic.org/docs/software/python/cli/#--host-host).

## Development

`nix fmt` formats Nix files. `nix flake check` builds the package, runs the Python
tests and evaluates the NixOS and Darwin service definitions. The package uses
the Meshtastic library pinned through nixpkgs and requires a compatible 2.7.x
Python API. Device firmware can be older where its configuration schema agrees.
