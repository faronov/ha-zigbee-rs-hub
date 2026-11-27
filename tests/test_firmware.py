"""Firmware of the hub and of its NCP: GitHub releases, self-update, MG24 flash."""

from __future__ import annotations

from collections.abc import AsyncGenerator
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.zigbee_rs_hub import firmware
from custom_components.zigbee_rs_hub.const import CONF_REPLACE_STOCK, DOMAIN
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from .fake_hub import FakeHub
from .fake_maintenance import FakeGithub, hub_manifest
from .test_init import wait_for

HUB_REPO = "faronov/zigbee-rs-hub"
NCP_REPO = "faronov/zigbee-rs"
APP = bytes(range(256)) * 64 + b"tail"
GBL = b"GBL3" + bytes(range(200)) * 50


@pytest.fixture(autouse=True)
def fast_flash(monkeypatch: pytest.MonkeyPatch) -> None:
    """Poll the NCP flash quickly."""
    monkeypatch.setattr(firmware, "FLASH_POLL", 0.01)


@pytest.fixture
async def github(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[FakeGithub]:
    """A local GitHub API in place of api.github.com."""
    server = FakeGithub()
    await server.start()
    monkeypatch.setattr(firmware, "GITHUB_API", server.base_url)
    yield server
    await server.stop()


def publish_hub(github: FakeGithub, version: str = "0.4.2", app: bytes = APP) -> dict[str, Any]:
    manifest = hub_manifest(version, app)
    return github.add(
        HUB_REPO,
        f"hub-v{version}",
        {
            "release-manifest.json": json.dumps(manifest).encode(),
            f"zrh-donglem-{version}-app.bin": app,
            f"zrh-donglem-{version}-devtools-app.bin": b"dev",
            "tbr-hub.bin": b"s3",
        },
        body="### Fixes\n- a fix",
    )


def publish_ncp(github: FakeGithub, version: str = "0.1.3", dongle: bool = True) -> None:
    boards: list[dict[str, Any]] = [
        {
            "id": "thread-br-h2-ncp",
            "chip": "esp32h2",
            "files": {"app": {"name": f"zrhp-ncp-h2-br-{version}-app.bin", "sha256": "0" * 64}},
        }
    ]
    files = {f"zrhp-ncp-h2-br-{version}-app.bin": b"h2"}
    if dongle:
        name = f"zrhp-ncp-donglem-{version}.gbl"
        boards.append(
            {
                "id": "sonoff-dongle-m-ncp",
                "chip": "efr32mg24",
                "files": {"gbl": {"name": name, "sha256": hashlib.sha256(GBL).hexdigest()}},
            }
        )
        files[name] = GBL
    manifest = {"schema": 1, "version": version, "boards": boards}
    github.add(
        NCP_REPO,
        f"ncp-v{version}",
        {"release-manifest.json": json.dumps(manifest).encode(), **files},
        body="NCP notes",
    )


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await async_setup_component(hass, "homeassistant", {})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _entity(hass: HomeAssistant, entry: MockConfigEntry, key: str) -> str | None:
    return er.async_get(hass).async_get_entity_id("update", DOMAIN, f"{entry.entry_id}_{key}")


async def _check(hass: HomeAssistant, entity_id: str) -> Any:
    await hass.services.async_call(
        "homeassistant", "update_entity", {"entity_id": entity_id}, blocking=True
    )
    return hass.states.get(entity_id)


async def _install(hass: HomeAssistant, entity_id: str, **data: Any) -> None:
    await hass.services.async_call(
        "update", "install", {"entity_id": entity_id, **data}, blocking=True
    )


def test_release_helpers() -> None:
    assert firmware.version_of("EmberZNet 7.4.5.0 build 397") == "7.4.5.0"
    assert firmware.version_of("zigbee-rs 0.1.3") == "0.1.3"
    assert firmware.version_of(None) is None
    assert firmware.image_family("zrh-donglem-0.4.1") == "zrh-donglem"
    assert firmware.image_family("tbr-hub 0.4.1") == "tbr-hub"
    assert firmware.image_family("hubctl") is None
    manifest = hub_manifest("0.4.2", APP)
    pick = firmware.pick_hub_board
    assert pick(manifest, "zrh-donglem", False, "quad")["id"] == "sonoff-dongle-max"
    assert pick(manifest, "zrh-donglem", True, None)["id"] == "sonoff-dongle-max-devtools"
    assert pick(manifest, "zrh-donglem", False, "octal") is None
    assert pick(manifest, "unknown", False, None) is None
    assert firmware.pick_ncp_image({"boards": [{"id": "esp32h2-devkit-ncp", "files": {}}]}) is None


async def test_no_firmware_entities_without_the_updaters(
    hass: HomeAssistant, fake_hub: FakeHub, config_entry: MockConfigEntry, github: FakeGithub
) -> None:
    # The ESP32-S3 hub: backups, no self-update, no NCP updater.
    fake_hub.maint.maintenance = ["diagnostics", "backup", "restore", "wifi"]
    await _setup(hass, config_entry)
    assert _entity(hass, config_entry, "hub_firmware") is None
    assert _entity(hass, config_entry, "ncp_firmware") is None
    await hass.config_entries.async_unload(config_entry.entry_id)
    assert not github.requests


async def test_hub_firmware_update(
    hass: HomeAssistant,
    fake_hub: FakeHub,
    config_entry: MockConfigEntry,
    github: FakeGithub,
    hass_ws_client,
) -> None:
    fake_hub.maint.dongle("0.4.1")
    publish_hub(github, "0.4.0")
    publish_hub(github, "0.4.2")
    # A pre-release and another product's tag are not offered.
    github.add(HUB_REPO, "hub-v0.5.0", {}, prerelease=True)
    github.add(HUB_REPO, "definitions-v9.0", {})
    await _setup(hass, config_entry)
    entity_id = _entity(hass, config_entry, "hub_firmware")
    assert entity_id is not None
    assert hass.states.get(entity_id).attributes["installed_version"] == "0.4.1"

    st = await _check(hass, entity_id)
    assert st.state == STATE_ON
    assert st.attributes["latest_version"] == "0.4.2"
    assert st.attributes["title"] == "zigbee-rs hub"
    assert "zrh-donglem-0.4.2-app.bin" in st.attributes["release_summary"]
    assert st.attributes["release_url"].endswith("/hub-v0.4.2")
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "update/release_notes", "entity_id": entity_id})
    assert (await client.receive_json())["result"] == "### Fixes\n- a fix"

    await _install(hass, entity_id, backup=True)
    assert fake_hub.maint.fw_written == APP
    begin = fake_hub.calls("firmware/begin")[0]
    assert begin["size"] == len(APP)
    assert begin["sha256"] == hashlib.sha256(APP).hexdigest()
    assert begin["replace_stock"] is False
    assert len(fake_hub.calls("firmware/chunk")) == 5
    assert all(len(c["data"]) <= 5464 for c in fake_hub.calls("firmware/chunk"))
    assert fake_hub.maint.reboots == 1
    assert [b["reason"] for b in config_entry.runtime_data.backups.backups] == ["before_hub_update"]
    st = hass.states.get(entity_id)
    assert st.state == STATE_OFF
    assert st.attributes["installed_version"] == "0.4.2"
    assert st.attributes["in_progress"] is False
    assert "get hub-v0.4.2/zrh-donglem-0.4.2-app.bin" in github.requests
    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_the_first_update_of_a_dongle_needs_the_option(
    hass: HomeAssistant, fake_hub: FakeHub, config_entry: MockConfigEntry, github: FakeGithub
) -> None:
    fake_hub.maint.dongle("0.4.1")
    fake_hub.maint.firmware |= {"replaces_stock": True, "target_holds": "Dongle-M 1.0.7"}
    publish_hub(github)
    await _setup(hass, config_entry)
    entity_id = _entity(hass, config_entry, "hub_firmware")
    st = await _check(hass, entity_id)
    assert "SONOFF" in st.attributes["release_summary"]
    with pytest.raises(HomeAssistantError, match="SONOFF's firmware"):
        await _install(hass, entity_id)
    assert not fake_hub.calls("firmware/begin")

    flow = await hass.config_entries.options.async_init(config_entry.entry_id)
    assert flow["menu_options"] == ["time_zone", "backups", "hub_firmware"]
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "hub_firmware"}
    )
    assert flow["step_id"] == "hub_firmware"
    await hass.config_entries.options.async_configure(flow["flow_id"], {CONF_REPLACE_STOCK: True})
    await hass.async_block_till_done()
    assert config_entry.options[CONF_REPLACE_STOCK] is True
    await _install(hass, entity_id)
    assert fake_hub.calls("firmware/begin")[0]["replace_stock"] is True
    await wait_for(hass, lambda: hass.states.get(entity_id).state == STATE_OFF)
    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_a_damaged_download_is_not_uploaded(
    hass: HomeAssistant, fake_hub: FakeHub, config_entry: MockConfigEntry, github: FakeGithub
) -> None:
    fake_hub.maint.dongle("0.4.1")
    rel = publish_hub(github)
    rel["_files"]["zrh-donglem-0.4.2-app.bin"] = APP[:-1] + b"X"
    await _setup(hass, config_entry)
    entity_id = _entity(hass, config_entry, "hub_firmware")
    await _check(hass, entity_id)
    with pytest.raises(HomeAssistantError, match="SHA-256 differs"):
        await _install(hass, entity_id)
    assert not fake_hub.calls("firmware/begin")
    assert fake_hub.maint.reboots == 0
    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_ncp_update_from_a_release(
    hass: HomeAssistant, fake_hub: FakeHub, config_entry: MockConfigEntry, github: FakeGithub
) -> None:
    fake_hub.maint.dongle()
    fake_hub.network["ncp"]["fw"] = "0.1.2"
    publish_ncp(github, "0.1.3")
    await _setup(hass, config_entry)
    entity_id = _entity(hass, config_entry, "ncp_firmware")
    assert entity_id is not None
    st = await _check(hass, entity_id)
    assert st.state == STATE_ON
    assert st.attributes["installed_version"] == "0.1.2"
    assert st.attributes["latest_version"] == "0.1.3"
    assert st.attributes["title"] == "zigbee-rs NCP"

    await _install(hass, entity_id)
    assert fake_hub.maint.ncp_flashed == GBL
    assert fake_hub.maint.ncp_start == {"foreign": False, "force": False, "restore": True}
    reasons = [b["reason"] for b in config_entry.runtime_data.backups.backups]
    assert reasons == ["before_ncp_update"]
    await wait_for(hass, lambda: hass.states.get(entity_id).state == STATE_OFF)
    assert hass.states.get(entity_id).attributes["installed_version"] == "0.1.3"
    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_ncp_entity_without_a_dongle_image_or_on_emberznet(
    hass: HomeAssistant, fake_hub: FakeHub, config_entry: MockConfigEntry, github: FakeGithub
) -> None:
    fake_hub.maint.dongle()
    fake_hub.network["ncp"]["fw"] = "0.1.2"
    publish_ncp(github, "0.1.3", dongle=False)
    await _setup(hass, config_entry)
    entity_id = _entity(hass, config_entry, "ncp_firmware")
    st = await _check(hass, entity_id)
    assert st.state == STATE_OFF
    assert "ncp_update" in st.attributes["release_summary"]
    with pytest.raises(HomeAssistantError, match="No update available"):
        await _install(hass, entity_id)

    # SONOFF's EmberZNet: the zigbee-rs NCP is a switch, not an update.
    publish_ncp(github, "0.1.4")
    fake_hub.maint.ncp_backend = "ezsp"
    fake_hub.network["ncp"]["fw"] = "EmberZNet 7.4.5.0 build 397"
    connections = fake_hub.connections
    await fake_hub.drop()
    await wait_for(
        hass,
        lambda: fake_hub.connections > connections and config_entry.runtime_data.connected,
    )
    st = await _check(hass, entity_id)
    assert st.state == STATE_OFF
    assert st.attributes["installed_version"] == "7.4.5.0"
    assert st.attributes["title"] == "EmberZNet"
    assert "not an update" in st.attributes["release_summary"]
    assert not fake_hub.calls("ncp/update/begin")
    await hass.config_entries.async_unload(config_entry.entry_id)


