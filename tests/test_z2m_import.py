"""Import from Zigbee2MQTT: its files → the hub's import document, and the service."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, MockUser

from custom_components.zigbee_rs_hub import z2m_import
from custom_components.zigbee_rs_hub.const import DOMAIN
from custom_components.zigbee_rs_hub.hub import find_device
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import ServiceValidationError, Unauthorized
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er

from .fake_hub import FakeHub

# zigbee-herdsman's database.db as in hubctl's tests (tools/hubctl/src/import/z2m.rs):
# the coordinator, a router with a colour light's cache, a sleepy sensor with an
# older `attrs` cache and no interview state, a Green Power switch, a failed
# interview (endpoints, no model), a broken line, a group.
DB = "\n".join(
    json.dumps(e) if not isinstance(e, str) else e
    for e in [
        {
            "id": 1,
            "type": "Coordinator",
            "ieeeAddr": "0x00124b00c0ffee01",
            "nwkAddr": 0,
            "manufId": 0,
            "epList": [1, 242],
            "endpoints": {
                "1": {
                    "profId": 260,
                    "epId": 1,
                    "devId": 5,
                    "inClusterList": [],
                    "outClusterList": [],
                    "clusters": {},
                    "binds": [],
                    "configuredReportings": [],
                    "meta": {},
                }
            },
            "interviewCompleted": True,
            "meta": {},
        },
        {
            "id": 2,
            "type": "Router",
            "ieeeAddr": "0x0017880100f1e2d3",
            "nwkAddr": 31248,
            "manufId": 4107,
            "manufName": "Signify Netherlands B.V.",
            "powerSource": "Mains (single phase)",
            "modelId": "LCT015",
            "epList": [11, 242],
            "endpoints": {
                "11": {
                    "profId": 260,
                    "epId": 11,
                    "devId": 269,
                    "inClusterList": [0, 3, 4, 5, 6, 8, 768, 4096],
                    "outClusterList": [25],
                    "clusters": {
                        "genBasic": {"attributes": {"modelId": "LCT015"}},
                        "lightingColorCtrl": {
                            "attributes": {
                                "colorCapabilities": 31,
                                "colorTempPhysicalMin": 153,
                                "colorTempPhysicalMax": 500,
                                "currentX": 1000,
                            }
                        },
                    },
                    "binds": [],
                    "configuredReportings": [],
                    "meta": {},
                },
                "242": {
                    "profId": 41440,
                    "epId": 242,
                    "devId": 97,
                    "inClusterList": [],
                    "outClusterList": [33],
                    "clusters": {},
                    "binds": [],
                    "configuredReportings": [],
                    "meta": {},
                },
            },
            "appVersion": 2,
            "stackVersion": 1,
            "hwVersion": 1,
            "dateCode": "20191218",
            "swBuildId": "1.50.2_r30933",
            "zclVersion": 1,
            "interviewCompleted": True,
            "interviewState": "SUCCESSFUL",
            "meta": {},
            "lastSeen": 1764000000000,
        },
        {
            "id": 3,
            "type": "EndDevice",
            "ieeeAddr": "0x00158d0003c4d5e6",
            "nwkAddr": 1202,
            "manufId": 4151,
            "manufName": "LUMI",
            "powerSource": "Battery",
            "modelId": "lumi.weather",
            "epList": [1],
            "endpoints": {
                "1": {
                    "profId": 260,
                    "epId": 1,
                    "devId": 24321,
                    "inClusterList": [0, 3, 65535, 1026, 1027, 1029],
                    "outClusterList": [0, 4, 65535],
                    "clusters": {
                        "genBasic": {"attrs": {"modelId": "lumi.weather"}},
                        "genPowerCfg": {"attrs": {"batteryVoltage": 30}},
                    },
                }
            },
            "appVersion": 3,
            "meta": {},
        },
        {
            "id": 4,
            "type": "GreenPower",
            "ieeeAddr": "0x00000000017880ff",
            "nwkAddr": 32767,
            "modelId": "GreenPower_2",
            "epList": [],
            "endpoints": {},
            "interviewCompleted": True,
            "meta": {},
            "gpSecurityKey": [1, 2, 3],
        },
        {
            "id": 5,
            "type": "EndDevice",
            "ieeeAddr": "0x00158d00aabbccdd",
            "nwkAddr": 4242,
            "epList": [1],
            "endpoints": {
                "1": {
                    "profId": 260,
                    "epId": 1,
                    "devId": 0,
                    "inClusterList": [0, 6],
                    "outClusterList": [],
                }
            },
            "interviewState": "FAILED",
            "meta": {},
        },
        "not json",
        {
            "id": 6,
            "type": "Group",
            "groupID": 4097,
            "members": [
                {"deviceIeeeAddr": "0x0017880100f1e2d3", "endpointID": 11},
                {"deviceIeeeAddr": "0x0000000000000bad", "endpointID": 1},
            ],
            "meta": {},
        },
    ]
)

BACKUP = {
    "metadata": {
        "format": "zigpy/open-coordinator-backup",
        "version": 1,
        "source": "zigbee-herdsman@3.2.0",
        "internal": {"date": "2026-11-20T10:00:00.000Z", "znpVersion": 2},
    },
    "stack_specific": {"zstack": {"tclk_seed": "77777777777777777777777777777777"}},
    "coordinator_ieee": "00124b00c0ffee01",
    "pan_id": "f3a",
    "extended_pan_id": "a1b2c3d4e5f60718",
    "nwk_update_id": 4,
    "security_level": 5,
    "channel": 20,
    "channel_mask": [20],
    "network_key": {
        "key": "8899aabbccddeeff0011223344556677",
        "sequence_number": 3,
        "frame_counter": 123456,
    },
    "devices": [
        {
            "nwk_address": "4b2",
            "ieee_address": "00158d0003c4d5e6",
            "link_key": {
                "key": "0123456789abcdef0123456789abcdef",
                "rx_counter": 34,
                "tx_counter": 12,
            },
        },
        {"nwk_address": "7a10", "ieee_address": "0017880100f1e2d3", "is_child": False},
    ],
}

CONFIGURATION = """homeassistant: true
mqtt:
  base_topic: zigbee2mqtt
  password: !secret mqtt_password
