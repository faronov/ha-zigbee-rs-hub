"""Hub events on Home Assistant's bus, and the hub's repair issues.

The hub has no join, leave or announce events of its own (device-support
14.2): life-cycle events are derived from its `devices` topic, the catalog
status and `hub/system`, and fired as `zigbee_rs_hub_event` (logbook.py
describes them):

| `type`                | from                                                      |
|-----------------------|-----------------------------------------------------------|
| `device_joined`       | `device_added` of a device being interviewed              |
| `device_added`        | `device_added` otherwise (Green Power, imported)          |
| `device_left`         | the record's `unavailable_reason: "left"` appears         |
| `device_rejoined`     | it goes again, or the record's NWK address changed        |
| `device_removed`      | `device_removed`                                          |
| `interview_started`   | `interview_progress` of a device not being interviewed    |
| `interview_complete`  | the record's `interview` (after `in_progress`, `partial`) |
| `interview_partial`   | the record's `interview`                                  |
| `definition_resolved` | the record's `definition` layers (not while interviewed)  |
| `definition_removed`  | the same, back to generic only                            |
| `definition_missing`  | catalog/status `missing`: nothing to fetch, or refused    |
| `hub_crashed`         | hub/system `reset_reason`: panic, watchdog, brownout      |

A device's leave or rejoin is fired at most once a minute. Every
`entity_event` of an event entity is fired as `zigbee_rs_hub_action`
(`type` = the event type, `subtype` as in device_trigger.py).

Repair issues: the hub's last reset was a crash; a device without a
definition (generic only, or the catalog's matches need features this
firmware lacks); an interview partial for hours (fix: re-interview); a
device offline for a day or gone (left); the last catalog update failed
(fix: retry). They go when the condition does.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
import logging
import time
from typing import TYPE_CHECKING, Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.util import dt as dt_util

from . import const
from .api import HubError
from .const import DOMAIN, EVENT_ACTION, EVENT_HUB

if TYPE_CHECKING:
    from .hub import ZigbeeRsHub

_LOGGER = logging.getLogger(__name__)

# ESP-IDF `esp_reset_reason_t` values of a crash (hub/system `reset_reason`).
CRASH_REASONS = {
    4: "panic",
    5: "interrupt watchdog",
    6: "task watchdog",
    7: "watchdog",
    9: "brownout",
    15: "CPU lock-up",
}
# A device that keeps leaving and rejoining: one event a minute. The others
# are transitions the hub itself spaces out (interview resumptions).
_RATE_LIMITED = frozenset({"device_left", "device_rejoined"})
# Two boot times this close are the same boot (HA's clock minus `uptime_s`).
_SAME_BOOT = 120.0
# After an interview that left a device generic only: read the catalog's
# `missing` again (the hub announces only devices with shards to fetch).
_CATALOG_RECHECK = 5.0

ISSUE_HUB_CRASH = "hub_crash"
ISSUE_CATALOG_FAILED = "catalog_failed"
ISSUE_NO_DEFINITION = "no_definition"
ISSUE_DEFINITION_REFUSED = "definition_refused"
ISSUE_INTERVIEW_PARTIAL = "interview_partial"
ISSUE_DEVICE_OFFLINE = "device_offline"
ISSUE_DEVICE_LEFT = "device_left"
_DEVICE_ISSUES = (
    ISSUE_NO_DEFINITION,
    ISSUE_DEFINITION_REFUSED,
    ISSUE_INTERVIEW_PARTIAL,
    ISSUE_DEVICE_OFFLINE,
    ISSUE_DEVICE_LEFT,
)


def issue_id(kind: str, entry_id: str, device: str | None = None) -> str:
    """The repair issue ID of `kind` for a hub (and one of its devices)."""
    base = f"{kind}_{entry_id}"
    return base if device is None else f"{base}_{device.replace(':', '_')}"


@callback
def async_delete_entry_issues(hass: HomeAssistant, entry_id: str) -> None:
    """Delete every repair issue of a hub (its config entry is removed)."""
    reg = ir.async_get(hass)
    for domain, ident in list(reg.issues):
        if domain == DOMAIN and f"_{entry_id}" in ident:
            ir.async_delete_issue(hass, DOMAIN, ident)


def definition_of(record: dict[str, Any]) -> str | None:
    """ID of the device's top non-generic definition layer, `None` for generic only."""
    layers = record.get("definition")
    if not isinstance(layers, list):
        return None
    for layer in reversed(layers):
        if isinstance(layer, dict) and layer.get("source") != "generic" and layer.get("id"):
            return str(layer["id"])
    return None


