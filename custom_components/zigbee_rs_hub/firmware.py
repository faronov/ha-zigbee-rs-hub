"""Firmware of the hub itself and of its NCP, from GitHub releases.

Hub: a hub with the self-update API (`firmware/*`, maintenance feature
`firmware`; the SONOFF Dongle Max today, docs/dongle-max.md "Self-update")
gets an `update` entity. The newest `hub-v*` release of
faronov/zigbee-rs-hub names its images per board in `release-manifest.json`;
the board is picked by the running image (`zrh-donglem`, `tbr-hub`), the
edition (`devtools`) and the PSRAM variant (`hub/system`). Home Assistant
downloads the app image, checks its SHA-256 against the manifest and
`SHA256SUMS`, uploads it in 4 KiB chunks (`firmware/begin`, `chunk`,
`end`: the hub checks it again and selects it) and reboots the hub
(`hub/reboot`). The first update of a Dongle Max writes over SONOFF's
application (`replace_stock`): only with the entry's option. Hubs without
the API (the ESP32-S3 Thread Border Router hub, the 4 MB layout) get no
entity.

NCP: a hub with the NCP updater (`ncp/*`, maintenance feature
`ncp_update`: the Dongle Max's MG24 through its Gecko bootloader,
docs/dongle-max.md "Replacing the MG24 firmware") gets an `update` entity
for the radio's firmware (`network/info` `ncp.fw`: SONOFF's EmberZNet or
the zigbee-rs ZRHP NCP). A newer zigbee-rs NCP (`ncp-v*` releases of
faronov/zigbee-rs) is offered only while the radio runs the zigbee-rs NCP
and only if the release has a GBL for the Dongle-M; any GBL can be
installed with the service `zigbee_rs_hub.ncp_update`. Both take a fresh
backup first (backup.py), and the hub keeps its own copy of the network
before it flashes, carries it over and restores it afterwards.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
import hashlib
import json
import logging
import re
import time
from typing import TYPE_CHECKING, Any

import aiohttp
from awesomeversion import AwesomeVersion, AwesomeVersionException
import voluptuous as vol

from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityFeature,
)
from homeassistant.const import EntityCategory
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.service import async_register_admin_service

from .backup import hub_of_call
from .const import ATTR_CONFIG_ENTRY_ID, CONF_REPLACE_STOCK, DOMAIN, SERVICE_NCP_UPDATE
from .entity import hub_device_info
from .hub import hub_version

if TYPE_CHECKING:
    from .hub import ZigbeeRsHub

_LOGGER = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
HUB_REPOSITORY = "faronov/zigbee-rs-hub"
HUB_TAG_PREFIX = "hub-v"
NCP_REPOSITORY = "faronov/zigbee-rs"
NCP_TAG_PREFIX = "ncp-v"
MANIFEST = "release-manifest.json"
SUMS = "SHA256SUMS"
API_HEADERS = {
    "Accept": "application/vnd.github+json",
    "User-Agent": "zigbee-rs-hub-home-assistant",
}
RELEASES_TTL = 6 * 3600.0
FETCH_TIMEOUT = aiohttp.ClientTimeout(total=300)
CHECK_FIRST = 90.0
CHECK_INTERVAL = timedelta(hours=12)
# Largest files downloaded: a hub app slot, the MG24 image store.
HUB_IMAGE_MAX = 8 * 1024 * 1024
NCP_IMAGE_MAX = 1024 * 1024
MANIFEST_MAX = 1024 * 1024
# Decoded bytes per `firmware/chunk` and `ncp/update/chunk`.
CHUNK = 4096
CHUNK_TIMEOUT = 30.0
# `firmware/end` checks the whole image in flash.
END_TIMEOUT = 120.0
# A reboot: the connection drops, then the hub is back.
REBOOT_DOWN = 60.0
REBOOT_UP = 300.0
# The MG24 flash: the hub's backup, up to 3 XMODEM uploads, its restart.
FLASH_POLL = 2.0
FLASH_TIMEOUT = 1200.0

REPLACE_STOCK_MESSAGE = (
    "This first update of the hub writes over SONOFF's firmware in the other app slot: "
    "afterwards the way back to SONOFF's firmware is USB only (SONOFF Dongle Flasher or "
    "a flash backup). To allow it, turn on 'Allow the first hub update to replace "
    "SONOFF's firmware' in the integration's options, then install again."
)

_VERSION = re.compile(r"\d+(?:\.\d+)+")


def version_of(text: Any) -> str | None:
    """The first dotted version in a firmware string (`EmberZNet 7.4.5.0 build 397`)."""
    if not isinstance(text, str):
        return None
    m = _VERSION.search(text)
    return m.group(0) if m else None


def image_family(version: Any) -> str | None:
    """The image name of an app version: `zrh-donglem-0.4.1` → `zrh-donglem`."""
    if not isinstance(version, str):
        return None
    m = re.match(r"^(.*?)[-_ ]v?\d+\.\d+", version.strip())
    return m.group(1) if m and m.group(1) else None


def _features(board: dict[str, Any]) -> set[str]:
    f = board.get("features")
    if isinstance(f, str):
        return {x.strip() for x in f.split(",") if x.strip()}
    return {str(x) for x in f} if isinstance(f, list) else set()


def pick_hub_board(
    manifest: dict[str, Any], family: str | None, devtools: bool, psram: str | None
) -> dict[str, Any] | None:
    """The manifest's board entry of this hub's image, or `None` when not exactly one."""
    if not family:
        return None
    boards = [
        b
        for b in manifest.get("boards") or []
        if isinstance(b, dict)
        and b.get("firmware") == family
        and isinstance((b.get("files") or {}).get("app"), dict)
        and ("dev" in _features(b)) == devtools
    ]
    if psram:
        boards = [b for b in boards if b.get("psram") in (None, psram)]
    return boards[0] if len(boards) == 1 else None


_DONGLE_M = re.compile(r"dongle[-_ ]?m(ax)?\b|donglem", re.IGNORECASE)


def pick_ncp_image(manifest: dict[str, Any]) -> dict[str, Any] | None:
    """A Dongle-M MG24 GBL in a zigbee-rs NCP release manifest, if there is one."""
    for b in manifest.get("boards") or []:
        if not isinstance(b, dict):
            continue
        label = " ".join(str(b.get(k) or "") for k in ("id", "title", "board"))
        chip = str(b.get("chip") or "").lower()
        if not (_DONGLE_M.search(label) or chip.startswith("efr32mg24")):
            continue
        for f in (b.get("files") or {}).values():
            if isinstance(f, dict) and str(f.get("name", "")).endswith(".gbl"):
                return {**f, "board": b.get("id")}
    return None


@dataclass(frozen=True)
class Release:
    """One GitHub release."""

    tag: str
    version: str
    name: str
    notes: str
    url: str
    assets: dict[str, str]


async def download(
    session: aiohttp.ClientSession, url: str, size_max: int, headers: dict[str, str] | None = None
) -> bytes:
    """GET `url`, at most `size_max` bytes."""
    data = bytearray()
    try:
        async with session.get(url, timeout=FETCH_TIMEOUT, headers=headers) as resp:
            resp.raise_for_status()
            if resp.content_length is not None and resp.content_length > size_max:
                raise HomeAssistantError(f"{url}: {resp.content_length} B, more than {size_max}")
            async for chunk in resp.content.iter_chunked(64 * 1024):
                data += chunk
                if len(data) > size_max:
                    raise HomeAssistantError(f"{url}: more than {size_max} B")
    except (aiohttp.ClientError, TimeoutError) as err:
        raise HomeAssistantError(f"download of {url} failed: {err}") from err
    return bytes(data)


class Releases:
    """The newest release of a repository whose tags start with a prefix (cached)."""

    def __init__(self, session: aiohttp.ClientSession, repository: str, prefix: str) -> None:
        """Releases of `repository` tagged `prefix*`."""
        self.session = session
        self.repository = repository
        self.prefix = prefix
        self._latest: tuple[float, Release | None] | None = None
        self._files: dict[tuple[str, str], Any] = {}

    async def latest(self, force: bool = False) -> Release | None:
        """The newest release that is neither a draft nor a pre-release."""
        if (
            not force
            and self._latest is not None
            and time.monotonic() - self._latest[0] < RELEASES_TTL
        ):
            return self._latest[1]
        url = f"{GITHUB_API}/repos/{self.repository}/releases?per_page=30"
        raw = await download(self.session, url, MANIFEST_MAX, API_HEADERS)
        try:
            items = json.loads(raw)
        except ValueError as err:
            raise HomeAssistantError(f"{url}: {err}") from err
        best: Release | None = None
        for r in items if isinstance(items, list) else []:
            if not isinstance(r, dict) or r.get("draft") or r.get("prerelease"):
                continue
            tag = r.get("tag_name")
            if not isinstance(tag, str) or not tag.startswith(self.prefix):
                continue
            rel = Release(
                tag=tag,
                version=tag[len(self.prefix) :],
                name=str(r.get("name") or tag),
                notes=str(r.get("body") or ""),
                url=str(r.get("html_url") or ""),
                assets={
                    a["name"]: a["browser_download_url"]
                    for a in r.get("assets") or []
                    if isinstance(a, dict)
                    and isinstance(a.get("name"), str)
                    and isinstance(a.get("browser_download_url"), str)
                },
            )
            try:
                if best is None or AwesomeVersion(rel.version) > AwesomeVersion(best.version):
                    best = rel
            except AwesomeVersionException:
                continue
        self._latest = (time.monotonic(), best)
        return best

    async def _asset(self, rel: Release, name: str, size_max: int = MANIFEST_MAX) -> bytes:
        url = rel.assets.get(name)
        if url is None:
            raise HomeAssistantError(f"{rel.tag} has no {name}")
        return await download(self.session, url, size_max)

    async def manifest(self, rel: Release) -> dict[str, Any]:
        """The release's `release-manifest.json`."""
        key = (rel.tag, MANIFEST)
        if key not in self._files:
            try:
                data = json.loads(await self._asset(rel, MANIFEST))
            except ValueError as err:
                raise HomeAssistantError(f"{rel.tag} {MANIFEST}: {err}") from err
            self._files[key] = data if isinstance(data, dict) else {}
        return self._files[key]

    async def sums(self, rel: Release) -> dict[str, str]:
        """The release's `SHA256SUMS`: file name → SHA-256."""
        key = (rel.tag, SUMS)
        if key not in self._files:
            text = (await self._asset(rel, SUMS)).decode(errors="replace")
            sums = {}
            for line in text.splitlines():
                parts = line.split()
                if len(parts) == 2 and len(parts[0]) == 64:
                    sums[parts[1].lstrip("*")] = parts[0].lower()
            self._files[key] = sums
        return self._files[key]

    async def fetch(self, rel: Release, name: str, size_max: int, sha256: Any) -> bytes:
        """A release file, checked against the manifest's SHA-256 and `SHA256SUMS`."""
        listed = (await self.sums(rel)).get(name)
        if listed is None:
            raise HomeAssistantError(f"{name} is not in {rel.tag}'s {SUMS}")
        if isinstance(sha256, str) and sha256.lower() != listed:
            raise HomeAssistantError(f"{name}: {rel.tag}'s manifest and {SUMS} differ")
        data = await self._asset(rel, name, size_max)
        if hashlib.sha256(data).hexdigest() != listed:
            raise HomeAssistantError(f"{name}: the download's SHA-256 differs from {SUMS}")
        return data


