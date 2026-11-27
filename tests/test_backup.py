"""Backups of the hub's network kept in Home Assistant, and their restore."""

from __future__ import annotations

from datetime import timedelta
import json
import os
from pathlib import Path
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.zigbee_rs_hub import backup as backup_mod
from custom_components.zigbee_rs_hub.const import (
    CONF_BACKUP_INTERVAL,
    CONF_BACKUP_KEEP,
    CONF_BACKUP_ON_CHANGE,
    DOMAIN,
)
from custom_components.zigbee_rs_hub.diagnostics import async_get_config_entry_diagnostics
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError, Unauthorized
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .fake_hub import FakeHub, device
from .fake_maintenance import LINK_KEY, NETWORK_KEY, OLD_NETWORK_KEY, ocb
from .test_init import wait_for


async def _setup(hass: HomeAssistant, entry: MockConfigEntry, **options: Any) -> None:
    entry.add_to_hass(hass)
    if options:
        hass.config_entries.async_update_entry(entry, options=options)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _dir(hass: HomeAssistant, entry: MockConfigEntry) -> Path:
    return Path(hass.config.path(".storage", f"{DOMAIN}_backups", entry.entry_id))


def _files(hass: HomeAssistant, entry: MockConfigEntry) -> list[str]:
    d = _dir(hass, entry)
    return sorted(os.listdir(d)) if d.is_dir() else []


def _sensor(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_last_backup"
    )
    assert entity_id is not None
    return entity_id


async def _backup_now(hass: HomeAssistant, **data: Any) -> dict[str, Any]:
    return await hass.services.async_call(
        DOMAIN, "backup_now", data, blocking=True, return_response=True
    )


