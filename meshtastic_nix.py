"""Apply explicit protobuf fields, preserving everything outside the profile."""

import argparse
import base64
import copy
import fcntl
import json
import logging
import os
import re
import socket
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from google.protobuf.json_format import MessageToDict, ParseDict
from meshtastic.protobuf import channel_pb2, localonly_pb2
from meshtastic.tcp_interface import TCPInterface


class ProfileError(Exception):
    """A message safe to include in logs."""


def merge(left, right):
    result = copy.deepcopy(left)
    for key, value in right.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def patched(message, fields, path):
    """Validate snake_case field names and merge without clearing other fields."""
    if not isinstance(fields, dict):
        raise ProfileError(f"Expected an object at {path}")
    for key, value in fields.items():
        field = message.DESCRIPTOR.fields_by_name.get(key)
        if field is None or value is None:
            raise ProfileError(f"Unknown field or null value at {path}.{key}")
        if field.message_type and isinstance(value, dict):
            patched(getattr(message, key), value, f"{path}.{key}")
    result = type(message)()
    result.CopyFrom(message)
    try:
        ParseDict(fields, result)
    except Exception:
        # Protobuf's exception includes input values, which may be passwords.
        raise ProfileError(f"Invalid protobuf value in {path}") from None
    return result


def changed_paths(actual, desired, fields, path):
    result = []
    for key, value in fields.items():
        before, after = getattr(actual, key), getattr(desired, key)
        if isinstance(value, dict):
            result.extend(changed_paths(before, after, value, f"{path}.{key}"))
        elif before != after:
            result.append(f"{path}.{key}")
    return result


def validate_settings(settings, public=False):
    if not isinstance(settings, dict) or set(settings) - {
        "owner",
        "config",
        "moduleConfig",
        "channels",
    }:
        raise ProfileError(
            "Settings may contain only owner, config, moduleConfig and channels"
        )
    for section, template in (
        ("config", localonly_pb2.LocalConfig()),
        ("moduleConfig", localonly_pb2.LocalModuleConfig()),
    ):
        patched(template, settings.get(section, {}), section)
    if set(settings.get("moduleConfig", {})) & {"statusmessage", "tak"}:
        raise ProfileError(
            "The pinned Python API cannot write the statusmessage or tak module"
        )
    owner = settings.get("owner", {})
    if not isinstance(owner, dict) or set(owner) - {
        "long_name",
        "short_name",
        "is_unmessagable",
    }:
        raise ProfileError("Only owner names and is_unmessagable can be managed")
    if "is_unmessagable" in owner and type(owner["is_unmessagable"]) is not bool:
        raise ProfileError("owner.is_unmessagable must be boolean")
    for key, limit in (("long_name", 39), ("short_name", 4)):
        if key in owner and (
            not isinstance(owner[key], str)
            or not owner[key].strip()
            or len(owner[key].encode()) > limit
        ):
            raise ProfileError(f"Invalid owner.{key}")
    channels = settings.get("channels", {})
    if not isinstance(channels, dict):
        raise ProfileError("channels must be an object keyed by index")
    for index, fields in channels.items():
        if str(index) not in [str(i) for i in range(8)]:
            raise ProfileError("Channel index must be 0 through 7")
        if not isinstance(fields, dict) or set(fields) - {"role", "settings"}:
            raise ProfileError("A channel may contain only role and settings")
        patched(channel_pb2.Channel(), fields, f"channels.{index}")
    # Identity keys belong to the device and are never replaced by reconciliation.
    security = settings.get("config", {}).get("security", {})
    if set(security) & {"private_key", "public_key"}:
        raise ProfileError("Device identity keys cannot be managed")

    def check_secrets(obj):
        if isinstance(obj, dict):
            for key, value in obj.items():
                if public and (
                    key in {"psk", "wifi_psk", "fixed_pin", "admin_key"}
                    or "password" in key
                ):
                    raise ProfileError(
                        "Secret fields must come from the runtime secrets file"
                    )
                check_secrets(value)

    check_secrets(settings)


