import copy
import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from google.protobuf.json_format import ParseDict
from meshtastic.protobuf import channel_pb2, localonly_pb2, mesh_pb2

import meshtastic_nix as manager


def radio():
    node = SimpleNamespace(
        localConfig=localonly_pb2.LocalConfig(),
        moduleConfig=localonly_pb2.LocalModuleConfig(),
        channels=[channel_pb2.Channel(index=i) for i in range(8)],
    )
    ParseDict(
        {
            "device": {"role": "ROUTER", "node_info_broadcast_secs": 1234},
            "network": {"wifi_enabled": True, "wifi_psk": "runtime-only"},
        },
        node.localConfig,
    )
    node.channels[0].settings.psk = b"\x01"
    user = {
        "id": "!12345678",
        "longName": "Station",
        "shortName": "TEST",
        "hwModel": "HELTEC_V3",
    }
    return SimpleNamespace(
        localNode=node,
        myInfo=mesh_pb2.MyNodeInfo(my_node_num=0x12345678, reboot_count=1),
        metadata=mesh_pb2.DeviceMetadata(firmware_version="2.7.test"),
        getMyNodeInfo=lambda: {"user": user},
        nodesByNum={},
    )


def profile(settings=None):
    return {
        "host": "192.0.2.1",
        "expectedNodeId": "!12345678",
        "hardwareModel": "HELTEC_V3",
        "settings": settings or {"config": {"device": {"role": "CLIENT"}}},
    }


def test_partial_patch_preserves_other_settings_and_keys():
    iface = radio()
    operations, fields = manager.plan(iface, profile())
    assert fields == ["config.device.role"]
    assert operations[0][2].node_info_broadcast_secs == 1234
    assert iface.localNode.localConfig.network.wifi_psk == "runtime-only"
    assert iface.localNode.channels[0].settings.psk == b"\x01"


def test_proto_defaults_are_managed_and_idempotent():
    iface = radio()
    wanted = profile(
        {"config": {"network": {"wifi_enabled": False}, "device": {"role": "CLIENT"}}}
    )
    operations, paths = manager.plan(iface, wanted)
    assert set(paths) == {"config.network.wifi_enabled", "config.device.role"}
    for _, group, target in operations:
        getattr(iface.localNode.localConfig, group).CopyFrom(target)
    assert manager.plan(iface, wanted) == ([], [])


@pytest.mark.parametrize(
    "guard,value",
    [
        ("expectedNodeId", "!87654321"),
        ("hardwareModel", "OTHER"),
        ("firmwareVersion", "wrong"),
    ],
)
def test_wrong_identity_or_firmware_refuses_before_planning(guard, value):
    wanted = profile()
    wanted[guard] = value
    with pytest.raises(manager.ProfileError):
        manager.plan(radio(), wanted)


@pytest.mark.parametrize(
    "settings",
    [
        {"config": {"device": {"rol": "CLIENT"}}},
        {"config": {"device": {"role": "INVALID"}}},
        {"config": {"device": {"role": None}}},
        {"config": {"security": {"private_key": "AQ=="}}},
        {"channels": {"8": {"role": "SECONDARY"}}},
        {"channels": {"0": {"index": 4}}},
        {"owner": {"short_name": "TOOLONG"}},
        {"host": "an-untrusted-device"},
        {"moduleConfig": {"tak": {"role": "TeamMember"}}},
    ],
)
def test_invalid_settings_rejected_before_connect(settings):
    with pytest.raises(manager.ProfileError):
        manager.validate_settings(settings)


def test_runtime_secret_overlay_and_redacted_parse_error(tmp_path):
    config = tmp_path / "profile.json"
    secret = tmp_path / "secret.json"
    config.write_text(
        json.dumps(profile({"config": {"network": {"wifi_enabled": True}}}))
    )
    secret.write_text(
        json.dumps({"config": {"network": {"wifi_psk": "never-print-this"}}})
    )
    loaded = manager.load_profile(config, secret)
    assert loaded["settings"]["config"]["network"] == {
        "wifi_enabled": True,
        "wifi_psk": "never-print-this",
    }
    config.write_text(
        json.dumps(profile({"config": {"network": {"wifi_psk": "never-print-this"}}}))
    )
    with pytest.raises(manager.ProfileError, match="runtime secrets"):
        manager.load_profile(config)
    with pytest.raises(manager.ProfileError) as error:
        manager.validate_settings({"config": {"device": {"role": "never-print-this"}}})
    assert "never-print-this" not in str(error.value)


