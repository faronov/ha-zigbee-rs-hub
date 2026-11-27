"""Bindings and groups from HA (device-support 14.9), sleepy devices queued."""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.zigbee_rs_hub.bindings import cluster_ids, group_id
from custom_components.zigbee_rs_hub.const import DOMAIN
from custom_components.zigbee_rs_hub.hub import find_device
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr

from .fake_hub import LIGHT_DEV, REMOTE_DEV, SENSOR_DEV
from .fake_network import NetworkFakeHub


@pytest.fixture
async def fake_hub() -> AsyncGenerator[NetworkFakeHub]:
    """The fake hub with groups and bindings."""
    hub = NetworkFakeHub()
    await hub.start()
    yield hub
    await hub.stop()


def ha_id(hass: HomeAssistant, entry: MockConfigEntry, dev: str) -> str:
    found = find_device(dr.async_get(hass), (DOMAIN, dev), entry.entry_id)
    assert found is not None
    return found.id


async def call(hass: HomeAssistant, service: str, data: dict) -> dict:
    return await hass.services.async_call(
        DOMAIN, service, data, blocking=True, return_response=True
    )


def test_cluster_and_group_ids() -> None:
    assert cluster_ids(["on_off", "0x0008", 768, "6"]) == [6, 8, 768]
    assert cluster_ids("on_off, level_control") == [6, 8]
    assert group_id("0x1001") == 0x1001
    assert group_id(7) == 7
    for bad in ("dimmer", "0x10000", -1):
        with pytest.raises(ServiceValidationError):
            cluster_ids([bad])
    for bad in (0, "0xFFF8", "kitchen"):
        with pytest.raises(ServiceValidationError):
            group_id(bad)


async def test_bind_a_mains_device(
    hass: HomeAssistant, fake_hub: NetworkFakeHub, setup_entry: MockConfigEntry
) -> None:
    # The light's switch endpoint to its light endpoint (both mains).
    fake_hub.sleepy.clear()
    res = await call(
        hass,
        "bind",
        {
            "device_id": ha_id(hass, setup_entry, LIGHT_DEV),
            "endpoint": 12,
            "clusters": ["on_off"],
            "target_device_id": ha_id(hass, setup_entry, LIGHT_DEV),
        },
    )
    req = fake_hub.calls("bindings/create")[0]
    # The target endpoint serving On/Off.
    assert req["target"] == {"device": LIGHT_DEV, "endpoint": 11}
    assert req["clusters"] == [6]
    assert res["status"] == "ok"
    assert res["results"] == [
        {
            "endpoint": 12,
            "cluster": 6,
            "target": {"device": LIGHT_DEV, "endpoint": 11},
            "state": "bound",
            "error": None,
        }
    ]
    assert res["binding"]["table"][0]["cluster"] == 6

    res = await call(
        hass,
        "unbind",
        {
            "device_id": ha_id(hass, setup_entry, LIGHT_DEV),
            "endpoint": 12,
            "clusters": [6],
            "target_device_id": ha_id(hass, setup_entry, LIGHT_DEV),
            "target_endpoint": 11,
        },
    )
    assert res["status"] == "ok"
    assert res["results"][0]["state"] == "unbound"
    assert fake_hub.bindings[LIGHT_DEV]["table"] == []


async def test_bind_a_sleepy_remote_to_a_group(
    hass: HomeAssistant, fake_hub: NetworkFakeHub, setup_entry: MockConfigEntry
) -> None:
    remote = ha_id(hass, setup_entry, REMOTE_DEV)
    res = await call(
        hass,
        "bind",
        {"device_id": remote, "clusters": ["on_off", "level_control"], "group_id": "0x1001"},
    )
    req = fake_hub.calls("bindings/create")[0]
    # The remote's endpoint with both as client clusters.
    assert req["endpoint"] == 1
    assert req["target"] == {"group": 0x1001}
    assert res["status"] == "queued"
    assert res["queued"] is True
    assert [r["state"] for r in res["results"]] == ["queued", "queued"]

    # It wakes and answers; a read shows the table.
    fake_hub.confirm(REMOTE_DEV)
    fake_hub.sleepy.clear()
    res = await call(hass, "read_bindings", {"device_id": remote})
    assert res["reading"] is False
    assert res["queued"] is False
    assert [e["cluster"] for e in res["table"]] == [6, 8]
    assert res["pending"] == []

    # A sleepy device's read waits for its wake-up.
    fake_hub.sleepy.add(REMOTE_DEV)
    res = await call(hass, "read_bindings", {"device_id": remote, "wait": 0.6})
    assert res["reading"] is True
    assert res["queued"] is True


