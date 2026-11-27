"""Bindings and groups from Home Assistant (device-support 14.9).

The hub sends the ZDO Bind/Unbind requests and the ZCL Groups commands; a
sleepy device gets them when it is next heard from (`queued`). The services
wait a few seconds for the device's answer and report each request's state.
"""

from __future__ import annotations

import asyncio
from typing import Any

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

# Cluster names the services take besides numbers (`6`, `"0x0006"`).
CLUSTERS: dict[str, int] = {
    "basic": 0x0000,
    "power_configuration": 0x0001,
    "identify": 0x0003,
    "groups": 0x0004,
    "scenes": 0x0005,
    "on_off": 0x0006,
    "level_control": 0x0008,
    "alarms": 0x0009,
    "door_lock": 0x0101,
    "window_covering": 0x0102,
    "thermostat": 0x0201,
    "fan_control": 0x0202,
    "color_control": 0x0300,
    "illuminance_measurement": 0x0400,
    "temperature_measurement": 0x0402,
    "pressure_measurement": 0x0403,
    "relative_humidity": 0x0405,
    "occupancy_sensing": 0x0406,
    "ias_zone": 0x0500,
    "metering": 0x0702,
    "electrical_measurement": 0x0B04,
}
GROUPS_CLUSTER = 0x0004
GROUP_MAX = 0xFFF7
# Endpoints that are not application endpoints (Green Power proxy, ZDO).
_SKIP_ENDPOINTS = (0, 242)
POLL = 0.5
DEFAULT_WAIT = 10.0


def cluster_id(value: Any) -> int:
    """A cluster ID from a number, `0x…` or a name of `CLUSTERS`."""
    if isinstance(value, bool):
        raise ServiceValidationError(f"not a cluster: {value}")
    if isinstance(value, int):
        cid = value
    else:
        text = str(value).strip().lower()
        if text in CLUSTERS:
            return CLUSTERS[text]
        try:
            cid = int(text, 16) if text.startswith("0x") else int(text)
        except ValueError as err:
            raise ServiceValidationError(
                f"not a cluster: {value!r} (a number, 0x…, or one of {', '.join(CLUSTERS)})"
            ) from err
    if not 0 <= cid <= 0xFFFF:
        raise ServiceValidationError(f"not a cluster: {value!r}")
    return cid


def cluster_ids(values: Any) -> list[int]:
    """Cluster IDs of one value or a list (comma-separated text too)."""
    if isinstance(values, str):
        values = [v for v in values.replace(",", " ").split() if v]
    elif not isinstance(values, (list, tuple)):
        values = [values]
    ids = list(dict.fromkeys(cluster_id(v) for v in values))
    if not ids:
        raise ServiceValidationError("no cluster given")
    return ids


def group_id(value: Any) -> int:
    """A group ID (1 to 0xFFF7) from a number or `0x…`."""
    try:
        gid = (
            int(value, 16)
            if isinstance(value, str) and value.lower().startswith("0x")
            else int(value)
        )
    except (TypeError, ValueError) as err:
        raise ServiceValidationError(f"not a group ID: {value!r}") from err
    if not 1 <= gid <= GROUP_MAX:
        raise ServiceValidationError(f"group IDs are 1 to 0x{GROUP_MAX:04X}, not {value!r}")
    return gid


async def endpoints(hub: Any, device: str) -> list[dict[str, Any]]:
    """The device's application endpoints (devices/get)."""
    rec = await hub.async_request("devices/get", device=device)
    eps = rec.get("endpoints") if isinstance(rec, dict) else None
    return [
        e
        for e in eps or []
        if isinstance(e, dict) and isinstance(e.get("ep"), int) and e["ep"] not in _SKIP_ENDPOINTS
    ]


def _clusters_of(ep: dict[str, Any], side: str) -> set[int]:
    return set(ep.get(f"{side}_clusters") or [])