devices: devices.yaml
groups:
  '4097':
    friendly_name: Hall
  7:
    friendly_name: 7
"""
DEVICES_YAML = """'0x00158d0003c4d5e6':
  friendly_name: Kitchen/temperature
  retain: false
0x0017880100f1e2d3:
  friendly_name: Hall light
"""


def make_dir(path: Path, coordinator: str | None = None) -> Path:
    """A Zigbee2MQTT data directory; `coordinator`: the backup's coordinator IEEE."""
    path.mkdir(parents=True, exist_ok=True)
    (path / "database.db").write_text(DB)
    (path / "configuration.yaml").write_text(CONFIGURATION)
    (path / "devices.yaml").write_text(DEVICES_YAML)
    backup = json.loads(json.dumps(BACKUP))
    if coordinator:
        backup["coordinator_ieee"] = coordinator
    (path / "coordinator_backup.json").write_text(json.dumps(backup))
    return path


def test_database_entries_become_document_devices() -> None:
    names = z2m_import.Names()
    names.devices["0017880100f1e2d3"] = "Hall light"
    names.groups[4097] = "Hall"
    devices, groups, coordinator, bad = z2m_import.database(DB, names)
    assert (len(devices), len(groups), bad) == (4, 1, 1)
    assert coordinator == "00124b00c0ffee01"
    light = devices[0]
    assert light["ieee"] == "0017880100f1e2d3"
    assert (light["name"], light["type"], light["power_source"]) == ("Hall light", "router", 1)
    assert light["endpoints"][0] == {
        "id": 11,
        "profile": 260,
        "device_id": 269,
        "in": [0, 3, 4, 5, 6, 8, 768, 4096],
        "out": [25],
    }
    assert light["endpoints"][1]["id"] == 242, "the GP proxy endpoint, sorted after 11"
    assert light["attributes"] == [
        {"ep": 11, "cluster": 0x0300, "attr": 0x400A, "value": 31},
        {"ep": 11, "cluster": 0x0300, "attr": 0x400B, "value": 153},
        {"ep": 11, "cluster": 0x0300, "attr": 0x400C, "value": 500},
    ]
    assert (light["sw_build_id"], light["interviewed"], light["source_id"]) == (
        "1.50.2_r30933",
        True,
        2,
    )
    sensor = devices[1]
    assert (sensor["type"], sensor["power_source"]) == ("end_device", 3)
    assert "interviewed" not in sensor
    assert "name" not in sensor and "attributes" not in sensor
    assert devices[2]["type"] == "green_power"
    assert devices[3]["interviewed"] is False
    assert "model" not in devices[3]
    assert groups[0] == {
        "id": 4097,
        "name": "Hall",
        "members": [{"ieee": "0017880100f1e2d3", "ep": 11}, {"ieee": "0000000000000bad", "ep": 1}],
    }