async def test_bind_failures(
    hass: HomeAssistant, fake_hub: NetworkFakeHub, setup_entry: MockConfigEntry
) -> None:
    fake_hub.sleepy.clear()
    remote = ha_id(hass, setup_entry, REMOTE_DEV)
    light = ha_id(hass, setup_entry, LIGHT_DEV)
    fake_hub.bind_errors[(REMOTE_DEV, 8)] = "TABLE_FULL"
    res = await call(
        hass,
        "bind",
        {"device_id": remote, "clusters": [6, 8], "target_device_id": light},
    )
    assert res["status"] == "failed"
    assert [(r["state"], r["error"]) for r in res["results"]] == [
        ("bound", None),
        ("failed", "TABLE_FULL"),
    ]
    with pytest.raises(ServiceValidationError, match="no endpoint"):
        await call(hass, "bind", {"device_id": remote, "clusters": [0x0300], "group_id": 1})
    with pytest.raises(HomeAssistantError, match="has no cluster"):
        await call(
            hass,
            "bind",
            {"device_id": remote, "endpoint": 1, "clusters": [0x0300], "group_id": 1},
        )
    with pytest.raises(Exception, match="target_device_id"):
        await call(hass, "bind", {"device_id": remote, "clusters": [6]})


async def test_groups(
    hass: HomeAssistant, fake_hub: NetworkFakeHub, setup_entry: MockConfigEntry
) -> None:
    group = await call(hass, "group_create", {"name": "Kitchen"})
    assert group["id"] == 1
    assert fake_hub.calls("groups/create")[0]["hub_member"] is True
    group = await call(
        hass, "group_create", {"name": "Hall", "group_id": "0x1001", "hub_member": False}
    )
    assert group["id"] == 0x1001

    fake_hub.sleepy.discard(LIGHT_DEV)
    light = ha_id(hass, setup_entry, LIGHT_DEV)
    res = await call(hass, "group_add_member", {"group_id": 1, "device_id": light})
    # The first endpoint with the Groups cluster.
    assert fake_hub.calls("groups/add_member")[0]["endpoint"] == 11
    assert res["status"] == "ok"
    assert res["state"] == "ok"

    # The sleepy sensor has no Groups cluster; the remote has none as a server.
    with pytest.raises(ServiceValidationError, match="Groups cluster"):
        await call(
            hass,
            "group_add_member",
            {"group_id": 1, "device_id": ha_id(hass, setup_entry, SENSOR_DEV)},
        )

    res = await call(hass, "group_remove_member", {"group_id": 1, "device_id": light})
    assert fake_hub.calls("groups/remove_member")[0]["endpoint"] == 11
    assert res["status"] == "ok"
    assert res["state"] == "removed"
    assert fake_hub.groups[1]["members"] == []

    await hass.services.async_call(
        DOMAIN, "group_remove", {"group_id": 1, "force": True}, blocking=True
    )
    assert fake_hub.calls("groups/delete")[0] == {
        "id": fake_hub.calls("groups/delete")[0]["id"],
        "type": "groups/delete",
        "group": 1,
        "force": True,
    }
    assert 1 not in fake_hub.groups
    with pytest.raises(HomeAssistantError, match="no group"):
        await hass.services.async_call(DOMAIN, "group_remove", {"group_id": 9}, blocking=True)


async def test_sleepy_group_member_is_queued(
    hass: HomeAssistant, fake_hub: NetworkFakeHub, setup_entry: MockConfigEntry
) -> None:
    await call(hass, "group_create", {"name": "Kitchen"})
    fake_hub.sleepy.add(LIGHT_DEV)
    res = await call(
        hass,
        "group_add_member",
        {"group_id": 1, "device_id": ha_id(hass, setup_entry, LIGHT_DEV), "endpoint": 11},
    )
    assert res["status"] == "queued"
    assert res["state"] == "pending"
    assert res["queued"] is True