def load_profile(config_file, secrets_file=None):
    profile = json.loads(Path(config_file).read_text())
    allowed = {
        "host",
        "port",
        "expectedNodeId",
        "hardwareModel",
        "firmwareVersion",
        "settings",
    }
    if not isinstance(profile, dict) or set(profile) - allowed:
        raise ProfileError("Unknown profile field")
    if not re.fullmatch(r"![0-9a-fA-F]{8}", profile.get("expectedNodeId", "")):
        raise ProfileError("expectedNodeId must be an eight-digit Meshtastic node ID")
    if not isinstance(profile.get("host"), str) or not profile["host"]:
        raise ProfileError("host is required")
    if (
        type(profile.get("port", 4403)) is not int
        or not 1 <= profile.get("port", 4403) <= 65535
    ):
        raise ProfileError("Invalid TCP port")
    settings = profile.get("settings", {})
    validate_settings(settings, public=True)
    if secrets_file:
        secrets = json.loads(Path(secrets_file).read_text())
        validate_settings(secrets)
        settings = merge(settings, secrets)
    validate_settings(settings)
    profile["settings"] = settings
    return profile


def identify(iface, profile):
    actual = f"!{iface.myInfo.my_node_num:08x}"
    if actual.lower() != profile["expectedNodeId"].lower():
        raise ProfileError(
            f"Node ID mismatch: expected {profile['expectedNodeId']}, received {actual}"
        )
    user = iface.getMyNodeInfo()["user"]
    if profile.get("hardwareModel") and user.get("hwModel") != profile["hardwareModel"]:
        raise ProfileError("Hardware model mismatch")
    if (
        profile.get("firmwareVersion")
        and iface.metadata.firmware_version != profile["firmwareVersion"]
    ):
        raise ProfileError(
            "Firmware version mismatch; configuration cannot flash firmware"
        )


def plan(iface, profile):
    identify(iface, profile)
    settings, node = profile["settings"], iface.localNode
    operations, paths = [], []
    user = iface.getMyNodeInfo()["user"]
    owner = settings.get("owner", {})
    owner_paths = [
        f"owner.{k}"
        for k, v in owner.items()
        if user.get(
            {
                "long_name": "longName",
                "short_name": "shortName",
                "is_unmessagable": "isUnmessagable",
            }[k],
            False if k == "is_unmessagable" else None,
        )
        != v
    ]
    if owner_paths:
        operations.append(("owner", None, owner))
        paths.extend(owner_paths)
    for section, actual in (
        ("config", node.localConfig),
        ("moduleConfig", node.moduleConfig),
    ):
        for group, fields in settings.get(section, {}).items():
            current = getattr(actual, group)
            target = patched(current, fields, f"{section}.{group}")
            changes = changed_paths(current, target, fields, f"{section}.{group}")
            if changes:
                operations.append((section, group, target))
                paths.extend(changes)
    for index, fields in settings.get("channels", {}).items():
        index = int(index)
        if index >= len(node.channels) or node.channels[index].index != index:
            raise ProfileError("Device did not return the requested channel index")
        current = node.channels[index]
        target = patched(current, fields, f"channels.{index}")
        changes = changed_paths(current, target, fields, f"channels.{index}")
        if changes:
            operations.append(("channels", index, target))
            paths.extend(changes)
    # Changing role installs firmware defaults. Apply it before explicit fields,
    # and owner fields last because role defaults can change owner flags too.
    operations.sort(
        key=lambda op: (
            0 if op[:2] == ("config", "device") else 2 if op[0] == "owner" else 1
        )
    )
    return operations, paths


def as_dict(message):
    return MessageToDict(message, preserving_proto_field_name=True)


def private_directory(path):
    path = Path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if (
        path.is_symlink()
        or not path.is_dir()
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o077
    ):
        raise ProfileError(
            "State directory must be owned by the current user with mode 0700"
        )
    return path


@contextmanager
def locked_state(path):
    directory = private_directory(path)
    fd = os.open(directory / "lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ProfileError(
                "Another operation is using this device state directory"
            ) from None
        yield directory
    finally:
        os.close(fd)


def backup(iface, directory):
    snapshot = {
        "read_at": datetime.now(timezone.utc).isoformat(),
        "my_info": as_dict(iface.myInfo),
        "metadata": as_dict(iface.metadata),
        "own_node": iface.getMyNodeInfo(),
        "config": as_dict(iface.localNode.localConfig),
        "module_config": as_dict(iface.localNode.moduleConfig),
        "channels": [as_dict(c) for c in iface.localNode.channels],
        "nodes": iface.nodesByNum,
    }
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = directory / f"before-{stamp}.json"
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(
            snapshot,
            handle,
            default=lambda value: (
                base64.b64encode(value).decode()
                if isinstance(value, bytes)
                else str(value)
            ),
        )
        handle.flush()
        os.fsync(handle.fileno())
    return target


