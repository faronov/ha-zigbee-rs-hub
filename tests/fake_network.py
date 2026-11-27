"""The fake hub with the maintenance, MQTT, groups and bindings API (14.6, 14.9, 14.10)."""

from __future__ import annotations

import asyncio
import copy
from typing import Any

from aiohttp import web

from .fake_hub import LIGHT_DEV, REMOTE_DEV, SENSOR_DEV, FakeHub

MAINTENANCE = ["diagnostics", "topology", "change_channel", "reset", "tx_power", "mqtt"]

# Short addresses of the fake network's devices.
NWK = {SENSOR_DEV: 0x1202, LIGHT_DEV: 0x7A10, REMOTE_DEV: 0x4242}

ENDPOINTS = {
    LIGHT_DEV: [
        {
            "ep": 11,
            "profile": 260,
            "device_id": 257,
            "in_clusters": [0, 3, 4, 5, 6, 8],
            "out_clusters": [],
        },
        {"ep": 12, "profile": 260, "device_id": 256, "in_clusters": [0, 6], "out_clusters": []},
        {"ep": 242, "profile": 41440, "device_id": 97, "in_clusters": [], "out_clusters": [33]},
    ],
    REMOTE_DEV: [
        {
            "ep": 1,
            "profile": 260,
            "device_id": 2048,
            "in_clusters": [0, 1, 3],
            "out_clusters": [3, 4, 6, 8],
        },
    ],
    SENSOR_DEV: [
        {
            "ep": 1,
            "profile": 260,
            "device_id": 263,
            "in_clusters": [0, 1, 1026, 1030],
            "out_clusters": [],
        },
    ],
}


def topology_map() -> dict[str, Any]:
    """A map: the light routes, the remote is the light's child, the sensor the coordinator's."""
    return {
        "time": 4000.0,
        "duration_ms": 1200,
        "nodes": [
            {
                "nwk": 0,
                "ieee": "00124b0001020304",
                "type": "coordinator",
                "device": None,
                "name": None,
                "scanned": True,
                "error": None,
            },
            {
                "nwk": NWK[LIGHT_DEV],
                "ieee": LIGHT_DEV[3:],
                "type": "router",
                "device": LIGHT_DEV,
                "name": "Dimmer",
                "scanned": True,
                "error": None,
            },
            {
                "nwk": NWK[REMOTE_DEV],
                "ieee": REMOTE_DEV[3:],
                "type": "end_device",
                "device": REMOTE_DEV,
                "name": "Remote",
                "scanned": False,
                "error": None,
            },
            {
                "nwk": NWK[SENSOR_DEV],
                "ieee": SENSOR_DEV[3:],
                "type": "end_device",
                "device": SENSOR_DEV,
                "name": "Motion sensor",
                "scanned": False,
                "error": None,
            },
        ],
        "links": [
            {
                "source": 0,
                "target": NWK[LIGHT_DEV],
                "lqi": 180,
                "relationship": "sibling",
                "depth": None,
            },
            {
                "source": 0,
                "target": NWK[SENSOR_DEV],
                "lqi": 150,
                "relationship": "child",
                "depth": None,
            },
            {
                "source": NWK[LIGHT_DEV],
                "target": NWK[REMOTE_DEV],
                "lqi": 120,
                "relationship": "child",
                "depth": 2,
            },
        ],
        "routes": [
            {"source": 0, "dst": NWK[REMOTE_DEV], "next_hop": NWK[LIGHT_DEV], "status": "active"},
        ],
        "notes": [],
    }


class _InfoProxy:
    """The socket for one `hub/info` answer: adds `maintenance`."""

    def __init__(self, ws: web.WebSocketResponse, extra: dict[str, Any]) -> None:
        self._ws = ws
        self._extra = extra

    async def send_json(self, data: dict[str, Any]) -> None:
        if data.get("success") and isinstance(data.get("result"), dict):
            data["result"].update(self._extra)
        await self._ws.send_json(data)


