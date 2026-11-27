"""Network health: hub counts, per-device diagnostics, the network map and its service."""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.zigbee_rs_hub.const import DOMAIN
from custom_components.zigbee_rs_hub.diagnostics import async_get_config_entry_diagnostics
from custom_components.zigbee_rs_hub.hub import find_device
from homeassistant.const import STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .fake_hub import LIGHT_DEV, REMOTE_DEV, SENSOR_DEV, device
from .fake_network import NWK, NetworkFakeHub
from .test_init import wait_for


@pytest.fixture
async def fake_hub() -> AsyncGenerator[NetworkFakeHub]:
    """The fake hub with the maintenance API."""
    hub = NetworkFakeHub()
    await hub.start()
    yield hub
    await hub.stop()


def hub_state(hass: HomeAssistant, entry: MockConfigEntry, platform: str, key: str):
    entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{entry.entry_id}_{key}")
    assert entity_id is not None, key
    return hass.states.get(entity_id)


async def enable(hass: HomeAssistant, entry: MockConfigEntry, unique_ids: list[str]) -> None:
    """Enable disabled-by-default sensors and reload the entry."""
    reg = er.async_get(hass)
    for uid in unique_ids:
        entity_id = reg.async_get_entity_id("sensor", DOMAIN, uid)
        assert entity_id is not None, uid
        assert reg.async_get(entity_id).disabled_by is er.RegistryEntryDisabler.INTEGRATION
        reg.async_update_entity(entity_id, disabled_by=None)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()


async def test_device_counts(
    hass: HomeAssistant, fake_hub: NetworkFakeHub, setup_entry: MockConfigEntry
) -> None:
    assert hub_state(hass, setup_entry, "sensor", "devices").state == "3"
    assert hub_state(hass, setup_entry, "sensor", "devices").attributes["zigbee"] == 3
    assert hub_state(hass, setup_entry, "sensor", "offline_devices").state == "0"
    # No map yet: mains devices count as routers.
    assert hub_state(hass, setup_entry, "sensor", "routers").state == "1"
    assert hub_state(hass, setup_entry, "sensor", "end_devices").state == "2"

    dev = dict(fake_hub.devices[SENSOR_DEV], available=False)
    await fake_hub.push("devices", {"topic": "device_updated", "device": dev})
    offline = lambda: hub_state(hass, setup_entry, "sensor", "offline_devices")  # noqa: E731
    await wait_for(hass, lambda: offline().state == "1")
    assert offline().attributes["devices"] == ["Motion sensor"]

    gp = device("gp:01020304", "GP", "PTM 215Z", ieee=None, src_id="01020304")
    await fake_hub.push("devices", {"topic": "device_added", "device": gp})
    await wait_for(hass, lambda: hub_state(hass, setup_entry, "sensor", "devices").state == "4")
    assert hub_state(hass, setup_entry, "sensor", "devices").attributes["green_power"] == 1
    assert hub_state(hass, setup_entry, "sensor", "routers").state == "1"

    await fake_hub.push("devices", {"topic": "device_removed", "id": SENSOR_DEV})
    await wait_for(hass, lambda: offline().state == "0")
    assert hub_state(hass, setup_entry, "sensor", "devices").state == "3"


async def test_device_diagnostic_sensors(
    hass: HomeAssistant, fake_hub: NetworkFakeHub, setup_entry: MockConfigEntry
) -> None:
    uids = [
        f"{SENSOR_DEV}_network_address",
        f"{SENSOR_DEV}_interview",
        f"{SENSOR_DEV}_parent",
        f"{SENSOR_DEV}_interview_resumes",
    ]
    fake_hub.devices[SENSOR_DEV]["interview"] = "imported"
    await enable(hass, setup_entry, uids)
    reg = er.async_get(hass)
    ids = [reg.async_get_entity_id("sensor", DOMAIN, u) for u in uids]
    nwk, interview, parent, resumes = (hass.states.get(i) for i in ids)
    assert nwk.entity_id == "sensor.motion_sensor_network_address"
    assert nwk.state == "0x1202"
    assert interview.state == "imported"
    assert parent.state == STATE_UNKNOWN
    # devices/diagnostics without `interview_resumes` (an older hub): unknown.
    await wait_for(hass, lambda: fake_hub.calls("devices/diagnostics"))
    assert hass.states.get(ids[3]).state == STATE_UNKNOWN
    assert resumes.attributes["friendly_name"] == "Motion sensor Interview resumes"

    dev = dict(fake_hub.devices[SENSOR_DEV], nwk=0x0BAD, interview="complete")
    await fake_hub.push("devices", {"topic": "device_updated", "device": dev})
    await wait_for(hass, lambda: hass.states.get(ids[0]).state == "0x0BAD")
    assert hass.states.get(ids[1]).state == "complete"

    # A device that joins later gets them too (disabled).
    new = device("zb:0011223344556677", "Sonoff", "SNZB-02", nwk=0x2222)
    await fake_hub.push("devices", {"topic": "device_added", "device": new})
    await wait_for(
        hass, lambda: reg.async_get_entity_id("sensor", DOMAIN, f"{new['id']}_parent") is not None
    )
    # Group devices have none.
    assert reg.async_get_entity_id("sensor", DOMAIN, "grp:0001_parent") is None