def test_friendly_names_and_document(tmp_path: Path) -> None:
    make_dir(tmp_path)
    names = z2m_import.Names()
    z2m_import.names(str(tmp_path / "configuration.yaml"), names)
    assert names.devices == {
        "00158d0003c4d5e6": "Kitchen/temperature",
        # An unquoted 0x… key is a YAML integer.
        "0017880100f1e2d3": "Hall light",
    }
    assert names.groups == {4097: "Hall", 7: "7"}
    # Inline devices.
    (tmp_path / "inline.yaml").write_text(
        "devices:\n  '0x00158d00aabbccdd':\n    friendly_name: Plug\n"
    )
    inline = z2m_import.Names()
    z2m_import.names(str(tmp_path / "inline.yaml"), inline)
    assert inline.devices == {"00158d00aabbccdd": "Plug"}

    doc, skipped = z2m_import.read_z2m(str(tmp_path))
    assert skipped == 1
    assert doc["format"] == "zigbee-rs-hub/import"
    assert doc["source"] == {"kind": "z2m", "name": "Zigbee2MQTT (zigbee-herdsman@3.2.0)"}
    assert doc["network"]["extended_pan_id"] == "a1b2c3d4e5f60718"
    assert doc["devices"][1]["name"] == "Kitchen/temperature"
    assert doc["groups"][0]["name"] == "Hall"

    (tmp_path / "coordinator_backup.json").write_text(json.dumps({"metadata": {"format": "x"}}))
    with pytest.raises(ValueError, match="not an Open Coordinator Backup"):
        z2m_import.read_z2m(str(tmp_path))


async def test_import_preview_and_apply(
    hass: HomeAssistant, fake_hub: FakeHub, setup_entry: MockConfigEntry, tmp_path: Path
) -> None:
    data_dir = make_dir(tmp_path / "zigbee2mqtt")
    # Zigbee2MQTT's MQTT devices in HA: an area, and a name set in HA.
    mqtt = MockConfigEntry(domain="mqtt")
    mqtt.add_to_hass(hass)
    reg = dr.async_get(hass)
    kitchen = ar.async_get(hass).async_create("Kitchen")
    sensor = reg.async_get_or_create(
        config_entry_id=mqtt.entry_id, identifiers={("mqtt", "zigbee2mqtt_0x00158d0003c4d5e6")}
    )
    reg.async_update_device(sensor.id, area_id=kitchen.id, name_by_user="Kitchen climate")

    preview = await hass.services.async_call(
        DOMAIN,
        "import_from_z2m",
        {"path": str(data_dir)},
        blocking=True,
        return_response=True,
    )
    first = fake_hub.calls("network/import/preview")[0]
    assert first["document"]["source"]["kind"] == "z2m"
    assert first["document"]["network"]["devices"] == []
    assert preview["result"]["summary"] == {"new": 3, "replace": 0, "keep": 0, "skip": 1}
    assert preview["skipped_lines"] == 1
    rows = {r["ieee"]: r for r in preview["rows"]}
    assert rows["0017880100f1e2d3"]["name"] == "Hall light"
    assert not fake_hub.calls("network/import")

    applied = await hass.services.async_call(
        DOMAIN,
        "import_from_z2m",
        {"path": str(data_dir), "apply": True, "force": True},
        blocking=True,
        return_response=True,
    )
    assert fake_hub.calls("network/import")[0]["force"] is True
    assert "backup" not in applied["result"]
    assert applied["areas"] == 1
    assert applied["names"] == 1
    dev = find_device(reg, (DOMAIN, "zb:00158d0003c4d5e6"), setup_entry.entry_id)
    assert dev.area_id == kitchen.id
    assert dev.name_by_user == "Kitchen climate"
    # The hub's name is Zigbee2MQTT's friendly name.
    assert dev.name == "Kitchen/temperature"