def releases_for(hass: HomeAssistant, repository: str, prefix: str) -> Releases:
    """The shared release cache of a repository (one GitHub request for every hub)."""
    cache: dict[tuple[str, str], Releases] = hass.data.setdefault(f"{DOMAIN}_releases", {})
    key = (repository, prefix)
    if key not in cache:
        cache[key] = Releases(async_get_clientsession(hass), repository, prefix)
    return cache[key]


# Hub requests and the restart.


async def upload(
    hub: ZigbeeRsHub, prefix: str, data: bytes, chunk: int, progress: Callable[[float], None]
) -> None:
    """`<prefix>/chunk` requests for all of `data`, in order."""
    chunk = max(1, min(chunk, CHUNK))
    offset = 0
    while offset < len(data):
        end = min(offset + chunk, len(data))
        await hub.async_request(
            f"{prefix}/chunk",
            timeout=CHUNK_TIMEOUT,
            offset=offset,
            data=base64.b64encode(data[offset:end]).decode(),
        )
        offset = end
        progress(offset / len(data))


async def _until(hub: ZigbeeRsHub, cond: Callable[[], bool], timeout: float) -> None:
    """Until `cond` holds; checked whenever the hub's connection changes."""
    if cond():
        return
    event = asyncio.Event()

    @callback
    def changed() -> None:
        if cond():
            event.set()

    remove = hub.add_hub_listener(changed)
    try:
        async with asyncio.timeout(timeout):
            await event.wait()
    finally:
        remove()


