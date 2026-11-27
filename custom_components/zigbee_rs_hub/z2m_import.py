"""Import from Zigbee2MQTT (docs/migration.md).

Zigbee2MQTT's data directory becomes the hub's import document
(`zigbee-rs-hub/import` v1) as `hubctl import z2m` builds it
(tools/hubctl/src/import/z2m.rs): `database.db` (zigbee-herdsman's
database, one JSON object per line: devices and groups),
`coordinator_backup.json` (the Open Coordinator Backup herdsman writes) and
the friendly names of `configuration.yaml`'s `devices:`/`groups:` (or the
files they name). The hub takes only that document; the service previews
or applies it like `import_from_zha`, then gives the hub's new devices the
areas (and names set in HA) of Zigbee2MQTT's MQTT devices.

Not imported: bindings and reporting (they live on the devices), Green
Power devices (commissioned again), external converters and device options.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

import yaml

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.hassio import is_hassio

from . import migrate
from .const import DOMAIN
from .hub import find_device
from .zha_import import (
    APPLY_TIMEOUT,
    AREA_WAIT,
    PREVIEW_TIMEOUT,
    apply_areas,
    import_options,
    source_names,
)

_LOGGER = logging.getLogger(__name__)

NOTIFICATION = "zigbee_rs_hub_migration_z2m"
# The Zigbee2MQTT add-on's data directory (`data_path`), older and newer.
DEFAULT_DIRS = ("/share/zigbee2mqtt", "zigbee2mqtt")
# Supervisor add-on slugs (after the repository prefix) of Zigbee2MQTT.
ADDON_SLUGS = frozenset({"zigbee2mqtt", "zigbee2mqtt_edge"})

# herdsman's attribute names of the cached values the hub keeps
# (`hub_devices::persist::persistent_value`).
ATTRS: tuple[tuple[str, int, str, int], ...] = (
    ("genPowerCfg", 0x0001, "batterySize", 0x0031),
    ("genPowerCfg", 0x0001, "batteryQuantity", 0x0033),
    ("genPowerCfg", 0x0001, "batteryRatedVoltage", 0x0034),
    ("hvacThermostat", 0x0201, "minHeatSetpointLimit", 0x0015),
    ("hvacThermostat", 0x0201, "maxHeatSetpointLimit", 0x0016),
    ("hvacThermostat", 0x0201, "ctrlSeqeOfOper", 0x001B),
    ("hvacFanCtrl", 0x0202, "fanModeSequence", 0x0001),
    ("lightingColorCtrl", 0x0300, "colorCapabilities", 0x400A),
    ("lightingColorCtrl", 0x0300, "colorTempPhysicalMin", 0x400B),
    ("lightingColorCtrl", 0x0300, "colorTempPhysicalMax", 0x400C),
    ("seMetering", 0x0702, "unitOfMeasure", 0x0300),
    ("seMetering", 0x0702, "multiplier", 0x0301),
    ("seMetering", 0x0702, "divisor", 0x0302),
    ("seMetering", 0x0702, "summationFormatting", 0x0303),
    ("seMetering", 0x0702, "meteringDeviceType", 0x0306),
    ("haElectricalMeasurement", 0x0B04, "acVoltageMultiplier", 0x0600),
    ("haElectricalMeasurement", 0x0B04, "acVoltageDivisor", 0x0601),
    ("haElectricalMeasurement", 0x0B04, "acCurrentMultiplier", 0x0602),
    ("haElectricalMeasurement", 0x0B04, "acCurrentDivisor", 0x0603),
    ("haElectricalMeasurement", 0x0B04, "acPowerMultiplier", 0x0604),
    ("haElectricalMeasurement", 0x0B04, "acPowerDivisor", 0x0605),
)

POWER = (
    "unknown",
    "mains (single phase)",
    "mains (3 phase)",
    "battery",
    "dc source",
    "emergency mains constantly powered",
    "emergency mains and transfer switch",
)

KINDS = {
    "Router": "router",
    "EndDevice": "end_device",
    "Coordinator": "coordinator",
    "GreenPower": "green_power",
}

_RENAMED = (
    ("app_version", "appVersion"),
    ("stack_version", "stackVersion"),
    ("hw_version", "hwVersion"),
    ("zcl_version", "zclVersion"),
    ("date_code", "dateCode"),
    ("sw_build_id", "swBuildId"),
)


class Names:
    """Friendly names by IEEE (big-endian hex) and by group ID."""

    def __init__(self) -> None:
        """No names."""
        self.devices: dict[str, str] = {}
        self.groups: dict[int, str] = {}


def _uint(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None


def _power_source(v: Any) -> int | None:
    if (n := _uint(v)) is not None:
        return n if n <= 0xFF else None
    if isinstance(v, str) and v.strip().lower() in POWER:
        return POWER.index(v.strip().lower())
    return None


def device(e: dict[str, Any], names: Names) -> dict[str, Any] | None:
    """One `database.db` device entry in the document's form."""
    ieee = migrate.ieee_hex(e.get("ieeeAddr"))
    if ieee is None:
        return None
    o: dict[str, Any] = {"ieee": ieee}

    def put(key: str, value: Any) -> None:
        if value is not None:
            o[key] = value

    put("nwk", e.get("nwkAddr"))
    put("name", names.devices.get(ieee))
    put("type", KINDS.get(e["type"]) if isinstance(e.get("type"), str) else None)
    put("manufacturer", e.get("manufName"))
    put("model", e.get("modelId"))
    put("manufacturer_code", e.get("manufId"))
    put("power_source", _power_source(e.get("powerSource")))
    for key, z2m in _RENAMED:
        put(key, e.get(z2m))
    eps: list[tuple[int, dict[str, Any]]] = []
    attrs: list[dict[str, Any]] = []
    endpoints = e.get("endpoints")
    # In key order, as hubctl reads them (serde_json's sorted maps).
    for k, ep in sorted((endpoints if isinstance(endpoints, dict) else {}).items()):
        if not isinstance(ep, dict):
            continue
        ep_id = _uint(ep.get("epId"))
        if ep_id is None and re.fullmatch(r"\+?\d+", str(k)):
            ep_id = int(k)
        if ep_id is None:
            continue

        def clusters(key: str, ep: dict[str, Any] = ep) -> list[Any]:
            value = ep.get(key)
            return value if isinstance(value, list) else []

        eps.append(
            (
                ep_id,
                {
                    "id": ep_id,
                    "profile": ep.get("profId"),
                    "device_id": ep.get("devId"),
                    "in": clusters("inClusterList"),
                    "out": clusters("outClusterList"),
                },
            )
        )
        cache = ep.get("clusters") if isinstance(ep.get("clusters"), dict) else {}
        for cname, cid, aname, aid in ATTRS:
            c = cache.get(cname)
            if not isinstance(c, dict):
                continue
            # `attrs` in databases of herdsman before 0.14.
            a = c["attributes"] if "attributes" in c else c.get("attrs")
            v = a.get(aname) if isinstance(a, dict) else None
            if isinstance(v, (int, float, str)) and not isinstance(v, bool):
                attrs.append({"ep": ep_id, "cluster": cid, "attr": aid, "value": v})
    eps.sort(key=lambda x: x[0])
    o["endpoints"] = [x[1] for x in eps]
    if attrs:
        o["attributes"] = attrs
    # herdsman ≥ 3 keeps `interviewState`, older ones `interviewCompleted`.
    if isinstance(e.get("interviewState"), str):
        o["interviewed"] = e["interviewState"] == "SUCCESSFUL"
    elif isinstance(e.get("interviewCompleted"), bool):
        o["interviewed"] = e["interviewCompleted"]
    put("source_id", e.get("id"))
    return o


