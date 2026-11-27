"""Backups of the hub's network and data, kept in Home Assistant.

The hub exports its network as an Open ZigBee Coordinator Backup with the
keys (`network/backup`, device-support 14.6): network key, Trust Center
link key, the NCP's link keys and frame counters, the devices and the
hub's groups. The hub has no export of its other data (device names,
automations, settings): a backup also holds a snapshot of them read from
the API (`hub_data`), for reference; a restore puts back the network.

Files: `.storage/zigbee_rs_hub_backups/<entry id>/<UTC time>_<reason>.json`,
mode 0600 in a 0700 directory. Home Assistant's own backups include
`.storage`, so they carry these files too. Backups are taken on a schedule
(option `backup_interval`, hours), after the network changed (a device
joined or left, a channel change, a restore; option `backup_on_change`),
before a restore and an NCP update, and by `zigbee_rs_hub.backup_now`; the
newest `backup_keep` files are kept. Keys are never logged.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import contextlib
from datetime import datetime, timedelta
import json
import logging
import os
import re
from typing import TYPE_CHECKING, Any

import voluptuous as vol

from homeassistant.core import (
    CALLBACK_TYPE,
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.util import dt as dt_util

from .api import HubApiError, HubError
from .const import (
    ATTR_CONFIG_ENTRY_ID,
    BACKUP_INTERVAL_DEFAULT,
    BACKUP_KEEP_DEFAULT,
    CONF_BACKUP_INTERVAL,
    CONF_BACKUP_KEEP,
    CONF_BACKUP_ON_CHANGE,
    DOMAIN,
    SERVICE_BACKUP_NOW,
    SERVICE_RESTORE_BACKUP,
)

if TYPE_CHECKING:
    from .hub import ZigbeeRsHub

_LOGGER = logging.getLogger(__name__)

FORMAT = "zigbee_rs_hub/backup"
FORMAT_VERSION = 1
# `network/backup` is a job on the hub (the NCP's export).
BACKUP_TIMEOUT = 120.0
# Requests that stage devices, and the last one, which runs the restore.
RESTORE_STAGE_TIMEOUT = 30.0
RESTORE_TIMEOUT = 300.0
# Devices per `network/restore` request (as the hub's web UI).
RESTORE_BATCH = 8
# After a change, the rest of a burst (an interview, an import) first.
CHANGE_DELAY = 120.0
FIRST_CHECK = 300.0
CHECK_EVERY = timedelta(hours=1)
# A scheduled backup a little early rather than an interval late.
CHECK_SLACK = timedelta(minutes=10)
# `network_updated` reasons after which a backup holds something else.
CHANGE_REASONS = frozenset(
    {"channel_changed", "restored", "imported", "formed", "registry_adopted", "registry_cleared"}
)
# Backups the integration took of the running network (`replaced`: one the
# hub returned of the network a restore replaced).
OWN_REASONS = frozenset(
    {
        "manual",
        "scheduled",
        "change",
        "before_restore",
        "before_hub_update",
        "before_ncp_update",
    }
)
# Files the integration wrote (only these are pruned).
_FILE_NAME = re.compile(r"^\d{8}T\d{6}Z_[a-z_]+(-\d+)?\.json$")
# Read-only requests whose results go into `hub_data`.
_HUB_DATA_REQUESTS: tuple[tuple[str, str, dict[str, Any]], ...] = (
    ("time", "hub/time", {}),
    ("reporting_defaults", "reporting/defaults/get", {}),
    ("user_definitions", "definitions/list", {"source": "user"}),
)


def network_of(doc: Any) -> dict[str, Any] | None:
    """The OCB network backup in a file: one of ours, or a plain OCB file."""
    if not isinstance(doc, dict):
        return None
    if doc.get("format") == FORMAT:
        net = doc.get("network")
        return net if isinstance(net, dict) else None
    if isinstance(doc.get("metadata"), dict) and "network_key" in doc:
        return doc
    return None


def complete(net: dict[str, Any]) -> bool:
    """A backup with the network key, which can be restored."""
    key = net.get("network_key")
    internal = (net.get("metadata") or {}).get("internal") or {}
    return (
        isinstance(key, dict)
        and bool(key.get("key"))
        and not (isinstance(internal, dict) and internal.get("incomplete"))
    )


def summary(name: str, doc: dict[str, Any], size: int, mtime: float) -> dict[str, Any] | None:
    """What a backup file holds, without its keys."""
    net = network_of(doc)
    if net is None:
        return None
    created = doc.get("created") if doc.get("format") == FORMAT else None
    if not isinstance(created, str) or dt_util.parse_datetime(created) is None:
        created = dt_util.utc_from_timestamp(mtime).isoformat()
    devices = net.get("devices")
    return {
        "file": name,
        "created": created,
        "reason": doc.get("reason") if doc.get("format") == FORMAT else "imported",
        "size": size,
        "pan_id": net.get("pan_id"),
        "extended_pan_id": net.get("extended_pan_id"),
        "channel": net.get("channel"),
        "coordinator_ieee": net.get("coordinator_ieee"),
        "devices": len(devices) if isinstance(devices, list) else 0,
        "complete": complete(net),
    }


def _scan(directory: str) -> list[dict[str, Any]]:
    """The backups in `directory`, newest first."""
    try:
        names = os.listdir(directory)
    except FileNotFoundError:
        return []
    found = []
    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(directory, name)
        try:
            with open(path, "rb") as f:
                raw = f.read()
            doc = json.loads(raw)
            mtime = os.path.getmtime(path)
        except (OSError, ValueError):
            continue
        if isinstance(doc, dict) and (s := summary(name, doc, len(raw), mtime)) is not None:
            found.append(s)
    found.sort(key=lambda s: (s["created"], s["file"]), reverse=True)
    return found


def _order(name: str) -> tuple[str, int]:
    """Our file names in the order they were written: time, then the number of the second."""
    suffix = name[:-5].rpartition("-")[2]
    return name[:16], int(suffix) if suffix.isdigit() else 0


def _write(directory: str, stamp: str, reason: str, doc: dict[str, Any], keep: int) -> str:
    """Write `doc` (mode 0600) under a new name; prune; returns the name."""
    os.makedirs(directory, mode=0o700, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(directory, 0o700)
    own = [n for n in os.listdir(directory) if _FILE_NAME.match(n)]
    same = [_order(n)[1] for n in own if n[:16] == stamp]
    name = f"{stamp}_{reason}-{max(same) + 1}.json" if same else f"{stamp}_{reason}.json"
    path = os.path.join(directory, name)
    tmp = f"{path}.tmp"
    data = json.dumps(doc, indent=1).encode()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise

    own = sorted([*own, name], key=_order, reverse=True)
    for old in own[max(keep, 1) :]:
        with contextlib.suppress(OSError):
            os.remove(os.path.join(directory, old))
    return name


def _read(path: str) -> Any:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class BackupManager:
    """The backups of one hub."""

    def __init__(self, hass: HomeAssistant, hub: ZigbeeRsHub) -> None:
        """Backups of `hub`, in its own directory."""
        self.hass = hass
        self.hub = hub
        self.directory = hass.config.path(".storage", f"{DOMAIN}_backups", hub.entry.entry_id)
        # Summaries, newest first.
        self.backups: list[dict[str, Any]] = []
        self.last_attempt: datetime | None = None
        self.last_error: str | None = None
        self._lock = asyncio.Lock()
        self._listeners: list[Callable[[], None]] = []
        # The network changed since the last backup.
        self._dirty = False
        self._change_timer: CALLBACK_TYPE | None = None
        self._warned: str | None = None

    # Options (read at every use: no reload needed).

    @property
    def interval(self) -> timedelta | None:
        """Time between scheduled backups, `None` without them."""
        hours = self.hub.entry.options.get(CONF_BACKUP_INTERVAL, BACKUP_INTERVAL_DEFAULT)
        return timedelta(hours=hours) if hours else None

    @property
    def keep(self) -> int:
        """How many backup files are kept."""
        return int(self.hub.entry.options.get(CONF_BACKUP_KEEP, BACKUP_KEEP_DEFAULT))

    @property
    def on_change(self) -> bool:
        """Back up after the network changed."""
        return bool(self.hub.entry.options.get(CONF_BACKUP_ON_CHANGE, True))

    @property
    def last(self) -> dict[str, Any] | None:
        """The newest backup the integration took of the running network."""
        return next((b for b in self.backups if b["reason"] in OWN_REASONS), None)

    # Listeners.

    @callback
    def add_listener(self, cb: Callable[[], None]) -> Callable[[], None]:
        """`cb` runs after every backup attempt."""
        self._listeners.append(cb)
        return lambda: self._listeners.remove(cb)

    @callback
    def _notify(self) -> None:
        for cb in list(self._listeners):
            cb()

    # Schedule.

    async def async_load(self) -> None:
        """Read the summaries of the stored backups."""
        self.backups = await self.hass.async_add_executor_job(_scan, self.directory)

    @callback
    def async_start(self) -> list[CALLBACK_TYPE]:
        """The checks and the change listener; returns the cancellers."""
        return [
            async_call_later(self.hass, FIRST_CHECK, self._check),
            async_track_time_interval(self.hass, self._check, CHECK_EVERY),
            self.hub.add_topic_listener(self._on_event),
            self._cancel_change_timer,
        ]

    @callback
    def _cancel_change_timer(self) -> None:
        if self._change_timer is not None:
            self._change_timer()
            self._change_timer = None

    @callback
    def _on_event(self, event: dict[str, Any]) -> None:
        topic = event.get("topic")
        if topic in ("device_added", "device_removed") or (
            topic == "network_updated" and event.get("reason") in CHANGE_REASONS
        ):
            self._changed()

    @callback
    def _changed(self) -> None:
        if not self.on_change:
            return
        self._dirty = True
        if self._change_timer is None:
            self._change_timer = async_call_later(self.hass, CHANGE_DELAY, self._change_due)

    @callback
    def _change_due(self, _now: datetime) -> None:
        self._change_timer = None
        self._start("change")

    @callback
    def _check(self, _now: datetime | None = None) -> None:
        if self._dirty and self.on_change and self._change_timer is None:
            self._start("change")
            return
        if (interval := self.interval) is None:
            return
        last = self.last
        created = dt_util.parse_datetime(last["created"]) if last else None
        if created is None or dt_util.utcnow() - created >= interval - CHECK_SLACK:
            self._start("scheduled")

    @callback
    def _start(self, reason: str) -> None:
        if self._lock.locked():
            return
        self.hub.entry.async_create_background_task(
            self.hass, self._automatic(reason), f"{DOMAIN} {reason} backup"
        )

    async def _automatic(self, reason: str) -> None:
        if not self.hub.connected or self.hub.network.get("state") not in (None, "up"):
            # No network to back up (or not up: the NCP exports only then).
            return
        try:
            await self.async_backup(reason)
        except HomeAssistantError as err:
            if str(err) != self._warned:
                _LOGGER.warning("Backup of the zigbee-rs hub at %s failed: %s", self.hub.url, err)
            self._warned = str(err)
        else:
            self._warned = None

    # Backup.

    async def async_backup(self, reason: str) -> dict[str, Any]:
        """Take a backup (`network/backup` with keys) and keep it; its summary."""
        async with self._lock:
            return await self._backup(reason)

    async def _backup(self, reason: str) -> dict[str, Any]:
        hub = self.hub
        self.last_attempt = dt_util.utcnow()
        was_dirty, self._dirty = self._dirty, False
        try:
            client = hub.client
            if client is None or not hub.connected:
                raise HomeAssistantError(f"zigbee-rs hub at {hub.url} is not connected")
            try:
                ocb = await client.request("network/backup", timeout=BACKUP_TIMEOUT, keys=True)
            except HubError as err:
                raise HomeAssistantError(f"network/backup failed: {err}") from err
            if not isinstance(ocb, dict) or not complete(ocb):
                raise HomeAssistantError(
                    "the hub's backup has no network key (the NCP's key export is locked): "
                    "not kept, it could not be restored"
                )
            saved = await self._store(ocb, reason, await self._hub_data())
        except HomeAssistantError as err:
            self._dirty = self._dirty or was_dirty
            self.last_error = str(err)
            self._notify()
            raise
        self.last_error = None
        _LOGGER.debug("Backup of %s kept: %s", hub.url, saved["file"])
        self._notify()
        return saved

    async def _hub_data(self) -> dict[str, Any]:
        """The hub's other data, read from the API (not restorable through it)."""
        hub = self.hub
        data: dict[str, Any] = {
            "info": hub.info,
            "devices": list(hub.devices.values()),
            "automations": list(hub.automations.values()),
            "ota_settings": hub.ota_settings,
        }
        for key, req_type, params in _HUB_DATA_REQUESTS:
            if hub.client is None:
                break
            try:
                data[key] = await hub.client.request(req_type, **params)
            except HubError as err:
                _LOGGER.debug("%s for the backup: %s", req_type, err)
        return data

    def _identity(self) -> dict[str, Any]:
        hub = self.hub
        return {
            "entry_id": hub.entry.entry_id,
            "title": hub.entry.title,
            "name": hub.info.get("name"),
            "api_version": hub.info.get("api_version"),
            "coordinator_ieee": hub.coordinator_ieee,
            "ncp": hub.network.get("ncp"),
        }

    async def _store(
        self, ocb: dict[str, Any], reason: str, hub_data: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Write a backup file; its summary."""
        now = dt_util.utcnow()
        doc: dict[str, Any] = {
            "format": FORMAT,
            "version": FORMAT_VERSION,
            "created": now.isoformat(),
            "reason": reason,
            "hub": self._identity(),
            "network": ocb,
        }
        if hub_data is not None:
            doc["hub_data"] = hub_data
        try:
            name = await self.hass.async_add_executor_job(
                _write, self.directory, now.strftime("%Y%m%dT%H%M%SZ"), reason, doc, self.keep
            )
            self.backups = await self.hass.async_add_executor_job(_scan, self.directory)
        except (OSError, TypeError, ValueError) as err:
            raise HomeAssistantError(f"cannot write the backup to {self.directory}: {err}") from err
        found = next((b for b in self.backups if b["file"] == name), None)
        return found or {"file": name, "created": doc["created"], "reason": reason}

    # Restore.

    def path_of(self, file: str) -> str:
        """A file of this hub's backups, or a path Home Assistant may read."""
        if os.path.basename(file) == file:
            if file in ("", ".", ".."):
                raise ServiceValidationError(f"{file!r} is not a backup file name")
            return os.path.join(self.directory, file)
        if not self.hass.config.is_allowed_path(file):
            raise ServiceValidationError(
                f"{file} is outside the directories Home Assistant may read "
                "(allowlist_external_dirs); give a file name of the hub's backups instead"
            )
        return file

    async def async_restore(
        self,
        file: str,
        *,
        force: bool = False,
        confirm: bool = False,
        confirm_ieee_write_once: bool = False,
        allow_ieee_change: bool = True,
        skip_backup: bool = False,
    ) -> dict[str, Any]:
        """Put a backup's network onto the hub's NCP (`network/restore`)."""
        path = self.path_of(file)
        try:
            doc = await self.hass.async_add_executor_job(_read, path)
        except FileNotFoundError as err:
            raise ServiceValidationError(f"no backup {file}") from err
        except (OSError, ValueError) as err:
            raise ServiceValidationError(f"cannot read {file}: {err}") from err
        net = network_of(doc)
        if net is None:
            raise ServiceValidationError(
                f"{file} is not a network backup (Open ZigBee Coordinator Backup)"
            )
        if not complete(net):
            raise ServiceValidationError(f"{file} has no network key: it cannot be restored")
        hub = self.hub
        async with self._lock:
            if not skip_backup and hub.connected and hub.network.get("state") == "up":
                # The running network first: a restore replaces it.
                await self._backup("before_restore")
            client = hub.client
            if client is None or not hub.connected:
                raise HomeAssistantError(f"zigbee-rs hub at {hub.url} is not connected")
            devices = [d for d in net.get("devices") or [] if isinstance(d, dict)]
            flags: dict[str, Any] = {
                k: True
                for k, v in (
                    ("force", force),
                    ("confirm", confirm),
                    ("confirm_ieee_write_once", confirm_ieee_write_once),
                    ("skip_backup", skip_backup),
                )
                if v
            }
            if not allow_ieee_change:
                flags["allow_ieee_change"] = False
            last = len(devices) <= RESTORE_BATCH
            try:
                result = await client.request(
                    "network/restore",
                    timeout=RESTORE_TIMEOUT if last else RESTORE_STAGE_TIMEOUT,
                    backup={**net, "devices": devices[:RESTORE_BATCH]},
                    more=not last,
                    **flags,
                )
                for at in range(RESTORE_BATCH, len(devices), RESTORE_BATCH):
                    last = at + RESTORE_BATCH >= len(devices)
                    result = await client.request(
                        "network/restore",
                        timeout=RESTORE_TIMEOUT if last else RESTORE_STAGE_TIMEOUT,
                        devices=devices[at : at + RESTORE_BATCH],
                        more=not last,
                    )
            except HubApiError as err:
                # Failed after the leave: the hub answers the replaced
                # network's backup with the error. Never lose its keys.
                if err.backup is not None and complete(err.backup):
                    with contextlib.suppress(HomeAssistantError):
                        await self._store(err.backup, "replaced")
                    self._notify()
                raise HomeAssistantError(f"network/restore failed: {err}") from err
            except HubError as err:
                raise HomeAssistantError(f"network/restore failed: {err}") from err
            if not isinstance(result, dict):
                result = {}
            replaced = result.get("backup")
            kept = None
            if isinstance(replaced, dict) and complete(replaced):
                kept = (await self._store(replaced, "replaced"))["file"]
            self._notify()
        await hub.async_refresh_network()
        _LOGGER.info("Network restored on %s from %s", hub.url, file)
        return {**{k: v for k, v in result.items() if k != "backup"}, "replaced_backup": kept}

    def as_dict(self) -> dict[str, Any]:
        """Diagnostics (no keys)."""
        return {
            "directory": self.directory,
            "interval_h": self.interval.total_seconds() / 3600 if self.interval else None,
            "keep": self.keep,
            "on_change": self.on_change,
            "pending_change": self._dirty,
            "last_attempt": self.last_attempt.isoformat() if self.last_attempt else None,
            "last_error": self.last_error,
            "backups": self.backups,
        }


# Services.

BACKUP_NOW_SCHEMA = vol.Schema({vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string})
RESTORE_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Required("file"): cv.string,
        vol.Optional("force", default=False): cv.boolean,
        vol.Optional("confirm", default=False): cv.boolean,
        vol.Optional("confirm_ieee_write_once", default=False): cv.boolean,
        vol.Optional("allow_ieee_change", default=True): cv.boolean,
        vol.Optional("skip_backup", default=False): cv.boolean,
    }
)