async def wait_for_restart(hub: ZigbeeRsHub, client: Any, down: float = REBOOT_DOWN) -> None:
    """The connection `client` drops (within `down` s), then the hub is back on a new one."""
    try:
        await _until(hub, lambda: hub.client is not client or not hub.connected, down)
    except TimeoutError as err:
        raise HomeAssistantError("the hub did not restart") from err
    await wait_until_back(hub, client)


async def wait_until_back(hub: ZigbeeRsHub, client: Any) -> None:
    """The hub is connected again, on another connection than `client`."""
    try:
        await _until(hub, lambda: hub.client is not client and hub.connected, REBOOT_UP)
    except TimeoutError as err:
        raise HomeAssistantError(f"the hub did not come back within {REBOOT_UP:.0f} s") from err


async def async_ncp_flash(
    hub: ZigbeeRsHub,
    data: bytes,
    *,
    foreign: bool = False,
    force: bool = False,
    restore: bool = True,
    upload_only: bool = False,
    progress: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    """Upload a GBL to the hub's MG24 image store and flash it (`ncp/update/*`).

    The image is uploaded and checked by the hub first; then a fresh
    backup of the running network goes into Home Assistant (refused
    without one, unless `force`), and the hub flashes, carrying the network
    over (it keeps its own backup first). `progress` gets 0 to 1: the upload
    is the first tenth, the XMODEM transfer the rest.
    """

    def report(x: float) -> None:
        if progress is not None:
            progress(x)

    status = await hub.async_request("ncp/update/status")
    if not isinstance(status, dict):
        raise HomeAssistantError("ncp/update/status: no answer")
    if (status.get("job") or {}).get("running"):
        raise HomeAssistantError("the hub is flashing the NCP already")
    if (status.get("store") or {}).get("available") is False:
        raise HomeAssistantError(
            "the hub's MG24 image store is not available: it needs the claimed files "
            "region (the hub's data are in RAM only; storage/status)"
        )
    sha = hashlib.sha256(data).hexdigest()
    try:
        begin = await hub.async_request("ncp/update/begin", size=len(data), sha256=sha)
        await upload(
            hub,
            "ncp/update",
            data,
            int((begin or {}).get("chunk") or CHUNK),
            lambda x: report(x / 10),
        )
        image = await hub.async_request("ncp/update/end", timeout=END_TIMEOUT)
    except HomeAssistantError:
        await _quiet(hub, "ncp/update/abort")
        raise
    if not isinstance(image, dict):
        image = {}
    if upload_only:
        return {"image": image, "flashed": False}
    if image.get("kind") != "zrhp" and not foreign:
        raise ServiceValidationError(
            f"the image is not a zigbee-rs ZRHP NCP application ({image.get('kind')}: "
            f"{image.get('known') or 'EmberZNet, another firmware or a maintenance image'}): "
            "the hub then runs on whatever the MG24 answers. Set foreign to flash it."
        )
    if hub.network.get("state") == "up":
        try:
            await hub.backups.async_backup("before_ncp_update")
        except HomeAssistantError as err:
            if not force:
                raise HomeAssistantError(
                    f"no fresh backup of the network, the NCP is not flashed: {err}"
                ) from err
            _LOGGER.warning("Flashing the NCP of %s without a fresh backup: %s", hub.url, err)
    client = hub.client
    await hub.async_request(
        "ncp/update/start", confirm=True, foreign=foreign, force=force, restore=restore
    )
    _LOGGER.info(
        "The hub at %s flashes its NCP (%s)", hub.url, image.get("fw") or image.get("kind")
    )
    final = await _follow_flash(hub, client, report)
    update = final.get("update") if isinstance(final.get("update"), dict) else {}
    if update.get("phase") not in (None, "done"):
        raise HomeAssistantError(
            f"NCP update {update.get('phase')}: {update.get('message') or final.get('job')}"
        )
    if update.get("restore") == "failed":
        raise HomeAssistantError(
            "the radio runs the new firmware, but the network was not put back onto it: "
            f"{update.get('restore_message')}. The hub keeps the network: retry with its web "
            "UI's Restore network (ncp/update/restore) or restore_backup"
        )
    return {"image": image, "flashed": True, "update": update}


async def _quiet(hub: ZigbeeRsHub, req_type: str) -> None:
    try:
        await hub.async_request(req_type)
    except HomeAssistantError as err:
        _LOGGER.debug("%s: %s", req_type, err)


async def _follow_flash(
    hub: ZigbeeRsHub, client: Any, report: Callable[[float], None]
) -> dict[str, Any]:
    """Follow `ncp/update/status` until the flash and the network's restore ended.

    The flash job ends with a restart of the hub (on the new firmware); a
    job that failed before it reports `failed` and the hub keeps running.
    After the restart the hub restores the network once the NCP's session
    is up (`update.restore`: `wanted`, `pending`, then `done` or `failed`).
    """
    try:
        async with asyncio.timeout(FLASH_TIMEOUT):
            while True:
                await asyncio.sleep(FLASH_POLL)
                if hub.client is not client or not hub.connected:
                    await wait_until_back(hub, client)
                    report(1.0)
                    return await _restored(hub)
                try:
                    status = await hub.async_request("ncp/update/status")
                except HomeAssistantError:
                    continue
                job = (status or {}).get("job") or {}
                if job.get("running"):
                    if total := job.get("total") or 0:
                        report(0.1 + 0.9 * min(job.get("sent") or 0, total) / total)
                elif job.get("phase") == "failed":
                    raise HomeAssistantError(f"NCP update failed: {job.get('message')}")
    except TimeoutError as err:
        raise HomeAssistantError(
            f"the NCP update did not end within {FLASH_TIMEOUT:.0f} s (ncp/update/status)"
        ) from err


async def _restored(hub: ZigbeeRsHub) -> dict[str, Any]:
    """The update state once the network's restore onto the new NCP ended."""
    status: dict[str, Any] = {}
    async with asyncio.timeout(REBOOT_UP):
        while True:
            try:
                answer = await hub.async_request("ncp/update/status")
            except HomeAssistantError:
                if not hub.connected:
                    raise
                answer = None
            if isinstance(answer, dict):
                status = answer
                update = status.get("update") if isinstance(status.get("update"), dict) else {}
                if not status.get("restoring") and update.get("restore") not in (
                    "wanted",
                    "pending",
                ):
                    return status
            await asyncio.sleep(FLASH_POLL)


# Entities.


class _FirmwareEntity(UpdateEntity):
    """An update entity on the hub device, refreshed from GitHub twice a day."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, hub: ZigbeeRsHub, key: str, releases: Releases) -> None:
        """Entity `key` of `hub`."""
        self.hub = hub
        self.releases = releases
        self._attr_translation_key = key
        self._attr_unique_id = f"{hub.entry.entry_id}_{key}"
        self._attr_device_info = hub_device_info(hub)
        self._release: Release | None = None
        self._note: str | None = None
        self._apply()

    def _apply(self) -> None:
        raise NotImplementedError

    @property
    def available(self) -> bool:
        """Only while connected."""
        return self.hub.connected

    async def async_added_to_hass(self) -> None:
        """Follow the connection; check GitHub soon, then twice a day."""
        self.async_on_remove(self.hub.add_hub_listener(self._hub_update))
        self.async_on_remove(async_call_later(self.hass, CHECK_FIRST, self._scheduled))
        self.async_on_remove(async_track_time_interval(self.hass, self._scheduled, CHECK_INTERVAL))

    @callback
    def _hub_update(self) -> None:
        self._apply()
        self.async_write_ha_state()

    @callback
    def _scheduled(self, _now: Any) -> None:
        self.hub.entry.async_create_background_task(
            self.hass, self._scheduled_refresh(), f"{DOMAIN} firmware check"
        )

    async def _scheduled_refresh(self) -> None:
        await self.async_refresh()
        self.async_write_ha_state()

    async def async_update(self) -> None:
        """`homeassistant.update_entity`: check GitHub now."""
        await self.async_refresh(force=True)

    async def async_refresh(self, force: bool = False) -> None:
        """Check the releases and the hub."""
        if not self.hub.connected:
            return
        try:
            await self._refresh(force)
        except HomeAssistantError as err:
            _LOGGER.debug("Firmware check for %s: %s", self.hub.url, err)
            self._note = f"Update check failed: {err}"
        self._apply()

    async def _refresh(self, force: bool) -> None:
        raise NotImplementedError

    async def async_release_notes(self) -> str | None:
        """The GitHub release's notes."""
        return self._release.notes if self._release is not None else None

    @callback
    def _progress(self, fraction: float) -> None:
        pct = int(fraction * 100)
        if pct != self._attr_update_percentage:
            self._attr_update_percentage = pct
            self.async_write_ha_state()

    def _begin_install(self) -> None:
        self._attr_in_progress = True
        self._attr_update_percentage = None
        self.async_write_ha_state()


class HubFirmwareUpdate(_FirmwareEntity):
    """The hub's own firmware (self-update through `firmware/*`)."""

    _attr_supported_features = (
        UpdateEntityFeature.INSTALL
        | UpdateEntityFeature.PROGRESS
        | UpdateEntityFeature.RELEASE_NOTES
        | UpdateEntityFeature.BACKUP
    )
    _attr_title = "zigbee-rs hub"

    def __init__(self, hub: ZigbeeRsHub) -> None:
        """Entity of `hub`."""
        self._image: dict[str, Any] | None = None
        self._status: dict[str, Any] = {}
        super().__init__(
            hub, "hub_firmware", releases_for(hub.hass, HUB_REPOSITORY, HUB_TAG_PREFIX)
        )

    def _apply(self) -> None:
        installed = hub_version(self.hub.info.get("name"))
        self._attr_installed_version = installed
        rel = self._release
        if rel is not None and self._image is not None:
            self._attr_latest_version = rel.version
            self._attr_release_url = rel.url or None
        else:
            self._attr_latest_version = installed
            self._attr_release_url = rel.url if rel is not None else None
        summary = self._note
        if summary is None and rel is not None and self._image is not None:
            summary = f"{rel.name}: {self._image['name']}"
            if self._status.get("replaces_stock") and not self.hub.entry.options.get(
                CONF_REPLACE_STOCK
            ):
                summary += " (writes over SONOFF's firmware: allow it in the options first)"
        self._attr_release_summary = summary

    async def _refresh(self, force: bool) -> None:
        self._note = None
        rel = await self.releases.latest(force)
        self._release = rel
        self._image = None
        system = await self.hub.async_request("hub/system")
        system = system if isinstance(system, dict) else {}
        status = system.get("firmware")
        if not isinstance(status, dict):
            status = await self.hub.async_request("firmware/status")
        self._status = status if isinstance(status, dict) else {}
        if rel is None:
            self._note = f"No {HUB_TAG_PREFIX}* release of {HUB_REPOSITORY}"
            return
        family = image_family(self._status.get("version") or system.get("version"))
        build = system.get("build") if isinstance(system.get("build"), dict) else {}
        board = pick_hub_board(
            await self.releases.manifest(rel),
            family,
            self.hub.info.get("edition") == "devtools",
            build.get("psram") or None,
        )
        if board is None:
            self._note = f"{rel.tag} has no single image for this hub ({family or 'unknown image'})"
            return
        self._image = board["files"]["app"]

    async def async_install(self, version: str | None, backup: bool, **kwargs: Any) -> None:
        """Download, check, upload, select and reboot into the release's image."""
        rel, image = self._release, self._image
        if rel is None or image is None:
            raise HomeAssistantError(
                self._note or "no hub image to install: check for updates first"
            )
        if version is not None and version != rel.version:
            raise HomeAssistantError(f"only the newest release ({rel.version}) can be installed")
        hub = self.hub
        status = await hub.async_request("firmware/status")
        status = status if isinstance(status, dict) else {}
        if status.get("state") == "uploading":
            raise HomeAssistantError("another firmware upload to the hub is running (web UI?)")
        replace = bool(status.get("replaces_stock"))
        if replace and not hub.entry.options.get(CONF_REPLACE_STOCK):
            raise HomeAssistantError(REPLACE_STOCK_MESSAGE)
        if backup:
            await hub.backups.async_backup("before_hub_update")
        data = await self.releases.fetch(rel, image["name"], HUB_IMAGE_MAX, image.get("sha256"))
        if isinstance(image.get("size"), int) and len(data) != image["size"]:
            raise HomeAssistantError(
                f"{image['name']}: {len(data)} B, the manifest says {image['size']}"
            )
        self._begin_install()
        try:
            try:
                begin = await hub.async_request(
                    "firmware/begin",
                    size=len(data),
                    sha256=hashlib.sha256(data).hexdigest(),
                    replace_stock=replace,
                )
                await upload(
                    hub, "firmware", data, int((begin or {}).get("chunk") or CHUNK), self._progress
                )
                end = await hub.async_request("firmware/end", timeout=END_TIMEOUT)
            except HomeAssistantError:
                await _quiet(hub, "firmware/abort")
                raise
            _LOGGER.info(
                "%s written to the hub at %s, rebooting", (end or {}).get("version"), hub.url
            )
            self._attr_update_percentage = None
            self.async_write_ha_state()
            client = hub.client
            await hub.async_request("hub/reboot")
            await wait_for_restart(hub, client)
            installed = hub_version(hub.info.get("name"))
            if installed != rel.version:
                raise HomeAssistantError(f"the hub came back with {installed}, not {rel.version}")
        finally:
            self._attr_in_progress = False
            self._attr_update_percentage = None
            self._apply()


class NcpFirmwareUpdate(_FirmwareEntity):
    """The firmware of the hub's radio (NCP), flashed by the hub (`ncp/update/*`)."""

    _attr_supported_features = (
        UpdateEntityFeature.INSTALL
        | UpdateEntityFeature.PROGRESS
        | UpdateEntityFeature.RELEASE_NOTES
    )

    def __init__(self, hub: ZigbeeRsHub) -> None:
        """Entity of `hub`."""
        self._image: dict[str, Any] | None = None
        self._backend: str | None = None
        super().__init__(
            hub, "ncp_firmware", releases_for(hub.hass, NCP_REPOSITORY, NCP_TAG_PREFIX)
        )

    @property
    def _fw(self) -> str | None:
        ncp = self.hub.network.get("ncp")
        fw = ncp.get("fw") if isinstance(ncp, dict) else None
        return fw if isinstance(fw, str) else None

    @property
    def _runs_zrhp(self) -> bool:
        if self._backend is not None:
            return self._backend == "zrhp"
        return self._fw is not None and not self._fw.startswith("EmberZNet")

    def _apply(self) -> None:
        fw = self._fw
        installed = version_of(fw) or fw
        self._attr_installed_version = installed
        if fw is not None and fw.startswith("EmberZNet"):
            self._attr_title = "EmberZNet"
        elif self._runs_zrhp:
            self._attr_title = "zigbee-rs NCP"
        else:
            self._attr_title = None
        rel = self._release
        offered = rel is not None and self._image is not None and self._runs_zrhp
        self._attr_latest_version = rel.version if offered and rel else installed
        self._attr_release_url = rel.url if rel is not None else None
        summary = self._note
        if summary is None and rel is not None:
            if self._image is None:
                summary = (
                    f"{rel.tag} has no zigbee-rs NCP image for the Dongle-M's MG24: install "
                    "a GBL with the zigbee_rs_hub.ncp_update action"
                )
            elif not self._runs_zrhp:
                summary = (
                    f"The radio runs {fw or 'another firmware'}: switching to the zigbee-rs NCP "
                    "is not an update, use the zigbee_rs_hub.ncp_update action"
                )
            else:
                summary = f"{rel.name}: {self._image['name']}"
        self._attr_release_summary = summary

    async def _refresh(self, force: bool) -> None:
        self._note = None
        try:
            status = await self.hub.async_request("ncp/update/status")
        except HomeAssistantError:
            status = None
        backend = (status or {}).get("backend") if isinstance(status, dict) else None
        self._backend = backend.get("running") if isinstance(backend, dict) else None
        rel = await self.releases.latest(force)
        self._release = rel
        self._image = None
        if rel is None:
            self._note = f"No {NCP_TAG_PREFIX}* release of {NCP_REPOSITORY}"
            return
        self._image = pick_ncp_image(await self.releases.manifest(rel))

    async def async_install(self, version: str | None, backup: bool, **kwargs: Any) -> None:
        """Download and check the release's GBL; the hub flashes it."""
        rel, image = self._release, self._image
        if rel is None or image is None or not self._runs_zrhp:
            raise HomeAssistantError(
                self._attr_release_summary or "no NCP image to install: check for updates first"
            )
        if version is not None and version != rel.version:
            raise HomeAssistantError(f"only the newest release ({rel.version}) can be installed")
        data = await self.releases.fetch(rel, image["name"], NCP_IMAGE_MAX, image.get("sha256"))
        self._begin_install()
        try:
            await async_ncp_flash(self.hub, data, progress=self._progress)
        finally:
            self._attr_in_progress = False
            self._attr_update_percentage = None
            await self.async_refresh()


def firmware_entities(hub: ZigbeeRsHub, added: set[str]) -> list[UpdateEntity]:
    """The firmware entities this hub supports (`maintenance` in hub/info), not in `added`."""
    maintenance = hub.info.get("maintenance")
    maintenance = maintenance if isinstance(maintenance, list) else []
    entities: list[UpdateEntity] = []
    for feature, cls in (("firmware", HubFirmwareUpdate), ("ncp_update", NcpFirmwareUpdate)):
        if feature in maintenance and feature not in added:
            added.add(feature)
            entities.append(cls(hub))
    return entities


# The `ncp_update` service.

NCP_UPDATE_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
            vol.Exclusive("url", "image"): cv.url,
            vol.Exclusive("path", "image"): cv.string,
            vol.Optional("sha256"): vol.All(cv.string, vol.Match(r"^[0-9a-fA-F]{64}$")),
            vol.Optional("confirm", default=False): cv.boolean,
            vol.Optional("foreign", default=False): cv.boolean,
            vol.Optional("force", default=False): cv.boolean,
            vol.Optional("restore", default=True): cv.boolean,
            vol.Optional("upload_only", default=False): cv.boolean,
        }
    ),
    cv.has_at_least_one_key("url", "path"),
)


