"""Device triggers, the action and life-cycle events on HA's bus, the logbook."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
    async_mock_service,
)

from custom_components.zigbee_rs_hub import logbook
from custom_components.zigbee_rs_hub.const import DOMAIN, EVENT_ACTION, EVENT_HUB
from custom_components.zigbee_rs_hub.hub import find_device
from homeassistant.components.device_automation import (
    DeviceAutomationType,
    async_get_device_automations,
)
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from .fake_hub import ACTION_UID, REMOTE_DEV, FakeHub, device, entity
from .test_init import wait_for

NEW_DEV = "zb:00158d00ffff0001"


def ha_device(hass: HomeAssistant, entry: MockConfigEntry, ident: str) -> dr.DeviceEntry:
    dev = find_device(dr.async_get(hass), (DOMAIN, ident), entry.entry_id)
    assert dev is not None, ident
    return dev


async def our_triggers(hass: HomeAssistant, device_id: str) -> set[tuple[str, str]]:
    triggers = await async_get_device_automations(hass, DeviceAutomationType.TRIGGER, [device_id])
    return {(t["type"], t["subtype"]) for t in triggers[device_id] if t["domain"] == DOMAIN}


async def test_device_triggers(
    hass: HomeAssistant, fake_hub: FakeHub, setup_entry: MockConfigEntry
) -> None:
    remote = ha_device(hass, setup_entry, REMOTE_DEV)
    assert await our_triggers(hass, remote.id) == {
        ("on", "action"),
        ("off", "action"),
        ("brightness_up", "action"),
    }

    calls = async_mock_service(hass, "test", "automation")
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": {
                "triggers": [
                    {
                        "trigger": "device",
                        "domain": DOMAIN,
                        "device_id": remote.id,
                        "type": "off",
                        "subtype": "action",
                    }
                ],
                "actions": [
                    {
                        "action": "test.automation",
                        "data": {"step": "{{ trigger.event.data.attributes.step }}"},
                    }
                ],
            }
        },
    )
    actions = async_capture_events(hass, EVENT_ACTION)
    for event_type, step in (("on", 1), ("off", 2)):
        await fake_hub.push(
            "events",
            {
                "topic": "entity_event",
                "uid": ACTION_UID,
                "event_type": event_type,
                "attributes": {"step": step},
                "time": 1795098200.0,
            },
        )
    await wait_for(hass, lambda: len(calls) == 1)
    assert calls[0].data == {"step": 2}
    assert [e.data["type"] for e in actions] == ["on", "off"]
    assert actions[0].data["device_id"] == remote.id
    assert actions[0].data["subtype"] == "action"
    assert actions[0].data["entity_id"] == "event.remote_action"

    # The same key on another endpoint: the subtypes name the endpoints.
    second = entity(
        f"{REMOTE_DEV}:2:action",
        "event",
        device_class="button",
        capabilities={"event_types": ["single"]},
    )
    await fake_hub.push("entities", {"topic": "entity_added", "entity": second})
    await wait_for(hass, lambda: second["uid"] in setup_entry.runtime_data.entities)
    assert await our_triggers(hass, remote.id) == {
        ("on", "action_1"),
        ("off", "action_1"),
        ("brightness_up", "action_1"),
        ("single", "action_2"),
    }


async def test_life_cycle_events(
    hass: HomeAssistant, fake_hub: FakeHub, setup_entry: MockConfigEntry
) -> None:
    events = async_capture_events(hass, EVENT_HUB)

    def kinds() -> list[str]:
        return [e.data["type"] for e in events]

    new = device(NEW_DEV, "ACME", "Button", interview="in_progress", nwk=0x1234)
    await fake_hub.push("devices", {"topic": "device_added", "device": new})
    await wait_for(hass, lambda: kinds() == ["device_joined"])
    dev = ha_device(hass, setup_entry, NEW_DEV)
    assert events[0].data == {
        "device_id": dev.id,
        "device": NEW_DEV,
        "name": "Button",
        "type": "device_joined",
        "config_entry_id": setup_entry.entry_id,
    }
    # Steps of the running interview are no event of their own.
    await fake_hub.push(
        "devices", {"topic": "interview_progress", "device": NEW_DEV, "step": "basic_0"}
    )

    layers = [
        {"source": "generic", "id": "generic"},
        {"source": "catalog", "id": "acme/button"},
    ]
    done = dict(new, interview="complete", definition=layers)
    await fake_hub.push("devices", {"topic": "device_updated", "device": done})
    await wait_for(hass, lambda: len(events) == 3)
    assert kinds()[1:] == ["interview_complete", "definition_resolved"]
    assert events[2].data["definition"] == "acme/button"

    # Leaves, comes back with another address.
    await fake_hub.push(
        "devices",
        {
            "topic": "device_updated",
            "device": dict(done, available=False, unavailable_reason="left"),
        },
    )
    await fake_hub.push("devices", {"topic": "device_updated", "device": dict(done, nwk=0x5678)})
    await wait_for(hass, lambda: len(events) == 5)
    assert kinds()[3:] == ["device_left", "device_rejoined"]
    # Gone again and back within the minute: nothing more (the last event,
    # the rejoin, holds again).
    await fake_hub.push(
        "devices",
        {
            "topic": "device_updated",
            "device": dict(done, available=False, unavailable_reason="left"),
        },
    )

    # A re-interview that ends partial.
    await fake_hub.push(
        "devices", {"topic": "interview_progress", "device": NEW_DEV, "step": "node_descriptor"}
    )
    await fake_hub.push(
        "devices",
        {"topic": "device_updated", "device": dict(done, nwk=0x5678, interview="partial")},
    )
    await wait_for(hass, lambda: len(events) == 7)
    assert kinds()[5:] == ["interview_started", "interview_partial"]
    assert events[5].data["step"] == "node_descriptor"

    await fake_hub.push("devices", {"topic": "device_removed", "id": NEW_DEV})
    await wait_for(hass, lambda: len(events) == 8)
    assert events[7].data["type"] == "device_removed"
    assert events[7].data["device_id"] == dev.id

    # Not from a snapshot: a reconnect only takes the new baseline.
    fake_hub.devices[REMOTE_DEV]["unavailable_reason"] = "left"
    connections = fake_hub.connections
    await fake_hub.drop()
    await wait_for(hass, lambda: fake_hub.connections > connections)
    await wait_for(hass, lambda: setup_entry.runtime_data.connected)
    assert len(events) == 8


async def test_flapping_device(
    hass: HomeAssistant, fake_hub: FakeHub, setup_entry: MockConfigEntry
) -> None:
    """Leaves, rejoins and leaves again within a minute: the last event says left."""
    events = async_capture_events(hass, EVENT_HUB)
    hub = setup_entry.runtime_data
    rec = dict(fake_hub.devices[REMOTE_DEV], nwk=0x1001)
    left = dict(rec, available=False, unavailable_reason="left")

    async def update(record: dict[str, Any]) -> None:
        await fake_hub.push("devices", {"topic": "device_updated", "device": record})
        await wait_for(hass, lambda: hub.devices[REMOTE_DEV] == record)

    def kinds() -> list[str]:
        return [e.data["type"] for e in events]

    await update(rec)
    await update(left)
    await update(dict(rec, nwk=0x2002))
    # A new address right after the rejoin: not a second rejoin.
    await update(dict(rec, nwk=0x3003))
    await update(left)
    assert kinds() == ["device_left", "device_rejoined"]
    # The minute ends: the device's state is reported.
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
    await hass.async_block_till_done()
    assert kinds() == ["device_left", "device_rejoined", "device_left"]

    # Back and gone again before the next window ends: its state is the one
    # reported last, so nothing more.
    await update(dict(rec, nwk=0x3003))
    await update(left)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=122))
    await hass.async_block_till_done()
    assert kinds() == ["device_left", "device_rejoined", "device_left"]


async def test_definition_removed_and_groups(
    hass: HomeAssistant, fake_hub: FakeHub, setup_entry: MockConfigEntry
) -> None:
    events = async_capture_events(hass, EVENT_HUB)
    rec = dict(
        fake_hub.devices[REMOTE_DEV],
        definition=[{"source": "generic", "id": "generic"}, {"source": "user", "id": "mine"}],
    )
    await fake_hub.push("devices", {"topic": "device_updated", "device": rec})
    await fake_hub.push(
        "devices",
        {"topic": "device_updated", "device": dict(rec, definition=rec["definition"][:1])},
    )
    # Group devices have no life cycle.
    group = device("grp:3", "zigbee-rs", "Zigbee group", group=3, interview="complete")
    await fake_hub.push("devices", {"topic": "device_added", "device": group})
    await wait_for(hass, lambda: len(events) == 2)
    assert [e.data["type"] for e in events] == ["definition_resolved", "definition_removed"]
    assert events[1].data["previous"] == "mine"


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"type": "device_joined"}, "joined the network"),
        ({"type": "device_rejoined", "nwk": "0x1A2B"}, "rejoined the network (address 0x1A2B)"),
        ({"type": "definition_resolved", "definition": "acme/x"}, "uses the definition acme/x"),
        (
            {"type": "definition_missing", "reason": "a (b c) requires tuya"},
            "has no definition it can run: a (b c) requires tuya",
        ),
        (
            {"type": "hub_crashed", "reason": "panic (reset reason 4)"},
            "restarted after a crash: panic (reset reason 4)",
        ),
        ({"type": "something_new"}, "something new"),
    ],
)
def test_logbook_messages(data: dict, message: str) -> None:
    assert logbook.describe(data) == message


async def test_logbook_names_the_device(hass: HomeAssistant, setup_entry: MockConfigEntry) -> None:
    described = {}

    def register(domain: str, event_type: str, describe) -> None:
        described[(domain, event_type)] = describe

    logbook.async_describe_events(hass, register)
    describe = described[(DOMAIN, EVENT_HUB)]
    dev = ha_device(hass, setup_entry, REMOTE_DEV)
    dr.async_get(hass).async_update_device(dev.id, name_by_user="Kitchen remote")
    entry = describe(
        Event(EVENT_HUB, {"device_id": dev.id, "name": "Remote", "type": "device_left"})
    )
    assert entry == {"name": "Kitchen remote", "message": "left the network"}
    entry = describe(
        Event(EVENT_HUB, {"device_id": None, "name": "Gone", "type": "device_removed"})
    )
    assert entry == {"name": "Gone", "message": "was removed from the network"}