class NetworkFakeHub(FakeHub):
    """`FakeHub` plus network maps, settings, groups and bindings."""

    def __init__(self) -> None:
        """Every feature on; scriptable from tests."""
        super().__init__()
        self.api_version = "1.3"
        self.maintenance: list[str] = list(MAINTENANCE)
        # hub/info answers without `maintenance` this many times (the NCP
        # session is not up yet).
        self.info_without_maintenance = 0
        for dev_id, nwk in NWK.items():
            self.devices[dev_id]["nwk"] = nwk
        self.topology: dict[str, Any] | None = None
        # The map the next scan finds; `scan_error`: the job fails.
        self.next_map: dict[str, Any] = topology_map()
        self.scan_error: dict[str, str] | None = None
        self.job = 0
        self.mqtt: dict[str, Any] = {
            "enabled": False,
            "host": "",
            "port": 1883,
            "client_id": "",
            "username": "",
            "password_set": False,
            "base_topic": "zigbee2mqtt",
            "keepalive_s": 60,
            "discovery": False,
            "discovery_prefix": "homeassistant",
            "retain_state": False,
            "retain_availability": True,
            "qos": 0,
        }
        self.mqtt_password = ""
        # `None`: a hub without network/ncp_log (EZSP, before it existed).
        self.ncp_log: int | None = 3
        self.groups: dict[int, dict[str, Any]] = {}
        self.bindings: dict[str, dict[str, Any]] = {}
        # Devices that do not answer at once (sleepy: their requests are queued).
        self.sleepy: set[str] = {REMOTE_DEV, SENSOR_DEV}
        # Bind requests the device refuses: (device, cluster) → status.
        self.bind_errors: dict[tuple[str, int], str] = {}

    async def _answer(
        self, ws: web.WebSocketResponse, subs: dict[int, set[str]], req: dict[str, Any]
    ) -> None:
        rid = req.get("id")
        kind = str(req.get("type"))

        async def ok(result: Any = None) -> None:
            await ws.send_json({"type": "result", "id": rid, "success": True, "result": result})

        async def err(code: str, message: str) -> None:
            await ws.send_json(
                {
                    "type": "result",
                    "id": rid,
                    "success": False,
                    "error": {"code": code, "message": message},
                }
            )

        if kind == "hub/info" and self.info_without_maintenance > 0:
            self.info_without_maintenance -= 1
            await super()._answer(ws, subs, req)
        elif kind == "hub/info":
            await super()._answer(_InfoProxy(ws, {"maintenance": self.maintenance}), subs, req)
        elif kind == "network/topology" and "topology" in self.maintenance:
            await self._topology(ws, req, ok)
        elif kind == "network/job/result":
            if self.topology is None:
                await err("not_found", "no kept result")
            else:
                await ok(
                    {"job": self.job, "kind": "topology", "partial": False, "result": self.topology}
                )
        elif kind.startswith("mqtt/") and "mqtt" in self.maintenance:
            await self._mqtt(req, ok, err)
        elif kind == "network/ncp_log" and self.ncp_log is not None:
            if "level" in req:
                if not 0 <= req["level"] <= 5:
                    await err("invalid_value", "level: 0 to 5")
                    return
                self.ncp_log = req["level"]
            await ok({"level": self.ncp_log, "forwarded": 7, "suppressed": 0, "dropped": 0})
        elif kind == "network/ncp_log":
            await err("unsupported", "NCP log: the NCP firmware does not implement it")
        elif kind == "network/set_tx_power" and "tx_power" in self.maintenance:
            applied = max(-24, min(20, req["dbm"]))
            self.network["tx_power"] = applied
            self.network["tx_power_requested"] = req["dbm"]
            await ok({"requested_dbm": req["dbm"], "applied_dbm": applied})
            await self.push(
                "network", {"topic": "network_updated", "reason": "tx_power", "tx_power": applied}
            )
        elif kind == "network/change_channel" and "change_channel" in self.maintenance:
            self.network["channel"] = req["channel"]
            self.network["nwk_update_id"] += 1
            result = {"channel": req["channel"], "nwk_update_id": self.network["nwk_update_id"]}
            await ok(result)
            await self.push(
                "network", {"topic": "network_updated", "reason": "channel_changed", **result}
            )
        elif kind == "devices/get":
            dev = self.devices.get(req.get("device"))
            if dev is None:
                await err("not_found", f"no device {req.get('device')}")
            else:
                await ok(
                    {**copy.deepcopy(dev), "endpoints": copy.deepcopy(ENDPOINTS.get(dev["id"], []))}
                )
        elif kind.startswith("bindings/"):
            await self._bindings(req, ok, err)
        elif kind.startswith("groups/"):
            await self._groups(req, ok, err)
        else:
            await super()._answer(ws, subs, req)

    # Network map (a background job).

    async def _topology(self, ws: web.WebSocketResponse, req: dict[str, Any], ok) -> None:
        if req.get("refresh") is False:
            await ok(copy.deepcopy(self.topology))
            return
        self.job += 1
        job = self.job
        await ok({"job": job, "kind": "topology", "passes": 1})
        await asyncio.sleep(0.05)
        await self.push(
            "network",
            {
                "topic": "job_progress",
                "job": job,
                "kind": "topology",
                "progress": 0.5,
                "pass": 0,
                "passes": 1,
            },
        )
        if self.scan_error is not None:
            await ws.send_json(
                {
                    "type": "job_result",
                    "job": job,
                    "kind": "topology",
                    "partial": False,
                    "error": self.scan_error,
                }
            )
            await self.push(
                "network",
                {"topic": "job_failed", "job": job, "kind": "topology", "error": self.scan_error},
            )
            return
        self.topology = copy.deepcopy(self.next_map)
        await ws.send_json(
            {
                "type": "job_result",
                "job": job,
                "kind": "topology",
                "partial": False,
                "result": self.topology,
            }
        )
        await self.push(
            "network", {"topic": "job_done", "job": job, "kind": "topology", "cancelled": False}
        )

    async def finish_foreign_scan(self) -> None:
        """A scan another client started (the web UI): only `job_done` arrives here."""
        self.job += 1
        self.topology = copy.deepcopy(self.next_map)
        await self.push(
            "network",
            {"topic": "job_done", "job": self.job, "kind": "topology", "cancelled": False},
        )

    # MQTT bridge settings.

    async def _mqtt(self, req: dict[str, Any], ok, err) -> None:
        kind = req["type"]
        if kind == "mqtt/config/get":
            await ok(copy.deepcopy(self.mqtt))
        elif kind == "mqtt/status":
            state = (
                ("connected" if self.mqtt["host"] else "retrying")
                if self.mqtt["enabled"]
                else "disabled"
            )
            await ok(
                {
                    "state": state,
                    "last_error": None,
                    "retry_in_s": None,
                    "connects": 1,
                    "published": 5,
                    "received": 0,
                    "dropped": 0,
                    "errors": 0,
                    "devices": 3,
                    "tls": False,
                }
            )
        elif kind == "mqtt/config/set":
            new = {**self.mqtt, **{k: v for k, v in req.items() if k in self.mqtt}}
            if new["enabled"] and not new["host"]:
                await err("invalid_value", "host: required when enabled")
                return
            if "password" in req:
                self.mqtt_password = req["password"]
                new["password_set"] = bool(req["password"])
            self.mqtt = new
            await ok(copy.deepcopy(self.mqtt))
        else:
            await err("invalid_format", f"unknown variant `{kind}`")

    # Bindings (ZDO on the source device).

    def _binding(self, dev: str) -> dict[str, Any]:
        return self.bindings.setdefault(
            dev,
            {
                "device": dev,
                "read_at": None,
                "total": None,
                "reading": False,
                "error": None,
                "table": [],
                "pending": [],
            },
        )

    def confirm(self, dev: str) -> None:
        """The (sleepy) device woke up and answered its queued requests."""
        b = self._binding(dev)
        for p in list(b["pending"]):
            if p["state"] == "failed":
                continue
            entry = {k: p[k] for k in ("endpoint", "cluster", "target")}
            if (dev, p["cluster"]) in self.bind_errors:
                p["state"] = "failed"
                p["error"] = self.bind_errors[(dev, p["cluster"])]
                continue
            b["pending"].remove(p)
            if p["op"] == "bind" and entry not in b["table"]:
                b["table"].append(entry)
            elif p["op"] == "unbind" and entry in b["table"]:
                b["table"].remove(entry)
        if b["reading"]:
            b["reading"] = False
            b["read_at"] = 4100.0
            b["total"] = len(b["table"])
        for g in self.groups.values():
            for m in list(g["members"]):
                if m["device"] != dev:
                    continue
                if m["state"] == "pending":
                    m["state"], m["queued"] = "ok", False
                elif m["state"] == "removing":
                    g["members"].remove(m)

    def _queued(self, dev: str) -> bool:
        return dev in self.sleepy

    async def _bindings(self, req: dict[str, Any], ok, err) -> None:
        kind = req["type"]
        dev = req.get("device")
        if kind == "bindings/list":
            await ok([copy.deepcopy(b) for d, b in self.bindings.items() if dev in (None, d)])
            return
        if dev not in self.devices:
            await err("not_found", f"no device {dev}")
            return
        b = self._binding(dev)
        if kind == "bindings/read":
            b["reading"] = True
            await ok(copy.deepcopy(b))
            if not self._queued(dev):
                self.confirm(dev)
            return
        clusters = req.get("clusters") or [req.get("cluster")]
        ep = req.get("endpoint")
        eps = {e["ep"]: e for e in ENDPOINTS.get(dev, [])}
        if kind == "bindings/create":
            if ep not in eps:
                await err("invalid_value", "missing endpoint")
                return
            missing = [
                c for c in clusters if c not in eps[ep]["in_clusters"] + eps[ep]["out_clusters"]
            ]
            if missing:
                await err("invalid_value", f"endpoint {ep} has no cluster 0x{missing[0]:04X}")
                return
        op = "unbind" if kind == "bindings/delete" else "bind"
        for c in clusters:
            entry = {"endpoint": ep, "cluster": c, "target": req["target"]}
            if op == "bind" and entry in b["table"]:
                continue
            b["pending"] = [p for p in b["pending"] if {k: p[k] for k in entry} != entry]
            b["pending"].append(
                {
                    **entry,
                    "op": op,
                    "state": "queued" if self._queued(dev) else "pending",
                    "error": None,
                }
            )
        await ok(copy.deepcopy(b))
        if not self._queued(dev):
            # A mains device answers a moment later.
            asyncio.get_running_loop().call_later(0.2, self.confirm, dev)

    # Groups (ZCL Groups cluster on the members).

    async def _groups(self, req: dict[str, Any], ok, err) -> None:
        kind = req["type"]
        if kind == "groups/list":
            await ok(copy.deepcopy(list(self.groups.values())))
            return
        if kind == "groups/create":
            gid = req.get("group") or next(i for i in range(1, 0xFFF8) if i not in self.groups)
            if gid in self.groups:
                await err("invalid_value", f"group {gid} exists")
                return
            self.groups[gid] = {
                "id": gid,
                "name": req["name"],
                "device": f"grp:{gid:04x}",
                "hub_member": req.get("hub_member", True),
                "hub_error": None,
                "deleting": False,
                "entity": None,
                "members": [],
                "scenes": [],
            }
            await ok(copy.deepcopy(self.groups[gid]))
            return
        gid = req.get("group")
        if gid not in self.groups:
            await err("not_found", f"no group {gid}")
            return
        g = self.groups[gid]
        if kind == "groups/delete":
            if req.get("force") or not g["members"]:
                del self.groups[gid]
            else:
                g["deleting"] = True
            await ok(None)
            return
        dev, ep = req.get("device"), req.get("endpoint")
        eps = {e["ep"]: e for e in ENDPOINTS.get(dev, [])}
        if kind == "groups/add_member":
            if ep not in eps or 4 not in eps[ep]["in_clusters"]:
                await err("unsupported", f"{dev} endpoint {ep} has no Groups cluster (0x0004)")
                return
            queued = self._queued(dev)
            g["members"].append(
                {"device": dev, "endpoint": ep, "state": "pending", "queued": queued, "error": None}
            )
            await ok(copy.deepcopy(g))
            if not queued:
                asyncio.get_running_loop().call_later(0.2, self.confirm, dev)
        elif kind == "groups/remove_member":
            m = next((m for m in g["members"] if m["device"] == dev and m["endpoint"] == ep), None)
            if m is None:
                await err("not_found", f"{dev}/{ep} is not a member")
                return
            if req.get("force"):
                g["members"].remove(m)
            else:
                m["state"], m["queued"] = "removing", self._queued(dev)
                if not m["queued"]:
                    asyncio.get_running_loop().call_later(0.2, self.confirm, dev)
            await ok(copy.deepcopy(g))
        else:
            await err("invalid_format", f"unknown request {kind}")