def group(e: dict[str, Any], names: Names) -> dict[str, Any] | None:
    """One `database.db` group entry."""
    gid = _uint(e.get("groupID"))
    if gid is None or gid > 0xFFFF:
        return None
    members = []
    for m in e.get("members") if isinstance(e.get("members"), list) else []:
        if not isinstance(m, dict) or "endpointID" not in m:
            continue
        if (ieee := migrate.ieee_hex(m.get("deviceIeeeAddr"))) is not None:
            members.append({"ieee": ieee, "ep": m["endpointID"]})
    return {"id": gid, "name": names.groups.get(gid), "members": members}


def database(
    text: str, names: Names
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None, int]:
    """Devices, groups, the coordinator's IEEE and the lines skipped (not JSON, not a device)."""
    devices: list[dict[str, Any]] = []
    groups: list[dict[str, Any]] = []
    coordinator = None
    bad = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except ValueError:
            bad += 1
            continue
        kind = e.get("type") if isinstance(e, dict) else None
        if kind == "Group":
            if (g := group(e, names)) is not None:
                groups.append(g)
        elif kind == "Coordinator":
            coordinator = migrate.ieee_hex(e.get("ieeeAddr"))
        elif isinstance(kind, str):
            if (d := device(e, names)) is not None:
                devices.append(d)
            else:
                bad += 1
        else:
            bad += 1
    return devices, groups, coordinator, bad


class _Loader(yaml.SafeLoader):
    """Zigbee2MQTT's YAML (1.1); its tags (`!secret`) read as null."""


_Loader.add_multi_constructor("!", lambda _loader, _suffix, _node: None)