async def _ncp_update(hass: HomeAssistant, **data: Any) -> dict[str, Any]:
    return await hass.services.async_call(
        DOMAIN, "ncp_update", data, blocking=True, return_response=True
    )


async def test_ncp_update_service(
    hass: HomeAssistant,
    fake_hub: FakeHub,
    config_entry: MockConfigEntry,
    github: FakeGithub,
    tmp_path: Path,
) -> None:
    fake_hub.maint.dongle()
    image = tmp_path / "stock.gbl"
    image.write_bytes(GBL)
    await _setup(hass, config_entry)
    with pytest.raises(ServiceValidationError, match="outside"):
        await _ncp_update(hass, path=str(image), upload_only=True)
    hass.config.allowlist_external_dirs = {str(tmp_path)}

    # Upload only: the hub checks the image; nothing is flashed.
    res = await _ncp_update(hass, path=str(image), upload_only=True)
    assert res["image"]["kind"] == "zrhp"
    assert res["flashed"] is False
    assert res["sha256"] == hashlib.sha256(GBL).hexdigest()
    assert not fake_hub.calls("ncp/update/start")

    with pytest.raises(ServiceValidationError, match="confirm"):
        await _ncp_update(hass, path=str(image))
    with pytest.raises(ServiceValidationError, match="SHA-256"):
        await _ncp_update(hass, path=str(image), confirm=True, sha256="0" * 64)

    # SONOFF's EmberZNet image: only with foreign.
    fake_hub.maint.ncp_kind = "other"
    with pytest.raises(ServiceValidationError, match="foreign"):
        await _ncp_update(hass, path=str(image), confirm=True)
    assert not fake_hub.calls("ncp/update/start")

    # No fresh backup: refused unless force.
    fake_hub.maint.backup_error = ("failed", "the NCP did not answer")
    with pytest.raises(HomeAssistantError, match="no fresh backup"):
        await _ncp_update(hass, path=str(image), confirm=True, foreign=True)
    assert not fake_hub.calls("ncp/update/start")
    fake_hub.maint.backup_error = None

    # From a URL.
    rel = github.add("someone/gbls", "v1", {"stock.gbl": GBL})
    url = rel["assets"][0]["browser_download_url"]
    res = await _ncp_update(hass, url=url, confirm=True, foreign=True, restore=False)
    assert res["flashed"] is True
    assert res["update"]["phase"] == "done"
    assert fake_hub.maint.ncp_flashed == GBL
    assert fake_hub.maint.ncp_start == {"foreign": True, "force": False, "restore": False}
    await wait_for(hass, lambda: config_entry.runtime_data.connected)

    # Flashed, but the network did not go back onto the new firmware.
    fake_hub.maint.ncp_restore = "failed"
    with pytest.raises(HomeAssistantError, match="another coordinator IEEE"):
        await _ncp_update(hass, path=str(image), confirm=True, foreign=True)
    await wait_for(hass, lambda: config_entry.runtime_data.connected)

    fake_hub.maint.ncp_fail = True
    with pytest.raises(HomeAssistantError, match="no bootloader menu"):
        await _ncp_update(hass, path=str(image), confirm=True, foreign=True)
    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_ncp_update_needs_the_updater(
    hass: HomeAssistant, fake_hub: FakeHub, config_entry: MockConfigEntry, tmp_path: Path
) -> None:
    await _setup(hass, config_entry)
    with pytest.raises(ServiceValidationError, match="no NCP updater"):
        await _ncp_update(hass, url="http://127.0.0.1:1/x.gbl", confirm=True)
    await hass.config_entries.async_unload(config_entry.entry_id)