def test_channel_merge_preserves_undeclared_settings():
    iface = radio()
    iface.localNode.channels[1].settings.downlink_enabled = True
    wanted = profile(
        {
            "channels": {
                "1": {
                    "role": "SECONDARY",
                    "settings": {"name": "private", "psk": "AQ=="},
                }
            }
        }
    )
    operations, _ = manager.plan(iface, wanted)
    assert operations[0][2].settings.downlink_enabled
    assert operations[0][2].settings.psk == b"\x01"


def test_readback_and_second_apply_does_not_write(tmp_path, monkeypatch, capsys):
    iface = radio()
    writes = []

    @contextmanager
    def connect(_):
        yield iface

    def write(device, operations):
        writes.extend(operations)
        for _, group, target in operations:
            getattr(device.localNode.localConfig, group).CopyFrom(target)
        device.myInfo.reboot_count += 1

    monkeypatch.setattr(manager, "apply_operations", write)
    state = tmp_path / "state"
    assert (
        manager.reconcile(profile(), "apply", state, connect, sleep=lambda _: None) == 0
    )
    assert len(writes) == 1
    saved = list(state.glob("before-*.json"))
    assert len(saved) == 1 and saved[0].stat().st_mode & 0o777 == 0o600
    assert json.loads(saved[0].read_text())["config"]["device"]["role"] == "ROUTER"
    assert manager.reconcile(profile(), "apply", state, connect) == 0
    assert len(writes) == 1
    assert "runtime-only" not in capsys.readouterr().out


def test_diff_check_and_wrong_node_never_write(tmp_path, monkeypatch):
    iface = radio()

    @contextmanager
    def connect(_):
        yield iface

    def no_writes(*_):
        pytest.fail("Read-only command attempted to write")

    monkeypatch.setattr(manager, "apply_operations", no_writes)
    assert manager.reconcile(profile(), "diff", tmp_path / "state", connect) == 0
    assert manager.reconcile(profile(), "check", tmp_path / "state", connect) == 2
    wrong = copy.deepcopy(profile())
    wrong["expectedNodeId"] = "!00000000"
    with pytest.raises(manager.ProfileError):
        manager.reconcile(wrong, "apply", tmp_path / "state", connect)
    assert not list((tmp_path / "state").glob("before-*.json"))


def test_failed_readback_does_not_report_success(tmp_path, monkeypatch):
    iface = radio()

    @contextmanager
    def connect(_):
        yield iface

    monkeypatch.setattr(manager, "apply_operations", lambda *_: None)
    clock = iter([0, 1, 121])
    monkeypatch.setattr(manager.time, "monotonic", lambda: next(clock))
    with pytest.raises(manager.ProfileError, match="verification failed"):
        manager.reconcile(
            profile(), "apply", tmp_path / "state", connect, sleep=lambda _: None
        )


def test_state_lock_and_permissions(tmp_path):
    state = tmp_path / "state"
    with manager.locked_state(state):
        with pytest.raises(manager.ProfileError, match="Another operation"):
            with manager.locked_state(state):
                pass
    state.chmod(0o755)
    with pytest.raises(manager.ProfileError, match="0700"):
        with manager.locked_state(state):
            pass


def test_connection_accepts_sdk_success_without_return_value(monkeypatch):
    calls = []
    fake = SimpleNamespace(
        connect=lambda: calls.append("connect"),
        waitForConfig=lambda: calls.append("ready"),
        close=lambda: calls.append("close"),
    )
    monkeypatch.setattr(manager, "BoundedTCPInterface", lambda *a, **kw: fake)
    monkeypatch.setattr(manager.time, "sleep", lambda _: None)
    with manager.connection(profile()) as connected:
        assert connected is fake
    assert calls == ["connect", "ready", "close"]