def hubs_of_call(hass: HomeAssistant, call: ServiceCall) -> list[ZigbeeRsHub]:
    """The loaded hubs a call names (`config_entry_id`), all without one."""
    hubs = [e.runtime_data for e in hass.config_entries.async_loaded_entries(DOMAIN)]
    if entry_id := call.data.get(ATTR_CONFIG_ENTRY_ID):
        hubs = [h for h in hubs if h.entry.entry_id == entry_id]
    if not hubs:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_hub")
    return hubs


def hub_of_call(hass: HomeAssistant, call: ServiceCall) -> ZigbeeRsHub:
    """The one hub a call is for."""
    hubs = hubs_of_call(hass, call)
    if len(hubs) > 1:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="one_hub")
    return hubs[0]


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """`backup_now` and `restore_backup` (administrators: the files hold the keys)."""

    async def backup_now(call: ServiceCall) -> ServiceResponse:
        taken: dict[str, Any] = {}
        errors = []
        for hub in hubs_of_call(hass, call):
            try:
                taken[hub.entry.entry_id] = await hub.backups.async_backup("manual")
            except HomeAssistantError as err:
                errors.append(f"{hub.entry.title}: {err}")
        if errors:
            raise HomeAssistantError("; ".join(errors))
        return {"backups": taken}

    async def restore_backup(call: ServiceCall) -> ServiceResponse:
        hub = hub_of_call(hass, call)
        data = call.data
        return await hub.backups.async_restore(
            data["file"],
            force=data["force"],
            confirm=data["confirm"],
            confirm_ieee_write_once=data["confirm_ieee_write_once"],
            allow_ieee_change=data["allow_ieee_change"],
            skip_backup=data["skip_backup"],
        )

    async_register_admin_service(
        hass,
        DOMAIN,
        SERVICE_BACKUP_NOW,
        backup_now,
        BACKUP_NOW_SCHEMA,
        SupportsResponse.OPTIONAL,
    )
    async_register_admin_service(
        hass,
        DOMAIN,
        SERVICE_RESTORE_BACKUP,
        restore_backup,
        RESTORE_SCHEMA,
        SupportsResponse.OPTIONAL,
    )
