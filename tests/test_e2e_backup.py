"""End to end against `hubctl --sim serve`: backups and their restore, no firmware entities.

Runs when `HUBCTL` names a hubctl binary, on the ZRHP backend's simulated
NCP or, with `HUBCTL_BACKEND=ezsp`, the EZSP one. The simulated hubs have
neither the self-update API (`firmware/*`) nor the NCP updater
(`ncp/update/*`): those are the Dongle Max's, tested against the fake hub.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
import json
import os
from pathlib import Path

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.zigbee_rs_hub.const import CONF_PATH, DOMAIN
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_TOKEN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from .test_e2e_hubctl import HUBCTL, SIM_COORDINATOR_IEEE, Hubctl, _free_port, _wait

pytestmark = pytest.mark.skipif(not HUBCTL, reason="HUBCTL is not set")


@pytest.fixture
async def hub_entry(hass: HomeAssistant) -> AsyncGenerator[MockConfigEntry]:
    """A simulated hub with a network up, and an entry for it."""
    server = Hubctl(_free_port())
    await server.start(ready=True)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="hubctl",
        unique_id=SIM_COORDINATOR_IEEE,
        data={
            CONF_HOST: "127.0.0.1",
            CONF_PORT: server.port,
            CONF_TOKEN: "e2e",
            CONF_PATH: "/api",
        },
    )
    entry.add_to_hass(hass)
    try:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        hub = entry.runtime_data
        if hub.network.get("state") != "up":
            # The ZRHP simulator starts without a network.
            await hub.async_request("network/form", timeout=60, confirm="RESET", channel=15)
            await hub.async_refresh_network()
        await _wait(hass, lambda: hub.network.get("state") == "up")
        yield entry
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
    finally:
        await server.stop()


async def test_hubctl_backup_and_restore(hass: HomeAssistant, hub_entry: MockConfigEntry) -> None:
    entry = hub_entry
    reg = er.async_get(hass)
    # No updater on the simulated hub: no firmware entities.
    assert reg.async_get_entity_id("update", DOMAIN, f"{entry.entry_id}_hub_firmware") is None
    assert reg.async_get_entity_id("update", DOMAIN, f"{entry.entry_id}_ncp_firmware") is None

    res = await hass.services.async_call(
        DOMAIN, "backup_now", {}, blocking=True, return_response=True
    )
    saved = res["backups"][entry.entry_id]
    assert saved["complete"] is True
    assert saved["coordinator_ieee"] == SIM_COORDINATOR_IEEE
    path = Path(hass.config.path(".storage", f"{DOMAIN}_backups", entry.entry_id, saved["file"]))
    assert os.stat(path).st_mode & 0o777 == 0o600
    doc = json.loads(await hass.async_add_executor_job(path.read_text))
    net = doc["network"]
    assert net["metadata"]["format"] == "zigpy/open-coordinator-backup"
    assert len(net["network_key"]["key"]) == 32
    assert not net["metadata"]["internal"].get("incomplete")
    sensor = reg.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_last_backup")
    assert hass.states.get(sensor).attributes["file"] == saved["file"]

    # The radio runs the network: the hub wants force.
    with pytest.raises(HomeAssistantError, match="force"):
        await hass.services.async_call(
            DOMAIN, "restore_backup", {"file": saved["file"]}, blocking=True
        )
    restored = await hass.services.async_call(
        DOMAIN,
        "restore_backup",
        {"file": saved["file"], "force": True, "confirm": True},
        blocking=True,
        return_response=True,
    )
    assert restored["pan_id"] == int(net["pan_id"], 16)
    assert restored["channel"] == net["channel"]
    assert "backup" not in restored
    # The running network was backed up first, and the hub's own backup of
    # the network it replaced is kept too.
    reasons = [b["reason"] for b in entry.runtime_data.backups.backups]
    assert "before_restore" in reasons
    if restored["replaced_backup"] is not None:
        assert "replaced" in reasons
    hub = entry.runtime_data
    await _wait(hass, lambda: hub.network.get("state") == "up")