def trigger_subtype(hub: ZigbeeRsHub, record: dict[str, Any]) -> str:
    """Device-trigger subtype of an event entity.

    Its key, with the endpoint when the device has the key on several.
    """
    key = str(record.get("key") or "action")
    same = [
        e
        for e in hub.entities.values()
        if e.get("device") == record.get("device")
        and e.get("platform") == "event"
        and e.get("key") == key
        and not e.get("orphaned")
    ]
    return key if len(same) <= 1 else f"{key}_{record.get('endpoint')}"


def _hours(delta: timedelta) -> str:
    return f"{delta.total_seconds() / 3600:g}"


def _local(when: datetime | None) -> str:
    return dt_util.as_local(when).strftime("%Y-%m-%d %H:%M") if when else "-"


def _radio_device(record: dict[str, Any]) -> bool:
    """A Zigbee or GP device (not a group device)."""
    return record.get("group") is None and not str(record.get("id", "")).startswith("grp:")


@dataclass(frozen=True, slots=True)
class _Facts:
    """What the events are derived from, per device."""

    name: str
    interview: str | None
    left: bool
    available: bool
    nwk: int | None
    definition: str | None

    @classmethod
    def of(cls, rec: dict[str, Any]) -> _Facts:
        nwk = rec.get("nwk")
        return cls(
            name=str(rec.get("name") or rec.get("model") or rec.get("id")),
            interview=rec.get("interview") if isinstance(rec.get("interview"), str) else None,
            left=rec.get("unavailable_reason") == "left",
            available=rec.get("available") is not False,
            nwk=nwk if isinstance(nwk, int) and not isinstance(nwk, bool) else None,
            definition=definition_of(rec),
        )