async def test_import_refused_while_z2m_runs(
    hass: HomeAssistant, fake_hub: FakeHub, setup_entry: MockConfigEntry, tmp_path: Path
) -> None:
    data_dir = str(make_dir(tmp_path / "z2m"))
    mqtt = MockConfigEntry(domain="mqtt")
    mqtt.add_to_hass(hass)
    reg = dr.async_get(hass)
    ents = er.async_get(hass)

    def bridge(ident: str, manufacturer: str, state: str) -> None:
        dev = reg.async_get_or_create(
            config_entry_id=mqtt.entry_id,
            identifiers={("mqtt", ident)},
            manufacturer=manufacturer,
            model="Bridge",
            name=f"{manufacturer} Bridge",
        )
        ent = ents.async_get_or_create(
            "binary_sensor",
            "mqtt",
            f"{ident}_connection_state",
            device_id=dev.id,
            config_entry=mqtt,
            original_device_class="connectivity",
        )
        hass.states.async_set(ent.entity_id, state)

    # The hub's own MQTT bridge does not count.
    bridge("zigbee2mqtt_bridge_0x00124b0001020304", "zigbee-rs", "on")
    bridge("zigbee2mqtt_bridge_0x00124b00c0ffee01", "Zigbee2MQTT", "off")
    data = {"path": data_dir, "apply": True, "force": True}
    await hass.services.async_call(
        DOMAIN, "import_from_z2m", data, blocking=True, return_response=True
    )
    bridge("zigbee2mqtt_bridge_0x00124b00c0ffee01", "Zigbee2MQTT", "on")
    with pytest.raises(ServiceValidationError, match=r"Zigbee2MQTT may be running.*bridge"):
        await hass.services.async_call(
            DOMAIN, "import_from_z2m", data, blocking=True, return_response=True
        )
    # A preview is fine.
    await hass.services.async_call(
        DOMAIN, "import_from_z2m", {"path": data_dir}, blocking=True, return_response=True
    )


ADDON = "45df7312_zigbee2mqtt"


def supervisor(
    monkeypatch: pytest.MonkeyPatch,
    *,
    addons_list: list | None = None,
    supervisor_info: dict | None = None,
    addons_info: dict | None = None,
    new_api: bool = True,
) -> None:
    """A Supervisor installation; `new_api`: HA 2026.4+ with `get_addons_list`."""
    from homeassistant.components import hassio
    from homeassistant.components.hassio import HassioNotReadyError

    def value(v):
        def get(_hass):
            if v is None:
                raise HassioNotReadyError
            return v

        return get

    monkeypatch.setattr(z2m_import, "is_hassio", lambda _hass: True)
    if new_api:
        monkeypatch.setattr(hassio, "get_addons_list", value(addons_list))
    else:
        monkeypatch.delattr(hassio, "get_addons_list")
    monkeypatch.setattr(hassio, "get_supervisor_info", value(supervisor_info))
    monkeypatch.setattr(hassio, "get_addons_info", value(addons_info))


async def _apply(hass: HomeAssistant, data_dir: str) -> dict:
    return await hass.services.async_call(
        DOMAIN,
        "import_from_z2m",
        {"path": data_dir, "apply": True, "force": True},
        blocking=True,
        return_response=True,
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        # HA 2026.4 and newer.
        {"addons_list": [{"slug": ADDON, "name": "Zigbee2MQTT", "state": "started"}]},
        # Older: the Supervisor info's add-on list.
        {
            "new_api": False,
            "supervisor_info": {
                "addons": [{"slug": ADDON, "name": "Zigbee2MQTT", "state": "started"}]
            },
        },
        # Older, without the Supervisor info: the add-on infos (HA 2025.1).
        {
            "new_api": False,
            "addons_info": {ADDON: {"name": "Zigbee2MQTT", "state": "started", "options": {}}},
        },
    ],
)
async def test_import_refused_while_the_addon_runs(
    hass: HomeAssistant,
    fake_hub: FakeHub,
    setup_entry: MockConfigEntry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kwargs: dict,
) -> None:
    data_dir = str(make_dir(tmp_path / "z2m"))
    supervisor(monkeypatch, **kwargs)
    with pytest.raises(ServiceValidationError, match="Zigbee2MQTT add-on is running"):
        await _apply(hass, data_dir)
    assert not fake_hub.calls("network/import")


@pytest.mark.parametrize("new_api", [True, False])
async def test_import_refused_when_addon_states_are_unknown(
    hass: HomeAssistant,
    fake_hub: FakeHub,
    setup_entry: MockConfigEntry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    new_api: bool,
) -> None:
    """A Supervisor installation whose add-on states cannot be read: refused."""
    data_dir = str(make_dir(tmp_path / "z2m"))
    supervisor(monkeypatch, new_api=new_api)
    with pytest.raises(ServiceValidationError, match="cannot be read"):
        await _apply(hass, data_dir)
    assert not fake_hub.calls("network/import")
    # A preview is fine.
    await hass.services.async_call(
        DOMAIN, "import_from_z2m", {"path": data_dir}, blocking=True, return_response=True
    )