def apply_operations(iface, operations):
    node = iface.localNode
    node.beginSettingsTransaction()
    time.sleep(0.3)
    for section, name, target in operations:
        if section == "owner":
            user = iface.getMyNodeInfo()["user"]
            node.setOwner(**target, is_licensed=user.get("isLicensed", False))
        elif section == "channels":
            node.channels[name].CopyFrom(target)
            node.writeChannel(name)
        else:
            config = node.localConfig if section == "config" else node.moduleConfig
            getattr(config, name).CopyFrom(target)
            node.writeConfig(name)
        time.sleep(0.3)
    node.commitSettingsTransaction()
    # Let the firmware consume the commit before closing the TCP stream.
    time.sleep(2)


class BoundedTCPInterface(TCPInterface):
    def myConnect(self):
        self.socket = socket.create_connection(
            (self.hostname, self.portNumber), timeout=10
        )
        self.socket.settimeout(None)


@contextmanager
def connection(profile):
    iface = BoundedTCPInterface(
        profile["host"],
        portNumber=profile.get("port", 4403),
        timeout=20,
        connectNow=False,
    )
    try:
        iface.connect()
        # The SDK raises on timeout and returns None on success.
        iface.waitForConfig()
        # Config completion releases the constructor before its heartbeat finishes.
        time.sleep(0.5)
        yield iface
    finally:
        iface.close()


def reconcile(profile, command, state_dir, connect=connection, sleep=time.sleep):
    with locked_state(state_dir) as directory:
        with connect(profile) as iface:
            operations, paths = plan(iface, profile)
            print(
                json.dumps(
                    {
                        "node": profile["expectedNodeId"],
                        "firmware": iface.metadata.firmware_version,
                        "status": "drift" if paths else "in-sync",
                        "fields": paths,
                    }
                ),
                flush=True,
            )
            if command != "apply" or not paths:
                return 2 if command == "check" and paths else 0
            saved = backup(iface, directory)
            print(f"Backup: {saved}", flush=True)
            apply_operations(iface, operations)
        # Commit may reboot the radio. Always verify through a fresh connection.
        deadline = time.monotonic() + 120
        passes, writes = 1, len(operations)
        while time.monotonic() < deadline:
            sleep(5)
            try:
                with connect(profile) as iface:
                    retry_operations, remaining = plan(iface, profile)
                    if remaining:
                        if passes >= 3:
                            raise ProfileError(
                                "Configuration did not converge after three passes: "
                                + ", ".join(remaining)
                            )
                        print(
                            json.dumps(
                                {
                                    "node": profile["expectedNodeId"],
                                    "status": "retry",
                                    "fields": remaining,
                                }
                            ),
                            flush=True,
                        )
                        saved = backup(iface, directory)
                        print(f"Backup: {saved}", flush=True)
                        apply_operations(iface, retry_operations)
                        writes += len(retry_operations)
                        passes += 1
                if not remaining:
                    print(
                        json.dumps(
                            {
                                "node": profile["expectedNodeId"],
                                "status": "verified",
                                "writes": writes,
                            }
                        ),
                        flush=True,
                    )
                    return 0
            except ProfileError:
                raise
            except Exception:
                continue
        raise ProfileError(
            "Read-back verification failed or the device did not reconnect within 120 seconds"
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["diff", "check", "apply"])
    parser.add_argument("--config", required=True)
    parser.add_argument(
        "--secrets", help="Runtime JSON settings overlay; contents are never logged"
    )
    parser.add_argument(
        "--state-dir", default=str(Path.home() / ".local/state/meshtastic-nix")
    )
    args = parser.parse_args()
    os.umask(0o077)
    # SDK debug messages and protobuf errors can contain configuration secrets.
    logging.disable(logging.CRITICAL)
    try:
        profile = load_profile(args.config, args.secrets)
        return reconcile(profile, args.command, args.state_dir)
    except ProfileError as error:
        print(f"meshtastic-nix: {error}", file=sys.stderr)
    except Exception as error:
        print(
            f"meshtastic-nix: operation failed ({type(error).__name__}); details suppressed to protect secrets",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    sys.exit(main())