class HubMonitor:
    """Life-cycle events and repair issues of one hub."""

    def __init__(self, hass: HomeAssistant, hub: ZigbeeRsHub) -> None:
        """Monitor of `hub`."""
        self.hass = hass
        self.hub = hub
        self._devices: dict[str, _Facts] = {}
        # The definition before the running interview (none while joining).
        self._stable_definition: dict[str, str | None] = {}
        self._ha_ids: dict[str, str] = {}
        self._fired: dict[tuple[str, str], float] = {}
        # (issue kind, device) → since when its condition holds.
        self._since: dict[tuple[str, str], datetime] = {}
        self._catalog_issues: set[tuple[str, str]] = set()
        # Devices `definition_missing` was fired for (or that were missing
        # already at the first catalog status: no event for those).
        self._missing_fired: set[str] = set()
        self._missing_baseline = False
        self._issues: set[str] = set()
        self._catalog_recheck: CALLBACK_TYPE | None = None
        # The last hub/system answer.
        self.system: dict[str, Any] | None = None

    @property
    def _entry_id(self) -> str:
        return self.hub.entry.entry_id

    # Set-up.

    @callback
    def async_start(self) -> list[CALLBACK_TYPE]:
        """Follow the catalog and check the timed issues; returns the cancellers."""
        self._catalog_changed()
        return [
            self.hub.catalog.add_listener(self._catalog_changed),
            async_track_time_interval(self.hass, self._periodic, const.REPAIR_CHECK_INTERVAL),
            self._unloaded,
        ]

    @callback
    def _unloaded(self) -> None:
        if self._catalog_recheck is not None:
            self._catalog_recheck()
            self._catalog_recheck = None
        # The crash issue stays (persistent, its dismissal kept); the others
        # are found again after the next set-up.
        for ident in self._issues:
            ir.async_delete_issue(self.hass, DOMAIN, ident)
        self._issues.clear()

    async def async_connected(self) -> None:
        """A (re)connection: the hub's last reset reason (`hub/system`)."""
        try:
            system = await self.hub.async_request("hub/system")
        except HomeAssistantError as err:
            _LOGGER.debug("hub/system failed: %s", err)
            return
        if isinstance(system, dict):
            self.system = system
            self._check_crash(system)

    # Hub events (after the hub applied them).

    @callback
    def on_event(self, event: dict[str, Any]) -> None:
        """One event of the hub's subscription."""
        topic = event.get("topic")
        if topic == "snapshot":
            self._snapshot()
        elif topic in ("device_added", "device_updated"):
            rec = event.get("device")
            if isinstance(rec, dict) and isinstance(rec.get("id"), str):
                self._device(rec, topic == "device_added")
        elif topic == "device_removed":
            self._removed(str(event.get("id")))
        elif topic == "interview_progress":
            self._interview_progress(event)
        elif topic == "resolution_changed":
            self._resolution_changed(event)
        elif topic == "entity_event":
            self._action(event)

    @callback
    def _snapshot(self) -> None:
        # A (re)connection: the new baseline, without events.
        known = set(self._devices) | set(self._stable_definition)
        self._devices = {
            dev_id: _Facts.of(rec) for dev_id, rec in self.hub.devices.items() if _radio_device(rec)
        }
        for dev_id, facts in self._devices.items():
            if facts.interview != "in_progress":
                self._stable_definition[dev_id] = facts.definition
            self._ha_device_id(dev_id)
        for dev_id in known - set(self._devices):
            self._forget(dev_id)
        self._check_devices()

    @callback
    def _device(self, rec: dict[str, Any], added: bool) -> None:
        if not _radio_device(rec):
            return
        dev_id = rec["id"]
        new = _Facts.of(rec)
        old = self._devices.get(dev_id)
        self._devices[dev_id] = new
        self._ha_device_id(dev_id)
        if old is None:
            if added:
                joined = new.interview == "in_progress"
                self._fire(dev_id, "device_joined" if joined else "device_added")
            # A joining device's definition is reported once it is interviewed.
            if new.interview != "in_progress":
                self._stable_definition[dev_id] = new.definition
        else:
            self._compare(dev_id, old, new)
        self._check_device(dev_id)

    @callback
    def _compare(self, dev_id: str, old: _Facts, new: _Facts) -> None:
        if new.left and not old.left:
            self._fire(dev_id, "device_left")
        elif old.left and not new.left:
            self._fire(dev_id, "device_rejoined")
        elif old.nwk is not None and new.nwk is not None and old.nwk != new.nwk:
            self._fire(dev_id, "device_rejoined", nwk=f"0x{new.nwk:04X}")
        if new.interview != old.interview:
            if new.interview == "in_progress":
                self._fire(dev_id, "interview_started")
            elif new.interview == "partial":
                self._fire(dev_id, "interview_partial")
            elif new.interview == "complete" and old.interview in ("in_progress", "partial"):
                self._fire(dev_id, "interview_complete")
                if new.definition is None:
                    self._recheck_catalog()
        self._definition_changed(dev_id, new)

    @callback
    def _definition_changed(self, dev_id: str, facts: _Facts) -> None:
        # Not while it is interviewed: its layers come and go.
        if facts.interview == "in_progress":
            return
        old = self._stable_definition.get(dev_id)
        self._stable_definition[dev_id] = facts.definition
        if facts.definition == old:
            return
        if facts.definition is not None:
            self._missing_fired.discard(dev_id)
            self._fire(dev_id, "definition_resolved", definition=facts.definition, previous=old)
        elif old is not None:
            self._fire(dev_id, "definition_removed", previous=old)

    @callback
    def _removed(self, dev_id: str) -> None:
        if dev_id in self._devices:
            self._fire(dev_id, "device_removed")
        self._forget(dev_id)

    @callback
    def _forget(self, dev_id: str) -> None:
        self._devices.pop(dev_id, None)
        self._stable_definition.pop(dev_id, None)
        self._missing_fired.discard(dev_id)
        self._ha_ids.pop(dev_id, None)
        for key in [k for k in self._fired if k[0] == dev_id]:
            del self._fired[key]
        for kind in _DEVICE_ISSUES:
            self._since.pop((kind, dev_id), None)
            self._catalog_issues.discard((kind, dev_id))
            self._delete(issue_id(kind, self._entry_id, dev_id))

    @callback
    def _interview_progress(self, event: dict[str, Any]) -> None:
        dev_id = event.get("device")
        facts = self._devices.get(dev_id) if isinstance(dev_id, str) else None
        if facts is None or not isinstance(dev_id, str):
            return
        step = event.get("step")
        if step == "done" or facts.interview == "in_progress":
            return
        # The record says `in_progress` only with the next device_updated.
        self._devices[dev_id] = replace(facts, interview="in_progress")
        self._fire(dev_id, "interview_started", step=step if isinstance(step, str) else None)
        self._check_device(dev_id)

    @callback
    def _resolution_changed(self, event: dict[str, Any]) -> None:
        dev_id = event.get("device")
        facts = self._devices.get(dev_id) if isinstance(dev_id, str) else None
        if facts is None or not isinstance(dev_id, str):
            return
        new = replace(facts, definition=definition_of({"definition": event.get("layers")}))
        self._devices[dev_id] = new
        self._definition_changed(dev_id, new)

    @callback
    def _action(self, event: dict[str, Any]) -> None:
        uid = event.get("uid")
        rec = self.hub.entities.get(uid) if isinstance(uid, str) else None
        if rec is None or rec.get("platform") != "event":
            return
        dev_id = str(rec.get("device"))
        attributes = event.get("attributes")
        self.hass.bus.async_fire(
            EVENT_ACTION,
            {
                "device_id": self._ha_device_id(dev_id),
                "device": dev_id,
                "uid": uid,
                "entity_id": er.async_get(self.hass).async_get_entity_id("event", DOMAIN, uid),
                "type": str(event.get("event_type")),
                "subtype": trigger_subtype(self.hub, rec),
                "endpoint": rec.get("endpoint"),
                "attributes": attributes if isinstance(attributes, dict) else {},
            },
        )

    # Events on the bus.

    @callback
    def _registry_id(self, identifier: tuple[str, str]) -> str | None:
        dev = dr.async_get(self.hass).async_get_device(identifiers={identifier})
        return dev.id if dev else None

    @callback
    def _ha_device_id(self, dev_id: str) -> str | None:
        if dev_id in self._ha_ids:
            return self._ha_ids[dev_id]
        if (ident := self._registry_id((DOMAIN, dev_id))) is not None:
            self._ha_ids[dev_id] = ident
        return ident

    @callback
    def _name(self, dev_id: str, fallback: str) -> str:
        """The device's name in HA (the user's, else the hub's)."""
        ident = self._ha_device_id(dev_id)
        dev = dr.async_get(self.hass).async_get(ident) if ident else None
        return (dev.name_by_user or dev.name or fallback) if dev else fallback

    @callback
    def _fire(self, dev_id: str, kind: str, **extra: Any) -> None:
        if kind in _RATE_LIMITED:
            now = time.monotonic()
            last = self._fired.get((dev_id, kind))
            if last is not None and now - last < const.EVENT_DEDUPE.total_seconds():
                return
            self._fired[(dev_id, kind)] = now
        facts = self._devices.get(dev_id)
        self.hass.bus.async_fire(
            EVENT_HUB,
            {
                "device_id": self._ha_device_id(dev_id),
                "device": dev_id,
                "name": self._name(dev_id, facts.name if facts else dev_id),
                "type": kind,
                "config_entry_id": self._entry_id,
                **{k: v for k, v in extra.items() if v is not None},
            },
        )

    # Repair issues.

    @callback
    def _create(
        self,
        kind: str,
        device: str | None,
        placeholders: dict[str, str],
        *,
        fixable: bool = False,
    ) -> None:
        ident = issue_id(kind, self._entry_id, device)
        self._issues.add(ident)
        data: dict[str, str | int | float | None] = {"entry_id": self._entry_id}
        if device is not None:
            data["device"] = device
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            ident,
            data=data,
            is_fixable=fixable,
            severity=ir.IssueSeverity.WARNING,
            translation_key=kind,
            translation_placeholders={"hub": self.hub.entry.title, **placeholders},
        )

    @callback
    def _delete(self, ident: str) -> None:
        self._issues.discard(ident)
        ir.async_delete_issue(self.hass, DOMAIN, ident)

    @callback
    def _periodic(self, _now: datetime) -> None:
        # The records are stale while the hub is away.
        if self.hub.connected:
            self._check_devices()

    @callback
    def _check_devices(self) -> None:
        for dev_id in self._devices:
            self._check_device(dev_id)
        # Issues of devices that went while HA was not listening.
        prefixes = tuple(issue_id(kind, self._entry_id) + "_" for kind in _DEVICE_ISSUES)
        known = {
            issue_id(kind, self._entry_id, dev_id)
            for dev_id in self._devices
            for kind in _DEVICE_ISSUES
        }
        for domain, ident in list(ir.async_get(self.hass).issues):
            if domain == DOMAIN and ident.startswith(prefixes) and ident not in known:
                self._delete(ident)

    @callback
    def _check_device(self, dev_id: str) -> None:
        facts = self._devices.get(dev_id)
        rec = self.hub.devices.get(dev_id)
        if facts is None or rec is None:
            return
        enabled = rec.get("disabled") is not True
        now = dt_util.utcnow()
        name = {"name": self._name(dev_id, facts.name)}
        self._timed(
            ISSUE_INTERVIEW_PARTIAL,
            dev_id,
            enabled and facts.interview == "partial",
            const.REPAIR_INTERVIEW_PARTIAL,
            now,
            None,
            {**name, "hours": _hours(const.REPAIR_INTERVIEW_PARTIAL)},
            fixable=True,
        )
        self._timed(
            ISSUE_DEVICE_LEFT,
            dev_id,
            enabled and facts.left,
            const.REPAIR_DEVICE_LEFT,
            now,
            None,
            name,
        )
        last_seen = self.hub.hub_time(rec.get("last_seen"))
        self._timed(
            ISSUE_DEVICE_OFFLINE,
            dev_id,
            enabled and not facts.available and not facts.left,
            const.REPAIR_DEVICE_OFFLINE,
            now,
            last_seen,
            {
                **name,
                "hours": _hours(const.REPAIR_DEVICE_OFFLINE),
                "last_seen": _local(last_seen),
            },
        )

    @callback
    def _timed(
        self,
        kind: str,
        dev_id: str,
        active: bool,
        after: timedelta,
        now: datetime,
        since: datetime | None,
        placeholders: dict[str, str],
        *,
        fixable: bool = False,
    ) -> None:
        """Issue `kind` of a device once its condition held for `after`."""
        key = (kind, dev_id)
        ident = issue_id(kind, self._entry_id, dev_id)
        if not active:
            self._since.pop(key, None)
            self._delete(ident)
            return
        if key not in self._since:
            start = since or now
            # Shown before HA restarted: it still holds since then.
            if (issue := ir.async_get(self.hass).async_get_issue(DOMAIN, ident)) is not None:
                start = min(start, issue.created)
            self._since[key] = start
        if now - self._since[key] >= after:
            self._create(kind, dev_id, placeholders, fixable=fixable)

    @callback
    def restarted_interview(self, dev_id: str) -> None:
        """A re-interview was asked for: the partial interview's time starts over."""
        self._since.pop((ISSUE_INTERVIEW_PARTIAL, dev_id), None)
        self._delete(issue_id(ISSUE_INTERVIEW_PARTIAL, self._entry_id, dev_id))

    @callback
    def _check_crash(self, system: dict[str, Any]) -> None:
        reason = system.get("reset_reason")
        if not isinstance(reason, int) or isinstance(reason, bool):
            return  # a hub that does not tell (hubctl)
        ident = issue_id(ISSUE_HUB_CRASH, self._entry_id)
        crash = CRASH_REASONS.get(reason)
        if crash is None:
            # A clean restart since.
            ir.async_delete_issue(self.hass, DOMAIN, ident)
            return
        uptime = system.get("uptime_s")
        boot = (
            dt_util.utcnow() - timedelta(seconds=uptime)
            if isinstance(uptime, (int, float)) and not isinstance(uptime, bool)
            else None
        )
        boot_ts = boot.timestamp() if boot else None
        existing = ir.async_get(self.hass).async_get_issue(DOMAIN, ident)
        known = (existing.data or {}).get("boot") if existing else None
        same = existing is not None and (
            boot_ts is None
            or (isinstance(known, (int, float)) and abs(known - boot_ts) < _SAME_BOOT)
        )
        if same:
            return  # reported (and maybe dismissed) already
        if existing is not None:
            # Another crash: shown again even if the last one was dismissed.
            ir.async_delete_issue(self.hass, DOMAIN, ident)
        text = f"{crash} (reset reason {reason})"
        self.hass.bus.async_fire(
            EVENT_HUB,
            {
                "device_id": self._registry_id(self.hub.hub_identifier),
                "device": None,
                "name": self.hub.entry.title,
                "type": "hub_crashed",
                "config_entry_id": self._entry_id,
                "reason": text,
            },
        )
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            ident,
            data={"entry_id": self._entry_id, "boot": boot_ts},
            is_fixable=False,
            is_persistent=True,
            severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_HUB_CRASH,
            translation_placeholders={
                "hub": self.hub.entry.title,
                "reason": text,
                "boot": _local(boot),
            },
        )

    # Catalog.

    @callback
    def _recheck_catalog(self) -> None:
        if not self.hub.catalog.supported or self._catalog_recheck is not None:
            return

        @callback
        def recheck(_now: datetime) -> None:
            self._catalog_recheck = None
            self.hub.entry.async_create_background_task(
                self.hass, self._refresh_catalog(), f"{DOMAIN} catalog status"
            )

        self._catalog_recheck = async_call_later(self.hass, _CATALOG_RECHECK, recheck)

    async def _refresh_catalog(self) -> None:
        try:
            await self.hub.catalog.async_refresh()
        except HubError as err:
            _LOGGER.debug("catalog/status failed: %s", err)

    @callback
    def _catalog_changed(self) -> None:
        cat = self.hub.catalog
        failed = issue_id(ISSUE_CATALOG_FAILED, self._entry_id)
        if cat.last_error:
            self._create(ISSUE_CATALOG_FAILED, None, {"error": cat.last_error}, fixable=True)
        else:
            self._delete(failed)
        if cat.supported is False:
            present: set[tuple[str, str]] = set()
        elif cat.status is None:
            return
        else:
            present = self._missing_definitions(cat.status.get("missing"))
        for kind, dev_id in self._catalog_issues - present:
            self._delete(issue_id(kind, self._entry_id, dev_id))
        self._catalog_issues = present

    @callback
    def _missing_definitions(self, missing: Any) -> set[tuple[str, str]]:
        """Devices the catalog has no definition for (`missing` of catalog/status)."""
        present: set[tuple[str, str]] = set()
        for item in missing if isinstance(missing, list) else []:
            if not isinstance(item, dict):
                continue
            dev_id = item.get("device")
            rec = self.hub.devices.get(dev_id) if isinstance(dev_id, str) else None
            if rec is None or not isinstance(dev_id, str) or definition_of(rec) is not None:
                continue
            refused = [str(r) for r in item.get("refused") or [] if r]
            shards = item.get("shards")
            if refused:
                kind = ISSUE_DEFINITION_REFUSED
            elif isinstance(shards, list) and not shards and not item.get("recheck"):
                kind = ISSUE_NO_DEFINITION
            else:
                continue  # shards still to fetch
            present.add((kind, dev_id))
            placeholders = {
                "name": self._name(dev_id, str(rec.get("name") or rec.get("model") or dev_id)),
                "model": " ".join(
                    str(x) for x in (rec.get("manufacturer"), rec.get("model_id")) if x
                )
                or dev_id,
                "reason": "; ".join(refused),
            }
            if rec.get("disabled") is not True:
                self._create(kind, dev_id, placeholders)
            if dev_id not in self._missing_fired:
                self._missing_fired.add(dev_id)
                if self._missing_baseline:
                    self._fire(dev_id, "definition_missing", reason="; ".join(refused) or None)
        self._missing_baseline = True
        return present
