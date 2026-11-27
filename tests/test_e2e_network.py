"""End to end against `hubctl --sim serve`: network health, bindings, groups,
hub settings and the Zigbee2MQTT import on the real API server.

Skipped without `HUBCTL` (see test_e2e_hubctl.py); `HUBCTL_BACKEND=ezsp`
runs them on the EZSP backend's simulated NCP.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.zigbee_rs_hub import z2m_import
from custom_components.zigbee_rs_hub.const import CONF_PATH, DOMAIN
from custom_components.zigbee_rs_hub.hub import find_device
from homeassistant.const import ATTR_ENTITY_ID, CONF_HOST, CONF_PORT, CONF_TOKEN, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .test_e2e_hubctl import HUBCTL, SIM_COORDINATOR_IEEE, Hubctl, _free_port, _run, _wait
from .test_z2m_import import make_dir

pytestmark = pytest.mark.skipif(not HUBCTL, reason="HUBCTL is not set")

PLUG1 = "zb:5a52530000000001"
PLUG2 = "zb:5a52530000000002"
SENSOR = "zb:5a52530000000003"


async def _entry(hass: HomeAssistant, hub: Hubctl) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="hubctl",
        data={CONF_HOST: "127.0.0.1", CONF_PORT: hub.port, CONF_TOKEN: "e2e", CONF_PATH: "/api"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _hub_entity(hass: HomeAssistant, entry: MockConfigEntry, platform: str, key: str) -> str | None:
    return er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{entry.entry_id}_{key}")


def _ha_device(hass: HomeAssistant, entry: MockConfigEntry, dev: str) -> str:
    found = find_device(dr.async_get(hass), (DOMAIN, dev), entry.entry_id)
    assert found is not None, dev
    return found.id


async def _call(hass: HomeAssistant, service: str, data: dict) -> dict:
    return await hass.services.async_call(
        DOMAIN, service, data, blocking=True, return_response=True
    )


async def _interviewed(hass: HomeAssistant, *devices: str) -> None:
    """Until the simulated devices are interviewed (the EZSP backend takes longer)."""
    reg = er.async_get(hass)
    for dev in devices:
        uid = f"{dev}:1:{'temperature' if dev == SENSOR else 'switch'}"
        platform = "sensor" if dev == SENSOR else "switch"
        await _wait(
            hass, lambda u=uid, p=platform: reg.async_get_entity_id(p, DOMAIN, u) is not None
        )
        await _wait(
            hass, lambda u=f"{dev}:0:rssi": reg.async_get_entity_id("sensor", DOMAIN, u) is not None
        )


@pytest.fixture
async def sim_devices() -> Hubctl:
    hub = Hubctl(_free_port(), "--sim-devices")
    await hub.start(ready=True)
    yield hub
    await hub.stop()


async def test_e2e_health_and_settings(hass: HomeAssistant, sim_devices: Hubctl) -> None:
    entry = await _entry(hass, sim_devices)
    hub = entry.runtime_data
    await _interviewed(hass, PLUG1, PLUG2, SENSOR)
    # The EZSP backend's NCP session (and so `maintenance`) may come up later.
    await _wait(hass, lambda: _hub_entity(hass, entry, "switch", "mqtt_enabled") is not None)
    await _wait(hass, lambda: _hub_entity(hass, entry, "select", "channel_change") is not None)
    devices = _hub_entity(hass, entry, "sensor", "devices")
    await _wait(hass, lambda: hass.states.get(devices).state == "3")
    assert hass.states.get(_hub_entity(hass, entry, "sensor", "offline_devices")).state == "0"
    assert hass.states.get(_hub_entity(hass, entry, "sensor", "routers")).state == "3"
    # The hub's link quality and last seen: enabled; its RSSI stays disabled.
    reg = er.async_get(hass)
    lqi = reg.async_get(reg.async_get_entity_id("sensor", DOMAIN, f"{PLUG1}:0:linkquality"))
    assert lqi.disabled_by is None
    rssi = reg.async_get(reg.async_get_entity_id("sensor", DOMAIN, f"{PLUG1}:0:rssi"))
    assert rssi.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert reg.async_get_entity_id("sensor", DOMAIN, f"{PLUG1}_network_address")

    # The network map (the simulated routers do not answer Mgmt_Lqi_req).
    result = await _call(hass, "scan_topology", {"routes": False})
    nodes = {n["nwk"]: n for n in result["nodes"]}
    assert nodes[0]["type"] == "coordinator"
    assert {n.get("device") for n in result["nodes"]} >= {PLUG1, PLUG2, SENSOR}
    assert hub.topology.node_of_device(PLUG1)["type"] == "router"
    assert await _call(hass, "scan_topology", {"refresh": False}) == result

    # TX power.
    tx = _hub_entity(hass, entry, "number", "tx_power")
    await hass.services.async_call(
        "number", "set_value", {ATTR_ENTITY_ID: tx, "value": 5}, blocking=True
    )
    await _wait(hass, lambda: hass.states.get(tx).state == "5")

    # MQTT bridge: refused without a broker, then on (nothing listens there).
    switch = _hub_entity(hass, entry, "switch", "mqtt_enabled")
    status = _hub_entity(hass, entry, "sensor", "mqtt_status")
    assert hass.states.get(status).state == "disabled"
    with pytest.raises(HomeAssistantError, match="host"):
        await hass.services.async_call("switch", "turn_on", {ATTR_ENTITY_ID: switch}, blocking=True)
    # Broker and credentials in the options flow (the hub keeps the password).
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "mqtt"}
    )
    port = _free_port()
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"],
        {
            "enabled": False,
            "host": "127.0.0.1",
            "port": port,
            "username": "ha",
            "password": "pw",
            "base_topic": "zigbee2mqtt",
        },
    )
    assert flow["type"] == "create_entry", flow
    cfg = await hub.async_request("mqtt/config/get")
    assert (cfg["host"], cfg["port"], cfg["username"], cfg["password_set"]) == (
        "127.0.0.1",
        port,
        "ha",
        True,
    )
    assert "password" not in cfg
    await hass.services.async_call("switch", "turn_on", {ATTR_ENTITY_ID: switch}, blocking=True)
    assert hass.states.get(switch).state == STATE_ON
    for _ in range(50):
        await hub.settings.async_refresh_mqtt_status()
        if hass.states.get(status).state not in ("disabled", "waiting_for_hub"):
            break
        await asyncio.sleep(0.1)
    assert hass.states.get(status).state in ("connecting", "retrying", "low_memory")
    await hass.services.async_call("switch", "turn_off", {ATTR_ENTITY_ID: switch}, blocking=True)

    # NCP log level: ZRHP NCPs of hubs with network/ncp_log only.
    if ncp_log := _hub_entity(hass, entry, "select", "ncp_log_level"):
        await hass.services.async_call(
            "select", "select_option", {ATTR_ENTITY_ID: ncp_log, "option": "debug"}, blocking=True
        )
        level = await hub.async_request("network/ncp_log")
        assert level["level"] == 4

    # Channel change: staged by the select, applied by the admin service.
    select = _hub_entity(hass, entry, "select", "channel_change")
    channel = _hub_entity(hass, entry, "sensor", "channel")
    old = int(hass.states.get(channel).state)
    new = 25 if old != 25 else 20
    await hass.services.async_call(
        "select", "select_option", {ATTR_ENTITY_ID: select, "option": str(new)}, blocking=True
    )
    assert int(hass.states.get(channel).state) == old
    moved = await _call(hass, "change_channel", {"confirm": True})
    assert moved["channel"] == new
    await _wait(hass, lambda: hass.states.get(channel).state == str(new))
    info = await hub.async_request("network/info")
    assert info["channel"] == new

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_e2e_bindings_and_groups(hass: HomeAssistant, sim_devices: Hubctl) -> None:
    entry = await _entry(hass, sim_devices)
    hub = entry.runtime_data
    await _interviewed(hass, PLUG1, PLUG2)
    plug1 = _ha_device(hass, entry, PLUG1)
    plug2 = _ha_device(hass, entry, PLUG2)

    bound = await _call(
        hass, "bind", {"device_id": plug1, "clusters": ["on_off"], "target_device_id": plug2}
    )
    assert bound["status"] == "ok", bound
    assert bound["endpoint"] == 1
    assert bound["results"][0]["target"] == {"device": PLUG2, "endpoint": 1}
    read = await _call(hass, "read_bindings", {"device_id": plug1})
    assert read["reading"] is False, read
    assert {"cluster": 6, "endpoint": 1, "target": {"device": PLUG2, "endpoint": 1}} in read[
        "table"
    ]
    unbound = await _call(
        hass,
        "unbind",
        {"device_id": plug1, "clusters": [6], "target_device_id": plug2, "target_endpoint": 1},
    )
    assert unbound["status"] == "ok", unbound
    assert unbound["results"][0]["state"] == "unbound"

    group = await _call(hass, "group_create", {"name": "Kitchen", "group_id": "0x0042"})
    assert group["id"] == 0x42
    # The simulated plugs have no Groups cluster.
    with pytest.raises(HomeAssistantError, match="Groups cluster"):
        await _call(hass, "group_add_member", {"group_id": 0x42, "device_id": plug1})
    bound = await _call(hass, "bind", {"device_id": plug1, "clusters": [6], "group_id": 0x42})
    assert bound["status"] == "ok", bound
    await hass.services.async_call(DOMAIN, "group_remove", {"group_id": "0x42"}, blocking=True)
    assert not [g for g in await hub.async_request("groups/list") if g["id"] == 0x42]

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_e2e_import_from_z2m(hass: HomeAssistant, tmp_path: Path) -> None:
    """Zigbee2MQTT's files read as hubctl reads them, previewed and imported."""
    data_dir = make_dir(tmp_path / "zigbee2mqtt", coordinator=SIM_COORDINATOR_IEEE)
    assert HUBCTL
    out = tmp_path / "doc.json"
    await _run(HUBCTL, "import", "z2m", str(data_dir), "--out", str(out))
    doc, _ = await hass.async_add_executor_job(z2m_import.read_z2m, str(data_dir))
    assert doc == json.loads(out.read_text())

    hub = Hubctl(_free_port())
    await hub.start(ready=True)
    try:
        entry = await _entry(hass, hub)
        data = {"path": str(data_dir)}
        preview = await _call(hass, "import_from_z2m", data)
        res = preview["result"]
        assert res["network"]["ext_pan_id"] == "a1b2c3d4e5f60718"
        assert res["network"]["coordinator"] == "same"
        assert res["summary"]["new"] == 3, preview
        rows = {r["ieee"]: r for r in preview["rows"]}
        assert rows["00000000017880ff"]["reason"] == "green_power"
        applied = await _call(
            hass, "import_from_z2m", {**data, "apply": True, "force": True, "confirm": True}
        )
        res = applied["result"]
        assert res["network"]["action"] == "restored", res
        assert res["devices"]["new"] == 3 and not res["coordinator_changed"]
        reg = dr.async_get(hass)
        await _wait(hass, lambda: find_device(reg, (DOMAIN, "zb:0017880100f1e2d3"), entry.entry_id))
        light = find_device(reg, (DOMAIN, "zb:0017880100f1e2d3"), entry.entry_id)
        assert light.name == "Hall light"
        assert light.model == "LCT015"
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
    finally:
        await hub.stop()