def _read_bytes(path: str, size_max: int) -> bytes:
    with open(path, "rb") as f:
        data = f.read(size_max + 1)
    if len(data) > size_max:
        raise ValueError(f"more than {size_max} B")
    return data


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """`ncp_update` (administrators)."""

    async def ncp_update(call: ServiceCall) -> ServiceResponse:
        hub = hub_of_call(hass, call)
        data = call.data
        if "ncp_update" not in (hub.info.get("maintenance") or []):
            raise ServiceValidationError(
                f"{hub.entry.title} has no NCP updater (only the SONOFF Dongle Max's MG24 has one)"
            )
        if not data["upload_only"] and not data["confirm"]:
            raise ServiceValidationError(
                "Flashing replaces the radio's firmware: the hub backs up the network, flashes, "
                "restarts on the new firmware and restores the network onto it. Set confirm "
                "(or upload_only to check the image first)."
            )
        if "url" in data:
            image = await download(async_get_clientsession(hass), data["url"], NCP_IMAGE_MAX)
        else:
            path = data["path"]
            if not hass.config.is_allowed_path(path):
                raise ServiceValidationError(
                    f"{path} is outside the directories Home Assistant may read (allowlist_external_dirs)"
                )
            try:
                image = await hass.async_add_executor_job(_read_bytes, path, NCP_IMAGE_MAX)
            except (OSError, ValueError) as err:
                raise ServiceValidationError(f"cannot read {path}: {err}") from err
        digest = hashlib.sha256(image).hexdigest()
        if (want := data.get("sha256")) and want.lower() != digest:
            raise ServiceValidationError(f"the image's SHA-256 is {digest}, not {want}")
        result = await async_ncp_flash(
            hub,
            image,
            foreign=data["foreign"],
            force=data["force"],
            restore=data["restore"],
            upload_only=data["upload_only"],
        )
        return {**result, "sha256": digest, "size": len(image)}

    async_register_admin_service(
        hass,
        DOMAIN,
        SERVICE_NCP_UPDATE,
        ncp_update,
        NCP_UPDATE_SCHEMA,
        SupportsResponse.OPTIONAL,
    )