async def source_endpoint(hub: Any, device: str, clusters: list[int]) -> int:
    """The source endpoint with all `clusters`: client ones (a remote) first."""
    eps = await endpoints(hub, device)
    want = set(clusters)
    for test in (
        lambda e: want <= _clusters_of(e, "out"),
        lambda e: want <= _clusters_of(e, "out") | _clusters_of(e, "in"),
    ):
        for ep in eps:
            if test(ep):
                return ep["ep"]
    raise ServiceValidationError(
        f"no endpoint of {device} has the clusters {', '.join(f'0x{c:04X}' for c in clusters)}: "
        "give the endpoint"
    )


async def target_endpoint(hub: Any, device: str, clusters: list[int]) -> int:
    """The target endpoint serving the clusters (all, else any)."""
    eps = await endpoints(hub, device)
    want = set(clusters)
    for test in (
        lambda e: want <= _clusters_of(e, "in"),
        lambda e: want & _clusters_of(e, "in"),
    ):
        for ep in eps:
            if test(ep):
                return ep["ep"]
    raise ServiceValidationError(
        f"no endpoint of {device} serves the clusters "
        f"{', '.join(f'0x{c:04X}' for c in clusters)}: give the target endpoint"
    )


async def group_endpoint(hub: Any, device: str) -> int:
    """The device's first endpoint with the Groups server cluster."""
    for ep in await endpoints(hub, device):
        if GROUPS_CLUSTER in _clusters_of(ep, "in"):
            return ep["ep"]
    raise ServiceValidationError(f"{device} has no endpoint with the Groups cluster (0x0004)")


def _same_target(a: Any, b: Any) -> bool:
    if not isinstance(a, dict) or not isinstance(b, dict):
        return False
    if "group" in a or "group" in b:
        return a.get("group") == b.get("group")
    if a.get("hub") or b.get("hub"):
        return bool(a.get("hub")) == bool(b.get("hub"))
    return a.get("device") == b.get("device") and a.get("endpoint") == b.get("endpoint")


def _state(record: dict[str, Any], entry: dict[str, Any], unbind: bool) -> dict[str, Any]:
    """The state of one requested (endpoint, cluster, target) in a binding record."""

    def same(e: Any) -> bool:
        return (
            isinstance(e, dict)
            and e.get("endpoint") == entry["endpoint"]
            and e.get("cluster") == entry["cluster"]
            and _same_target(e.get("target"), entry["target"])
        )

    for p in record.get("pending") or []:
        if same(p) and p.get("op") == ("unbind" if unbind else "bind"):
            return {**entry, "state": p.get("state") or "pending", "error": p.get("error")}
    in_table = any(same(e) for e in record.get("table") or [])
    if unbind:
        return {**entry, "state": "bound" if in_table else "unbound", "error": None}
    return {**entry, "state": "bound" if in_table else "pending", "error": None}


async def _binding_record(hub: Any, device: str) -> dict[str, Any]:
    records = await hub.async_request("bindings/list", device=device)
    for r in records if isinstance(records, list) else []:
        if isinstance(r, dict) and r.get("device") == device:
            return r
    return {"device": device, "table": [], "pending": []}


async def async_bind(
    hub: Any,
    device: str,
    endpoint: int | None,
    clusters: list[int],
    target: dict[str, Any],
    unbind: bool,
    wait: float = DEFAULT_WAIT,
) -> dict[str, Any]:
    """`bindings/create` (or `delete`), then the device's answers for `wait` s."""
    if endpoint is None:
        endpoint = await source_endpoint(hub, device, clusters)
    if "device" in target and target.get("endpoint") is None:
        target = {**target, "endpoint": await target_endpoint(hub, target["device"], clusters)}
    record = await hub.async_request(
        "bindings/delete" if unbind else "bindings/create",
        device=device,
        endpoint=endpoint,
        clusters=clusters,
        target=target,
    )
    entries = [{"endpoint": endpoint, "cluster": c, "target": target} for c in clusters]
    loop = asyncio.get_running_loop()
    until = loop.time() + wait
    while True:
        results = [_state(record, e, unbind) for e in entries]
        if not any(r["state"] == "pending" for r in results) or loop.time() >= until:
            break
        await asyncio.sleep(POLL)
        record = await _binding_record(hub, device)
    states = {r["state"] for r in results}
    status = next((s for s in ("failed", "pending", "queued") if s in states), "ok")
    return {
        "status": status,
        "queued": "queued" in states,
        "device": device,
        "endpoint": endpoint,
        "results": results,
        "binding": record,
    }