def _friendly(v: Any) -> str | None:
    name = v.get("friendly_name") if isinstance(v, dict) else None
    if isinstance(name, bool):
        return None
    return str(name) if isinstance(name, (str, int)) else None


def _device_names(y: Any, out: Names) -> None:
    for k, v in (y if isinstance(y, dict) else {}).items():
        # An unquoted 0x… key is a YAML integer.
        if isinstance(k, int) and not isinstance(k, bool):
            ieee = f"{k:016x}" if 0 <= k < 1 << 64 else None
        else:
            ieee = migrate.ieee_hex(k) if isinstance(k, str) else None
        if ieee is not None and (name := _friendly(v)) is not None:
            out.devices[ieee] = name


def _group_names(y: Any, out: Names) -> None:
    for k, v in (y if isinstance(y, dict) else {}).items():
        gid = None
        if isinstance(k, (int, str)) and not isinstance(k, bool):
            text = str(k)
            if re.fullmatch(r"\+?\d+", text) and int(text) <= 0xFFFF:
                gid = int(text)
        if gid is None and isinstance(v, dict) and (i := _uint(v.get("ID"))) is not None:
            gid = i if i <= 0xFFFF else None
        if gid is not None and (name := _friendly(v)) is not None:
            out.groups[gid] = name


def _yaml(path: str) -> Any:
    with open(path, encoding="utf-8") as f:
        return yaml.load(f, Loader=_Loader)


def names(path: str, out: Names) -> None:
    """Names of `configuration.yaml`: its `devices:`/`groups:`, inline or in the files they name."""
    conf = _yaml(path)
    if not isinstance(conf, dict):
        return
    base = os.path.dirname(path)
    for key, read in (("devices", _device_names), ("groups", _group_names)):
        value = conf.get(key)
        if isinstance(value, dict):
            read(value, out)
            continue
        files = [value] if isinstance(value, str) else value if isinstance(value, list) else []
        for f in files:
            if isinstance(f, str):
                read(_yaml(os.path.join(base, f)), out)


def read_z2m(path: str) -> tuple[dict[str, Any], int]:
    """The import document of a Zigbee2MQTT data directory, and the database lines skipped."""
    db_path = os.path.join(path, "database.db")
    with open(db_path, encoding="utf-8") as f:
        text = f.read()
    found = Names()
    conf = os.path.join(path, "configuration.yaml")
    if os.path.exists(conf):
        try:
            names(conf, found)
        except yaml.YAMLError as err:
            raise ValueError(f"{conf}: {err}") from err
    network = None
    backup = os.path.join(path, "coordinator_backup.json")
    if os.path.exists(backup):
        with open(backup, encoding="utf-8") as f:
            network = json.load(f)
        meta = network.get("metadata") if isinstance(network, dict) else None
        if not isinstance(meta, dict) or meta.get("format") != migrate.OCB:
            raise ValueError(f"{backup}: not an Open Coordinator Backup")
    devices, groups, _, bad = database(text, found)
    source = (network or {}).get("metadata", {}).get("source")
    return (
        {
            "format": migrate.FORMAT,
            "version": 1,
            "source": {
                "kind": "z2m",
                "name": f"Zigbee2MQTT ({source})" if isinstance(source, str) else "Zigbee2MQTT",
            },
            "network": network,
            "devices": devices,
            "groups": groups,
        },
        bad,
    )


def data_dir(hass: HomeAssistant, path: str | None) -> str:
    """The data directory given, else the first default one with a `database.db`."""
    if path:
        return path
    for d in DEFAULT_DIRS:
        p = d if os.path.isabs(d) else hass.config.path(d)
        if os.path.exists(os.path.join(p, "database.db")):
            return p
    return hass.config.path(DEFAULT_DIRS[-1])


def z2m_running(hass: HomeAssistant) -> str | None:
    """Why Zigbee2MQTT looks to be running, if it does.

    Its Supervisor add-on started, or its bridge device (MQTT discovery,
    not the hub's own bridge) connected.
    """
    if is_hassio(hass):
        try:
            from homeassistant.components.hassio import get_addons_list  # noqa: PLC0415

            addons = get_addons_list(hass)
        except (ImportError, HomeAssistantError):
            addons = []
        for a in addons:
            slug = str(a.get("slug", ""))
            if slug.split("_", 1)[-1] in ADDON_SLUGS and str(a.get("state")) in (
                "started",
                "startup",
            ):
                return f"the {a.get('name') or slug} add-on is running"
    devices = dr.async_get(hass)
    entities = er.async_get(hass)
    for entry in hass.config_entries.async_entries("mqtt"):
        for d in dr.async_entries_for_config_entry(devices, entry.entry_id):
            if d.manufacturer == "zigbee-rs" or not any(
                domain == "mqtt" and ident.startswith("zigbee2mqtt_bridge_")
                for domain, ident in d.identifiers
            ):
                continue
            for e in er.async_entries_for_device(entities, d.id):
                state = hass.states.get(e.entity_id)
                if (
                    e.domain == "binary_sensor"
                    and state is not None
                    and state.state == "on"
                    and (e.device_class or e.original_device_class) == "connectivity"
                ):
                    return f"its bridge {d.name_by_user or d.name} is connected (MQTT)"
    return None


