"""Repair issues: hub crash, missing definitions, partial interviews, offline
devices, catalog failures; their fix flows."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)

from custom_components.zigbee_rs_hub.const import DOMAIN, EVENT_HUB
from homeassistant.components.repairs import repairs_flow_manager
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from .fake_hub import (
    LIGHT_DEV,
    REMOTE_DEV,
    SENSOR_DEV,
    FakeCatalog,
    FakeHub,
    ReleaseServer,
    make_release,
)
from .test_init import wait_for


class SystemHub(FakeHub):
    """The fake hub with `hub/system` (the platform's reset reason)."""

    def __init__(self) -> None:
        super().__init__()
        self.system: dict[str, Any] | None = None

    async def _answer(self, ws, subs, req: dict[str, Any]) -> None:
        if req.get("type") == "hub/system" and self.system is not None:
            await ws.send_json(
                {"type": "result", "id": req["id"], "success": True, "result": dict(self.system)}
            )
            return
        await super()._answer(ws, subs, req)


@pytest.fixture
async def fake_hub() -> AsyncGenerator[SystemHub]:
    hub = SystemHub()
    await hub.start()
    yield hub
    await hub.stop()


def issue_id(kind: str, entry: MockConfigEntry, device: str | None = None) -> str:
    base = f"{kind}_{entry.entry_id}"
    return base if device is None else f"{base}_{device.replace(':', '_')}"


def issue(hass: HomeAssistant, ident: str) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, ident)


async def setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def update_device(
    hass: HomeAssistant, hub: FakeHub, entry: MockConfigEntry, rec: dict[str, Any]
) -> None:
    """A `device_updated`, applied."""
    await hub.push("devices", {"topic": "device_updated", "device": rec})
    await wait_for(hass, lambda: entry.runtime_data.devices.get(rec["id"]) == rec)


async def reconnect(hass: HomeAssistant, hub: FakeHub, entry: MockConfigEntry) -> None:
    connections = hub.connections
    await hub.drop()
    await wait_for(hass, lambda: hub.connections > connections)
    await wait_for(hass, lambda: entry.runtime_data.connected)
    await hass.async_block_till_done()


async def test_hub_crash(
    hass: HomeAssistant, fake_hub: SystemHub, config_entry: MockConfigEntry
) -> None:
    events = async_capture_events(hass, EVENT_HUB)
    fake_hub.system = {"version": "tbr-hub 0.4.1", "uptime_s": 10_000, "reset_reason": 4}
    await setup(hass, config_entry)
    ident = issue_id("hub_crash", config_entry)
    await wait_for(hass, lambda: issue(hass, ident) is not None)
    crash = issue(hass, ident)
    assert crash.severity is ir.IssueSeverity.ERROR
    assert crash.is_persistent and not crash.is_fixable
    assert crash.translation_placeholders["reason"] == "panic (reset reason 4)"
    assert [e.data["type"] for e in events] == ["hub_crashed"]

    # Dismissed; the same boot after a reconnect: still dismissed, no event.
    ir.async_ignore_issue(hass, DOMAIN, ident, True)
    fake_hub.system["uptime_s"] = 10_030
    await reconnect(hass, fake_hub, config_entry)
    await wait_for(hass, lambda: len(fake_hub.calls("hub/system")) >= 2)
    await hass.async_block_till_done()
    assert issue(hass, ident).dismissed_version is not None
    assert len(events) == 1

    # Another crash: shown again.
    fake_hub.system.update(uptime_s=20, reset_reason=6)
    await reconnect(hass, fake_hub, config_entry)
    await wait_for(hass, lambda: issue(hass, ident).dismissed_version is None)
    assert issue(hass, ident).translation_placeholders["reason"] == "task watchdog (reset reason 6)"
    assert len(events) == 2

    # A clean restart.
    fake_hub.system.update(uptime_s=5, reset_reason=3)
    await reconnect(hass, fake_hub, config_entry)
    await wait_for(hass, lambda: issue(hass, ident) is None)

    # The crash issue outlives an unload; it goes with the entry.
    fake_hub.system.update(uptime_s=50, reset_reason=9)
    await reconnect(hass, fake_hub, config_entry)
    await wait_for(hass, lambda: issue(hass, ident) is not None)
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    assert issue(hass, ident) is not None
    assert await hass.config_entries.async_remove(config_entry.entry_id)
    assert issue(hass, ident) is None