@pytest.mark.parametrize("new_api", [True, False])
async def test_import_with_the_addon_stopped(
    hass: HomeAssistant,
    fake_hub: FakeHub,
    setup_entry: MockConfigEntry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    new_api: bool,
) -> None:
    data_dir = str(make_dir(tmp_path / "z2m"))
    stopped = [{"slug": ADDON, "name": "Zigbee2MQTT", "state": "stopped"}]
    supervisor(
        monkeypatch,
        new_api=new_api,
        addons_list=stopped,
        supervisor_info={"addons": stopped},
        addons_info={ADDON: {"name": "Zigbee2MQTT", "state": "started", "options": {}}},
    )
    result = await _apply(hass, data_dir)
    assert result["result"]["network"]["action"] == "restored"


async def test_data_directory_of_the_addon(
    hass: HomeAssistant, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hass.config.config_dir = str(tmp_path / "config")
    monkeypatch.setattr(
        z2m_import, "DEFAULT_DIRS", ("zigbee2mqtt", str(tmp_path / "share" / "zigbee2mqtt"))
    )
    # Data in both default places (the add-on moved): its data_path decides.
    make_dir(tmp_path / "config" / "zigbee2mqtt")
    make_dir(tmp_path / "share" / "zigbee2mqtt")
    info = {
        ADDON: {
            "name": "Zigbee2MQTT",
            "state": "stopped",
            "options": {"data_path": "/config/zigbee2mqtt"},
        }
    }
    supervisor(monkeypatch, new_api=False, addons_info=info)
    assert await z2m_import.async_data_dir(hass, None) == str(tmp_path / "config" / "zigbee2mqtt")
    info[ADDON]["options"]["data_path"] = "/share/z2m"
    assert await z2m_import.async_data_dir(hass, None) == "/share/z2m"
    info[ADDON]["options"]["data_path"] = "/addon_config/zigbee2mqtt"
    with pytest.raises(ServiceValidationError, match="cannot read"):
        await z2m_import.async_data_dir(hass, None)
    # A path given wins.
    assert await z2m_import.async_data_dir(hass, "/data") == "/data"


async def test_default_data_directories(
    hass: HomeAssistant, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hass.config.config_dir = str(tmp_path / "config")
    share = tmp_path / "share" / "zigbee2mqtt"
    monkeypatch.setattr(z2m_import, "DEFAULT_DIRS", ("zigbee2mqtt", str(share)))
    with pytest.raises(ServiceValidationError, match="no Zigbee2MQTT database"):
        await z2m_import.async_data_dir(hass, None)
    make_dir(share)
    assert await z2m_import.async_data_dir(hass, None) == str(share)
    # Both: refused, the newer one first.
    current = make_dir(tmp_path / "config" / "zigbee2mqtt")
    os.utime(share / "database.db", (1_700_000_000, 1_700_000_000))
    os.utime(share / "coordinator_backup.json", (1_700_000_000, 1_700_000_000))
    with pytest.raises(ServiceValidationError, match="more than one place") as err:
        await z2m_import.async_data_dir(hass, None)
    assert str(err.value).index(str(current)) < str(err.value).index(str(share))
    # No Zigbee2MQTT add-on on a Supervisor installation: the same.
    supervisor(monkeypatch, addons_list=[])
    with pytest.raises(ServiceValidationError, match="more than one place"):
        await z2m_import.async_data_dir(hass, None)


async def test_import_needs_files_and_an_admin(
    hass: HomeAssistant,
    fake_hub: FakeHub,
    setup_entry: MockConfigEntry,
    tmp_path: Path,
    hass_read_only_user: MockUser,
) -> None:
    with pytest.raises(ServiceValidationError, match="no Zigbee2MQTT database"):
        await hass.services.async_call(
            DOMAIN, "import_from_z2m", {"path": str(tmp_path)}, blocking=True, return_response=True
        )
    data_dir = make_dir(tmp_path / "z2m")
    (data_dir / "coordinator_backup.json").unlink()
    with pytest.raises(ServiceValidationError, match=r"no coordinator_backup\.json"):
        await hass.services.async_call(
            DOMAIN,
            "import_from_z2m",
            {"path": str(data_dir)},
            blocking=True,
            return_response=True,
        )
    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            DOMAIN,
            "import_from_z2m",
            {"path": str(data_dir)},
            blocking=True,
            return_response=True,
            context=Context(user_id=hass_read_only_user.id),
        )