async def test_scan_topology_and_parents(
    hass: HomeAssistant, fake_hub: NetworkFakeHub, setup_entry: MockConfigEntry
) -> None:
    await enable(hass, setup_entry, [f"{REMOTE_DEV}_parent", f"{SENSOR_DEV}_parent"])
    reg = er.async_get(hass)
    remote = reg.async_get_entity_id("sensor", DOMAIN, f"{REMOTE_DEV}_parent")
    sensor = reg.async_get_entity_id("sensor", DOMAIN, f"{SENSOR_DEV}_parent")
    assert hass.states.get(remote).state == STATE_UNKNOWN
    # The user's name of the light in HA names the parent.
    light = find_device(dr.async_get(hass), (DOMAIN, LIGHT_DEV), setup_entry.entry_id)
    dr.async_get(hass).async_update_device(light.id, name_by_user="Hall light")

    result = await hass.services.async_call(
        DOMAIN, "scan_topology", {}, blocking=True, return_response=True
    )
    assert {n["nwk"] for n in result["nodes"]} == {0, *NWK.values()}
    assert fake_hub.calls("network/topology")[-1] == {
        "id": fake_hub.calls("network/topology")[-1]["id"],
        "type": "network/topology",
        "refresh": True,
        "routes": True,
    }
    await wait_for(hass, lambda: hass.states.get(remote).state == "Hall light")
    attrs = hass.states.get(remote).attributes
    assert attrs["parent_nwk"] == "0x7A10"
    assert attrs["lqi"] == 120
    assert attrs["depth"] == 2
    assert attrs["route_next_hop"] == "Hall light"
    assert hass.states.get(sensor).state == "Coordinator"
    # The map's node types count routers and end devices.
    routers = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{setup_entry.entry_id}_routers"
    )
    assert hass.states.get(routers).state == "1"

    diag = await async_get_config_entry_diagnostics(hass, setup_entry)
    assert diag["topology"]["nodes"] == result["nodes"]

    # The last map without a scan.
    last = await hass.services.async_call(
        DOMAIN, "scan_topology", {"refresh": False}, blocking=True, return_response=True
    )
    assert last == result

    # A failed scan.
    fake_hub.scan_error = {"code": "busy", "message": "energy scan (job 3) is running"}
    with pytest.raises(HomeAssistantError, match="busy"):
        await hass.services.async_call(
            DOMAIN, "scan_topology", {}, blocking=True, return_response=True
        )


async def test_foreign_scan_and_reconnect_load_the_map(
    hass: HomeAssistant, fake_hub: NetworkFakeHub, setup_entry: MockConfigEntry
) -> None:
    hub = setup_entry.runtime_data
    assert hub.topology.last is None
    await fake_hub.finish_foreign_scan()
    await wait_for(hass, lambda: hub.topology.last is not None)
    assert hub.topology.parent(REMOTE_DEV)["nwk"] == NWK[LIGHT_DEV]

    fake_hub.next_map = {"time": 5000.0, "nodes": [], "links": [], "routes": []}
    fake_hub.topology = fake_hub.next_map
    await fake_hub.drop()
    await wait_for(hass, lambda: hub.topology.last == fake_hub.next_map, timeout=10)
    assert hub.topology.parent(REMOTE_DEV) is None


async def test_hub_without_maps(
    hass: HomeAssistant, fake_hub: NetworkFakeHub, config_entry
) -> None:
    fake_hub.maintenance = []
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    with pytest.raises(HomeAssistantError, match="no network map"):
        await hass.services.async_call(
            DOMAIN, "scan_topology", {}, blocking=True, return_response=True
        )
    assert not fake_hub.calls("network/topology")
    await hass.config_entries.async_unload(config_entry.entry_id)