async def test_no_crash_record(
    hass: HomeAssistant, fake_hub: SystemHub, setup_entry: MockConfigEntry
) -> None:
    """Hubs without `hub/system` (the fake's default) or without a reset reason."""
    assert issue(hass, issue_id("hub_crash", setup_entry)) is None
    fake_hub.system = {"version": "hubctl 0.4.1", "uptime_s": 3, "mode": "desktop"}
    await reconnect(hass, fake_hub, setup_entry)
    await wait_for(hass, lambda: len(fake_hub.calls("hub/system")) >= 1)
    await hass.async_block_till_done()
    assert issue(hass, issue_id("hub_crash", setup_entry)) is None


async def test_missing_definitions(
    hass: HomeAssistant, fake_hub: SystemHub, config_entry: MockConfigEntry
) -> None:
    events = async_capture_events(hass, EVENT_HUB)
    fake_hub.catalog = FakeCatalog()
    fake_hub.catalog.missing = [
        {"device": SENSOR_DEV, "shards": []},
        # Shards still to fetch: no issue yet.
        {"device": LIGHT_DEV, "shards": [0x1A]},
    ]
    await setup(hass, config_entry)
    # Missing at set-up already: an issue, no event (none at every HA start).
    assert issue(hass, issue_id("no_definition", config_entry, SENSOR_DEV)) is not None
    assert not events
    fake_hub.catalog.missing.append(
        {
            "device": REMOTE_DEV,
            "shards": [],
            "refused": ["ikea/e2001 (IKEA E2001) requires template.ikea_styrbar"],
        }
    )
    await config_entry.runtime_data.catalog.async_refresh()
    await hass.async_block_till_done()
    none = issue(hass, issue_id("no_definition", config_entry, SENSOR_DEV))
    assert none is not None
    assert none.translation_placeholders["name"] == "Motion sensor"
    assert none.translation_placeholders["model"] == "LUMI Motion sensor"
    refused = issue(hass, issue_id("definition_refused", config_entry, REMOTE_DEV))
    assert refused.translation_placeholders["reason"] == (
        "ikea/e2001 (IKEA E2001) requires template.ikea_styrbar"
    )
    assert issue(hass, issue_id("no_definition", config_entry, LIGHT_DEV)) is None
    assert [(e.data["type"], e.data["device"], e.data.get("reason")) for e in events] == [
        (
            "definition_missing",
            REMOTE_DEV,
            "ikea/e2001 (IKEA E2001) requires template.ikea_styrbar",
        )
    ]

    # The remote got its definition, the sensor is gone from `missing`.
    fake_hub.catalog.missing = []
    await config_entry.runtime_data.catalog.async_refresh()
    await hass.async_block_till_done()
    assert issue(hass, issue_id("no_definition", config_entry, SENSOR_DEV)) is None
    assert issue(hass, issue_id("definition_refused", config_entry, REMOTE_DEV)) is None
    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_catalog_failure_and_retry(
    hass: HomeAssistant, fake_hub: SystemHub, config_entry: MockConfigEntry
) -> None:
    fake_hub.catalog = FakeCatalog()
    await setup(hass, config_entry)
    assert await async_setup_component(hass, "repairs", {})
    ident = issue_id("catalog_failed", config_entry)
    # The mirror (127.0.0.1:1) cannot be reached.
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(DOMAIN, "update_catalog", {}, blocking=True)
    failed = issue(hass, ident)
    assert failed is not None and failed.is_fixable
    assert "manifest.cose" in failed.translation_placeholders["error"]

    manager = repairs_flow_manager(hass)
    assert manager is not None
    flow = await manager.async_init(DOMAIN, data={"issue_id": ident})
    assert flow["step_id"] == "confirm"
    # Still failing: the form again, with the error.
    flow = await manager.async_configure(flow["flow_id"], {})
    assert flow["type"] == "form"
    assert flow["errors"] == {"base": "failed"}

    release = ReleaseServer(make_release())
    await release.start()
    try:
        fake_hub.catalog.config["base_url"] = release.base_url
        flow = await manager.async_configure(flow["flow_id"], {})
        assert flow["type"] == "create_entry"
        assert issue(hass, ident) is None
        assert fake_hub.catalog.installed is not None
    finally:
        await release.stop()
    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_interview_partial(
    hass: HomeAssistant, fake_hub: SystemHub, config_entry: MockConfigEntry
) -> None:
    fake_hub.devices[SENSOR_DEV]["interview"] = "partial"
    await setup(hass, config_entry)
    assert await async_setup_component(hass, "repairs", {})
    ident = issue_id("interview_partial", config_entry, SENSOR_DEV)
    # Not reported at once.
    assert issue(hass, ident) is None
    with patch(
        "custom_components.zigbee_rs_hub.const.REPAIR_INTERVIEW_PARTIAL", timedelta(seconds=0)
    ):
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=6))
        await hass.async_block_till_done()
    partial = issue(hass, ident)
    assert partial is not None and partial.is_fixable
    assert partial.translation_placeholders == {
        "hub": config_entry.title,
        "name": "Motion sensor",
        "hours": "0",
    }

    manager = repairs_flow_manager(hass)
    flow = await manager.async_init(DOMAIN, data={"issue_id": ident})
    flow = await manager.async_configure(flow["flow_id"], {})
    assert flow["type"] == "create_entry"
    assert fake_hub.calls("devices/reinterview")[-1]["device"] == SENSOR_DEV
    assert issue(hass, ident) is None

    # The time starts over: not again at the next check.
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=12))
    await hass.async_block_till_done()
    assert issue(hass, ident) is None
    # Complete: nothing to report any more.
    with patch(
        "custom_components.zigbee_rs_hub.const.REPAIR_INTERVIEW_PARTIAL", timedelta(seconds=0)
    ):
        rec = dict(fake_hub.devices[SENSOR_DEV], interview="complete")
        await update_device(hass, fake_hub, config_entry, rec)
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=18))
        await hass.async_block_till_done()
    assert issue(hass, ident) is None
    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_device_offline_and_left(
    hass: HomeAssistant, fake_hub: SystemHub, config_entry: MockConfigEntry
) -> None:
    # Unavailable, last seen days ago (the fake's `last_seen`).
    fake_hub.devices[LIGHT_DEV]["available"] = False
    await setup(hass, config_entry)
    offline = issue_id("device_offline", config_entry, LIGHT_DEV)
    assert issue(hass, offline) is not None
    assert issue(hass, offline).translation_placeholders["hours"] == "24"

    await update_device(
        hass, fake_hub, config_entry, dict(fake_hub.devices[LIGHT_DEV], available=True)
    )
    assert issue(hass, offline) is None

    # Left: reported after a while, gone when it rejoins.
    left = issue_id("device_left", config_entry, REMOTE_DEV)
    rec = dict(fake_hub.devices[REMOTE_DEV], available=False, unavailable_reason="left")
    await update_device(hass, fake_hub, config_entry, rec)
    assert issue(hass, left) is None
    with patch("custom_components.zigbee_rs_hub.const.REPAIR_DEVICE_LEFT", timedelta(seconds=0)):
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=6))
        await hass.async_block_till_done()
    assert issue(hass, left) is not None
    assert issue(hass, issue_id("device_offline", config_entry, REMOTE_DEV)) is None

    # Removed on the hub: its issues go.
    await fake_hub.push("devices", {"topic": "device_removed", "id": REMOTE_DEV})
    await wait_for(hass, lambda: issue(hass, left) is None)

    # Unloaded: the device issues go.
    fake_hub.devices[LIGHT_DEV]["available"] = False
    await reconnect(hass, fake_hub, config_entry)
    assert issue(hass, offline) is not None
    await hass.config_entries.async_unload(config_entry.entry_id)
    assert issue(hass, offline) is None