async def test_backup_now_keeps_a_private_file(
    hass: HomeAssistant,
    fake_hub: FakeHub,
    config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await _setup(hass, config_entry)
    sensor = _sensor(hass, config_entry)
    assert hass.states.get(sensor).state == "unknown"

    res = await _backup_now(hass)
    saved = res["backups"][config_entry.entry_id]
    assert saved["reason"] == "manual"
    assert saved["complete"] is True
    assert saved["devices"] == 2
    assert fake_hub.calls("network/backup")[0]["keys"] is True

    path = _dir(hass, config_entry) / saved["file"]
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert os.stat(path.parent).st_mode & 0o777 == 0o700
    doc = json.loads(path.read_text())
    assert doc["format"] == "zigbee_rs_hub/backup"
    assert doc["network"]["network_key"]["key"] == NETWORK_KEY
    assert doc["hub"]["coordinator_ieee"] == config_entry.unique_id
    assert {d["id"] for d in doc["hub_data"]["devices"]} == set(fake_hub.devices)

    st = hass.states.get(sensor)
    assert dt_util.parse_datetime(st.state) is not None
    assert st.attributes["file"] == saved["file"]
    assert st.attributes["reason"] == "manual"
    assert st.attributes["extended_pan_id"] == "744dbdfffe641ece"
    assert st.attributes["backups"] == [saved["file"]]
    assert st.attributes["last_error"] is None

    # Keys appear neither in the log, nor in the state, nor in diagnostics.
    diag = await async_get_config_entry_diagnostics(hass, config_entry)
    assert diag["backups"]["backups"][0]["file"] == saved["file"]
    for text in (caplog.text, json.dumps(st.attributes), json.dumps(diag)):
        assert NETWORK_KEY not in text
        assert LINK_KEY not in text

    # A reload reads the stored backups back.
    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(sensor).attributes["file"] == saved["file"]
    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_backup_services_are_for_administrators(
    hass: HomeAssistant, fake_hub: FakeHub, config_entry: MockConfigEntry, hass_read_only_user
) -> None:
    await _setup(hass, config_entry)
    ctx = Context(user_id=hass_read_only_user.id)
    with pytest.raises(Unauthorized):
        await hass.services.async_call(DOMAIN, "backup_now", {}, blocking=True, context=ctx)
    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            DOMAIN, "restore_backup", {"file": "x.json"}, blocking=True, context=ctx
        )
    assert not fake_hub.calls("network/backup")
    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_a_backup_without_keys_is_not_kept(
    hass: HomeAssistant, fake_hub: FakeHub, config_entry: MockConfigEntry
) -> None:
    fake_hub.maint.backup = ocb(keys=False)
    await _setup(hass, config_entry)
    with pytest.raises(HomeAssistantError, match="no network key"):
        await _backup_now(hass)
    assert _files(hass, config_entry) == []
    st = hass.states.get(_sensor(hass, config_entry))
    assert "no network key" in st.attributes["last_error"]

    fake_hub.maint.backup_error = ("busy", "topology (job 3) is running")
    with pytest.raises(HomeAssistantError, match="busy"):
        await _backup_now(hass)
    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_only_the_newest_are_kept(
    hass: HomeAssistant, fake_hub: FakeHub, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry, **{CONF_BACKUP_KEEP: 2})
    names = [(await _backup_now(hass))["backups"][config_entry.entry_id]["file"] for _ in range(3)]
    # A file of someone else's in the directory is never pruned.
    (_dir(hass, config_entry) / "zha-backup.json").write_text(json.dumps(ocb()))
    names.append((await _backup_now(hass))["backups"][config_entry.entry_id]["file"])
    assert _files(hass, config_entry) == sorted([*names[-2:], "zha-backup.json"])
    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_scheduled_and_change_backups(
    hass: HomeAssistant, fake_hub: FakeHub, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)
    sensor = _sensor(hass, config_entry)

    # The first check after set-up: no backup yet, so one is taken.
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=backup_mod.FIRST_CHECK + 1))
    await wait_for(hass, lambda: hass.states.get(sensor).attributes["reason"] == "scheduled")
    assert len(_files(hass, config_entry)) == 1

    # A device joined: a backup two minutes later (one for a burst).
    dev = device("zb:00158d00000000aa", "IKEA", "E1743")
    for _ in range(3):
        await fake_hub.push("devices", {"topic": "device_added", "device": dev})
    hub = config_entry.runtime_data
    await wait_for(hass, lambda: dev["id"] in hub.devices)
    assert len(_files(hass, config_entry)) == 1
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=backup_mod.CHANGE_DELAY + 1))
    await wait_for(hass, lambda: hass.states.get(sensor).attributes["reason"] == "change")
    assert len(_files(hass, config_entry)) == 2
    assert len(fake_hub.calls("network/backup")) == 2

    # Within the interval the hourly check takes none; a channel change does.
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(hours=1, seconds=5))
    await hass.async_block_till_done()
    assert len(fake_hub.calls("network/backup")) == 2
    await fake_hub.push("network", {"topic": "network_updated", "reason": "tx_power"})
    await fake_hub.push("network", {"topic": "network_updated", "reason": "channel_changed"})
    await wait_for(hass, lambda: hub.backups.as_dict()["pending_change"])
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(hours=1, seconds=backup_mod.CHANGE_DELAY + 10)
    )
    await wait_for(hass, lambda: len(_files(hass, config_entry)) == 3)
    assert len(fake_hub.calls("network/backup")) == 3
    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_options_turn_automatic_backups_off(
    hass: HomeAssistant, fake_hub: FakeHub, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry, **{CONF_BACKUP_INTERVAL: 0, CONF_BACKUP_ON_CHANGE: False})
    await fake_hub.push("devices", {"topic": "device_removed", "id": "zb:00158d0001a2b3c4"})
    hub = config_entry.runtime_data
    await wait_for(hass, lambda: "zb:00158d0001a2b3c4" not in hub.devices)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(hours=2))
    await hass.async_block_till_done()
    assert not fake_hub.calls("network/backup")
    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_no_automatic_backup_without_a_network(
    hass: HomeAssistant, fake_hub: FakeHub, config_entry: MockConfigEntry
) -> None:
    fake_hub.network["state"] = "none"
    await _setup(hass, config_entry)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=backup_mod.FIRST_CHECK + 1))
    await hass.async_block_till_done()
    assert not fake_hub.calls("network/backup")
    await hass.config_entries.async_unload(config_entry.entry_id)


async def _restore(hass: HomeAssistant, **data: Any) -> dict[str, Any]:
    return await hass.services.async_call(
        DOMAIN, "restore_backup", data, blocking=True, return_response=True
    )