def test_missing_writes_are_retried_and_verified(tmp_path, monkeypatch):
    iface = radio()
    writes = []

    @contextmanager
    def connect(_):
        yield iface

    def write(device, operations):
        writes.append(operations)
        device.myInfo.reboot_count += 1
        if len(writes) == 2:
            device.localNode.localConfig.device.CopyFrom(operations[0][2])

    monkeypatch.setattr(manager, "apply_operations", write)
    assert (
        manager.reconcile(
            profile(), "apply", tmp_path / "state", connect, sleep=lambda _: None
        )
        == 0
    )
    assert len(writes) == 2
    assert len(list((tmp_path / "state").glob("before-*.json"))) == 2


def test_permanent_drift_fails_after_three_passes(tmp_path, monkeypatch):
    iface = radio()
    writes = []

    @contextmanager
    def connect(_):
        yield iface

    def write(device, operations):
        writes.append(operations)
        device.myInfo.reboot_count += 1

    monkeypatch.setattr(manager, "apply_operations", write)
    with pytest.raises(manager.ProfileError, match="three passes: config.device.role"):
        manager.reconcile(
            profile(), "apply", tmp_path / "state", connect, sleep=lambda _: None
        )
    assert len(writes) == 3


def test_role_is_written_first_and_owner_last():
    iface = radio()
    wanted = profile(
        {
            "owner": {"is_unmessagable": False, "short_name": "OFF"},
            "config": {"bluetooth": {"enabled": True}, "device": {"role": "CLIENT"}},
            "moduleConfig": {"telemetry": {"device_update_interval": 3600}},
        }
    )
    manager.validate_settings(wanted["settings"])
    operations, _ = manager.plan(iface, wanted)
    assert operations[0][:2] == ("config", "device")
    assert operations[-1][0] == "owner"
    assert operations[-1][2]["is_unmessagable"] is False


def test_matching_ram_before_reboot_is_not_success(tmp_path, monkeypatch):
    iface = radio()
    connects, writes = [], []

    @contextmanager
    def connect(_):
        connects.append(1)
        if len(connects) == 3:
            # The first reboot lost the RAM-only change.
            iface.myInfo.reboot_count = 2
            iface.localNode.localConfig.device.role = 2  # ROUTER
        elif len(connects) == 4:
            iface.myInfo.reboot_count = 3
        yield iface

    def write(device, operations):
        writes.append(operations)
        device.localNode.localConfig.device.CopyFrom(operations[0][2])

    monkeypatch.setattr(manager, "apply_operations", write)
    assert (
        manager.reconcile(
            profile(), "apply", tmp_path / "state", connect, sleep=lambda _: None
        )
        == 0
    )
    assert len(connects) == 4
    assert len(writes) == 2
    assert iface.myInfo.reboot_count == 3


def test_missing_reboot_counter_refuses_writes(tmp_path, monkeypatch):
    iface = radio()
    iface.myInfo.reboot_count = 0

    @contextmanager
    def connect(_):
        yield iface

    monkeypatch.setattr(
        manager, "apply_operations", lambda *_: pytest.fail("Unexpected write")
    )
    with pytest.raises(manager.ProfileError, match="reboot counter"):
        manager.reconcile(profile(), "apply", tmp_path / "state", connect)
    assert not list((tmp_path / "state").glob("before-*.json"))


def test_explicit_reboot_follows_commit(monkeypatch):
    iface = radio()
    events = []
    node = iface.localNode
    node.beginSettingsTransaction = lambda: events.append("begin")
    node.writeConfig = lambda _: events.append("write")
    node.commitSettingsTransaction = lambda: events.append("commit")
    node.reboot = lambda seconds: events.append(("reboot", seconds))
    monkeypatch.setattr(manager.time, "sleep", lambda _: None)
    manager.apply_operations(iface, manager.plan(iface, profile())[0])
    assert events == ["begin", "write", "commit", ("reboot", 2)]