async def async_import_from_z2m(
    hass: HomeAssistant, hub: Any, data: dict[str, Any]
) -> dict[str, Any]:
    """The `import_from_z2m` service: preview, or apply when `apply`."""
    path = data_dir(hass, data.get("path"))
    db = os.path.join(path, "database.db")
    if not await hass.async_add_executor_job(os.path.exists, db):
        raise ServiceValidationError(
            f"no Zigbee2MQTT database at {db}: give the data directory as path"
        )
    apply = data.get("apply", False)
    if apply and (why := z2m_running(hass)):
        raise ServiceValidationError(
            f"Zigbee2MQTT is running ({why}): stop it first (its add-on or container) and keep "
            "it stopped. Two coordinators must never run one network."
        )
    try:
        doc, skipped = await hass.async_add_executor_job(read_z2m, path)
    except (OSError, ValueError) as err:
        raise HomeAssistantError(f"cannot read {path}: {err}") from err
    if doc["network"] is None:
        raise ServiceValidationError(
            f"no coordinator_backup.json in {path}: Zigbee2MQTT writes it when it stops "
            "(stop it once, then call the service again)"
        )
    timeout = APPLY_TIMEOUT if apply else PREVIEW_TIMEOUT

    async def request(req_type: str, **params: Any) -> Any:
        return await hub.async_request(req_type, timeout=timeout, **params)

    req_type = "network/import" if apply else "network/import/preview"
    result, rows = await migrate.send(request, req_type, doc, import_options(data))
    result.pop("backup", None)
    answer: dict[str, Any] = {
        "path": path,
        "skipped_lines": skipped,
        "result": result,
        "rows": rows,
    }
    if apply and result.get("stored") is False:
        raise HomeAssistantError(
            "the hub imported the devices but could not store them (its device registry is "
            "not writable): they are lost at its next restart; check network/info's registry "
            "and import again"
        )
    if not apply:
        net = result.get("network", {})
        s = result.get("summary", {})
        persistent_notification.async_create(
            hass,
            f"Preview of the import from Zigbee2MQTT ({path}): network {net.get('ext_pan_id')} "
            f"({net.get('action')}, needs {', '.join(net.get('needs', [])) or 'nothing'}), "
            f"{s.get('new', 0)} new, {s.get('replace', 0)} replaced, {s.get('keep', 0)} kept, "
            f"{s.get('skip', 0)} skipped; restore check: {result.get('restore_check')}. "
            "Stop Zigbee2MQTT, then call the action again with apply.",
            title="zigbee-rs hub: migration from Zigbee2MQTT",
            notification_id=NOTIFICATION,
        )
        return answer
    # Areas, and names set in HA, of Zigbee2MQTT's MQTT devices.
    names_areas = source_names(hass, ("mqtt",))
    areas = {i: a for i, (_, a) in names_areas.items() if a}
    answer["areas"] = await apply_areas(hass, hub, areas, overwrite=False, wait=AREA_WAIT)
    answer["names"] = _apply_names(hass, hub, {i: n for i, (n, _) in names_areas.items() if n})
    d = result.get("devices", {})
    persistent_notification.async_create(
        hass,
        f"Imported from Zigbee2MQTT: {d.get('new', 0)} new, {d.get('replace', 0)} replaced, "
        f"{d.get('keep', 0)} kept, {d.get('skip', 0)} skipped; {result.get('groups', 0)} groups. "
        "Devices are verified when they next talk (sleepy ones when they wake). "
        "Keep Zigbee2MQTT stopped; once the devices work here, remove it (and its MQTT "
        "devices in Home Assistant).",
        title="zigbee-rs hub: migration from Zigbee2MQTT",
        notification_id=NOTIFICATION,
    )
    return answer


def _apply_names(hass: HomeAssistant, hub: Any, names: dict[str, str]) -> int:
    """Names set in HA on the source's devices, for the hub's devices without one."""
    reg = dr.async_get(hass)
    done = 0
    for ieee, name in names.items():
        dev = find_device(reg, (DOMAIN, f"zb:{ieee}"), hub.entry.entry_id)
        if dev is None or dev.name_by_user:
            continue
        reg.async_update_device(dev.id, name_by_user=name)
        done += 1
    return done
