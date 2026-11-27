"""The hub's network map (`network/topology`, device-support 14.6).

A scan is a background job on the hub: the request answers `{job}` at once,
the map follows as a `job_result` frame to the session that started it (and
`job_done` to every `network` subscriber). The last map is kept here for
the diagnostics and the per-device parent sensors; a scan another client
started (the web UI's network map) is fetched when it is done.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import logging
from typing import Any

from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError

from .api import HubClient, HubError
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

KIND = "topology"
# The hub stops a map after 150 s (48 routers, 4 s per answer).
SCAN_TIMEOUT = 200.0
# Relationships of a neighbour-table entry that make its source the parent.
_CHILD = ("child", "unauthenticated_child")


class TopologyMap:
    """The last network map of one hub, and scans."""

    def __init__(self, hub: Any) -> None:
        """Map store of `hub` (a `ZigbeeRsHub`)."""
        self.hub = hub
        self.last: dict[str, Any] | None = None
        self._waiters: dict[int, asyncio.Future[dict[str, Any]]] = {}
        # `job_result` frames that came before their waiter.
        self._early: dict[int, dict[str, Any]] = {}
        self._own: set[int] = set()
        self._listeners: list[Callable[[], None]] = []
        self._nodes: dict[int, dict[str, Any]] = {}
        self._by_device: dict[str, dict[str, Any]] = {}
        self._parents: dict[int, dict[str, Any]] = {}
        self._routes: dict[int, dict[str, Any]] = {}

    @property
    def supported(self) -> bool:
        """The hub runs network maps (`topology` in hub/info `maintenance`)."""
        return KIND in (self.hub.info.get("maintenance") or [])

    @callback
    def add_listener(self, cb: Callable[[], None]) -> Callable[[], None]:
        """`cb` runs when the map changes."""
        self._listeners.append(cb)
        return lambda: self._listeners.remove(cb)

    async def async_connected(self, client: HubClient) -> None:
        """The last map the hub kept (none before the first scan)."""
        if not self.supported:
            return
        try:
            self._set(await client.request("network/topology", refresh=False))
        except HubError as err:
            _LOGGER.debug("network/topology failed: %s", err)

    @callback
    def _set(self, result: Any) -> None:
        if not isinstance(result, dict) or not isinstance(result.get("nodes"), list):
            return
        self.last = result
        nodes = [
            n for n in result["nodes"] if isinstance(n, dict) and isinstance(n.get("nwk"), int)
        ]
        self._nodes = {n["nwk"]: n for n in nodes}
        self._by_device = {n["device"]: n for n in nodes if isinstance(n.get("device"), str)}
        self._parents = {}
        for link in result.get("links") or []:
            if not isinstance(link, dict):
                continue
            src, dst, rel = link.get("source"), link.get("target"), link.get("relationship")
            if rel in _CHILD and isinstance(dst, int):
                # The parent's own table wins over the child's.
                self._parents[dst] = {
                    "nwk": src,
                    "lqi": link.get("lqi"),
                    "depth": link.get("depth"),
                }
            elif rel == "parent" and isinstance(src, int) and src not in self._parents:
                self._parents[src] = {
                    "nwk": dst,
                    "lqi": link.get("lqi"),
                    "depth": link.get("depth"),
                }
        self._routes = {
            r["dst"]: r
            for r in result.get("routes") or []
            if isinstance(r, dict) and r.get("source") == 0 and isinstance(r.get("dst"), int)
        }
        for cb in list(self._listeners):
            cb()

    def node(self, nwk: Any) -> dict[str, Any] | None:
        """The map's node with this short address."""
        return self._nodes.get(nwk) if isinstance(nwk, int) else None

    def node_of_device(self, device_id: str) -> dict[str, Any] | None:
        """The map's node of a hub device."""
        return self._by_device.get(device_id)

    def parent(self, device_id: str) -> dict[str, Any] | None:
        """`{nwk, lqi, depth}` of the device's parent in the map."""
        node = self.node_of_device(device_id)
        return self._parents.get(node["nwk"]) if node else None

    def route(self, device_id: str) -> dict[str, Any] | None:
        """The coordinator's route to the device: `{next_hop, status}`."""
        node = self.node_of_device(device_id)
        return self._routes.get(node["nwk"]) if node else None

    @callback
    def job_result(self, msg: dict[str, Any]) -> None:
        """A `job_result` frame (the session that started a scan gets it)."""
        if msg.get("kind") != KIND or msg.get("partial"):
            return
        job = msg.get("job")
        if not isinstance(job, int):
            return
        fut = self._waiters.pop(job, None)
        if fut is not None and not fut.done():
            fut.set_result(msg)
            return
        self._early[job] = msg
        while len(self._early) > 4:
            self._early.pop(next(iter(self._early)))

    @callback
    def job_finished(self, event: dict[str, Any]) -> None:
        """`job_done`/`job_failed`: another client's scan is fetched."""
        job = event.get("job")
        if event.get("kind") != KIND or event.get("topic") != "job_done" or job in self._own:
            return
        self.hub.entry.async_create_background_task(
            self.hub.hass, self._fetch(), f"{DOMAIN} network map"
        )

    async def _fetch(self) -> None:
        client = self.hub.client
        if client is None or not self.hub.connected:
            return
        try:
            self._set(await client.request("network/topology", refresh=False))
        except HubError as err:
            _LOGGER.debug("network/topology failed: %s", err)

    async def async_scan(self, refresh: bool = True, routes: bool = True) -> dict[str, Any]:
        """A new map (`refresh`), or the last one; raises `HomeAssistantError`."""
        if not self.supported:
            raise HomeAssistantError("this hub has no network map (topology)")
        if not refresh:
            result = await self.hub.async_request("network/topology", refresh=False)
            if not isinstance(result, dict):
                raise HomeAssistantError("the hub has no network map yet: scan first")
            self._set(result)
            return result
        client = self.hub.client
        started = await self.hub.async_request("network/topology", refresh=True, routes=routes)
        job = started.get("job") if isinstance(started, dict) else None
        if not isinstance(job, int):
            # An answer with the map itself.
            self._set(started)
            return started
        self._own.add(job)
        msg = self._early.pop(job, None)
        if msg is None:
            msg = await self._wait(client, job)
        error = msg.get("error")
        if isinstance(error, dict) or "result" not in msg:
            error = error if isinstance(error, dict) else {}
            raise HomeAssistantError(
                f"network/topology failed: {error.get('code', 'failed')}: {error.get('message', '')}"
            )
        self._set(msg["result"])
        return msg["result"]

    async def _wait(self, client: HubClient | None, job: int) -> dict[str, Any]:
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._waiters[job] = fut
        closed = asyncio.ensure_future(client.closed.wait()) if client else None
        try:
            waits: set[asyncio.Future[Any]] = {fut} | ({closed} if closed else set())
            await asyncio.wait(waits, timeout=SCAN_TIMEOUT, return_when=asyncio.FIRST_COMPLETED)
        finally:
            self._waiters.pop(job, None)
            if closed is not None:
                closed.cancel()
        if fut.done():
            return fut.result()
        fut.cancel()
        if closed is not None and closed.done():
            raise HomeAssistantError(
                "the connection to the hub was lost during the scan; the hub keeps the map "
                "(call the service again with refresh: false)"
            )
        # No frame (another session's job, a lost frame): the kept result.
        kept = await self.hub.async_request("network/job/result", job=job)
        if not isinstance(kept, dict):
            raise HomeAssistantError("network/topology: no result")
        return kept