async def async_read_bindings(
    hub: Any, device: str, sleepy: bool, wait: float = DEFAULT_WAIT
) -> dict[str, Any]:
    """`bindings/read` (Mgmt_Bind_req pages); the table once the device answered."""
    record = await hub.async_request("bindings/read", device=device)
    loop = asyncio.get_running_loop()
    until = loop.time() + wait
    while isinstance(record, dict) and record.get("reading") and loop.time() < until:
        await asyncio.sleep(POLL)
        record = await _binding_record(hub, device)
    if not isinstance(record, dict):
        raise HomeAssistantError(f"bindings/read of {device}: no binding record")
    reading = bool(record.get("reading"))
    read_at = hub.hub_time(record.get("read_at"))
    return {
        "device": device,
        "reading": reading,
        # The record does not say whether a read waits in a sleepy device's
        # queue: a battery device that has not answered is taken as queued.
        "queued": reading and sleepy,
        "read_at": read_at.isoformat() if read_at else None,
        "total": record.get("total"),
        "error": record.get("error"),
        "table": record.get("table") or [],
        "pending": record.get("pending") or [],
    }


def _group(groups: Any, gid: int) -> dict[str, Any] | None:
    for g in groups if isinstance(groups, list) else []:
        if isinstance(g, dict) and g.get("id") == gid:
            return g
    return None


def _member(group: dict[str, Any] | None, device: str, endpoint: int) -> dict[str, Any] | None:
    for m in (group or {}).get("members") or []:
        if isinstance(m, dict) and m.get("device") == device and m.get("endpoint") == endpoint:
            return m
    return None


async def async_group_member(
    hub: Any,
    gid: int,
    device: str,
    endpoint: int | None,
    remove: bool,
    force: bool = False,
    wait: float = DEFAULT_WAIT,
) -> dict[str, Any]:
    """`groups/add_member` (or `remove_member`), then the member's answer for `wait` s."""
    if endpoint is None:
        if remove:
            groups = await hub.async_request("groups/list")
            members = [
                m
                for m in (_group(groups, gid) or {}).get("members") or []
                if m.get("device") == device
            ]
            if not members:
                raise ServiceValidationError(f"{device} is not a member of group {gid}")
            endpoint = members[0]["endpoint"]
        else:
            endpoint = await group_endpoint(hub, device)
    params: dict[str, Any] = {"group": gid, "device": device, "endpoint": endpoint}
    if remove and force:
        params["force"] = True
    group = await hub.async_request(
        "groups/remove_member" if remove else "groups/add_member", **params
    )
    busy = ("removing",) if remove else ("pending",)
    loop = asyncio.get_running_loop()
    until = loop.time() + wait
    member = _member(group, device, endpoint)
    while (
        member is not None
        and member.get("state") in busy
        and not member.get("queued")
        and loop.time() < until
    ):
        await asyncio.sleep(POLL)
        group = _group(await hub.async_request("groups/list"), gid)
        member = _member(group, device, endpoint)
    state = member.get("state") if member else ("removed" if remove else "missing")
    queued = bool(member and member.get("queued"))
    if member is not None and state in busy and not queued:
        status = "pending"
    elif state == "failed":
        status = "failed"
    else:
        status = "queued" if queued else "ok"
    return {
        "status": status,
        "queued": queued,
        "group": gid,
        "device": device,
        "endpoint": endpoint,
        "state": state,
        "error": (member or {}).get("error"),
        "group_record": group,
    }