async def test_restore_backup(
    hass: HomeAssistant, fake_hub: FakeHub, config_entry: MockConfigEntry
) -> None:
    # 20 devices: sent in batches of 8.
    fake_hub.maint.backup = ocb(devices=20)
    await _setup(hass, config_entry)
    name = (await _backup_now(hass))["backups"][config_entry.entry_id]["file"]

    # The NCP runs a network: the hub wants force (and changes nothing).
    with pytest.raises(HomeAssistantError, match="set force"):
        await _restore(hass, file=name)
    assert fake_hub.maint.restored is None

    res = await _restore(hass, file=name, force=True, allow_ieee_change=False)
    restored = fake_hub.maint.restored
    assert restored["network_key"]["key"] == NETWORK_KEY
    assert len(restored["devices"]) == 20
    assert fake_hub.maint.restore_flags == {
        "force": True,
        "allow_ieee_change": False,
        "requests": 3,
    }
    assert res["devices"] == 20
    assert "backup" not in res
    # Backed up before (twice: the refused try too), and the hub's backup of
    # the replaced network kept.
    reasons = [b["reason"] for b in config_entry.runtime_data.backups.backups]
    assert reasons.count("before_restore") == 2
    assert reasons.count("replaced") == 1
    replaced = json.loads((_dir(hass, config_entry) / res["replaced_backup"]).read_text())
    assert replaced["network"]["network_key"]["key"] == OLD_NETWORK_KEY
    # The sensor still shows the backup of the running network.
    st = hass.states.get(_sensor(hass, config_entry))
    assert st.attributes["reason"] == "before_restore"
    assert OLD_NETWORK_KEY not in json.dumps(res)

    # Without the extra backup.
    calls = len(fake_hub.calls("network/backup"))
    await _restore(hass, file=name, force=True, skip_backup=True)
    assert len(fake_hub.calls("network/backup")) == calls
    assert fake_hub.maint.restore_flags["skip_backup"] is True
    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_restore_failure_keeps_the_replaced_network(
    hass: HomeAssistant, fake_hub: FakeHub, config_entry: MockConfigEntry
) -> None:
    await _setup(hass, config_entry)
    name = (await _backup_now(hass))["backups"][config_entry.entry_id]["file"]
    fake_hub.maint.restore_fail_after_leave = True
    with pytest.raises(HomeAssistantError, match="forming failed"):
        await _restore(hass, file=name, force=True)
    backups = config_entry.runtime_data.backups.backups
    replaced = next(b for b in backups if b["reason"] == "replaced")
    doc = json.loads((_dir(hass, config_entry) / replaced["file"]).read_text())
    assert doc["network"]["network_key"]["key"] == OLD_NETWORK_KEY
    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_restore_reads_only_allowed_files(
    hass: HomeAssistant, fake_hub: FakeHub, config_entry: MockConfigEntry, tmp_path: Path
) -> None:
    await _setup(hass, config_entry)
    with pytest.raises(ServiceValidationError, match="no backup"):
        await _restore(hass, file="missing.json", force=True)
    with pytest.raises(ServiceValidationError, match="outside"):
        await _restore(hass, file="../../secrets.yaml", force=True)
    plain = tmp_path / "zha.json"
    plain.write_text(json.dumps(ocb(keys=False)))
    with pytest.raises(ServiceValidationError, match="outside"):
        await _restore(hass, file=str(plain), force=True)
    hass.config.allowlist_external_dirs = {str(tmp_path)}
    with pytest.raises(ServiceValidationError, match="no network key"):
        await _restore(hass, file=str(plain), force=True)
    # A plain OCB file (ZHA, Zigbee2MQTT) restores too.
    plain.write_text(json.dumps(ocb(ext_pan_id="aabbccddeeff0011")))
    await _restore(hass, file=str(plain), force=True, skip_backup=True)
    assert fake_hub.maint.restored["extended_pan_id"] == "aabbccddeeff0011"
    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_backup_options(
    hass: HomeAssistant, fake_hub: FakeHub, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(config_entry, options={"sync_time_zone": True})
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    # Not a Dongle Max: no hub firmware entry in the menu.
    assert result["menu_options"] == ["time_zone", "backups"]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "backups"}
    )
    assert result["step_id"] == "backups"
    fields = {str(k): k.default() for k in result["data_schema"].schema}
    assert fields == {CONF_BACKUP_INTERVAL: 24, CONF_BACKUP_KEEP: 10, CONF_BACKUP_ON_CHANGE: True}
    await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_BACKUP_INTERVAL: 6, CONF_BACKUP_KEEP: 3}
    )
    # The other options stay.
    assert config_entry.options == {
        "sync_time_zone": True,
        CONF_BACKUP_INTERVAL: 6,
        CONF_BACKUP_KEEP: 3,
        CONF_BACKUP_ON_CHANGE: True,
    }
    assert config_entry.runtime_data.backups.keep == 3
    await hass.config_entries.async_unload(config_entry.entry_id)
