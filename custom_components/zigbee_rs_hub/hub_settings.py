"""Hub settings Home Assistant can change (device-support 14.6, 14.10).

Each part only where the hub has the API: the MQTT bridge (`mqtt` in
hub/info `maintenance`; the password is write-only, `password_set` tells
whether one is stored), the Zigbee TX power (`tx_power`), the channel
(`change_channel`; a select stages the channel, the admin service
`change_channel` with `confirm` moves the network) and the NCP's log level
(`network/ncp_log`, ZRHP NCPs of hubs that have it: probed on connect).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import logging
from typing import Any

from homeassistant.core import callback

from .api import HubClient, HubError
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

NCP_LOG_LEVELS = ["off", "error", "warn", "info", "debug", "trace"]
# network/info `tx_power` while the NCP reports it unknown (0x7F).
TX_POWER_UNKNOWN = 127
# Antenna-referred limits by network/info `ncp.chip` (the NCP clamps and
# rounds down to its radio's steps); other radios: the widest.
TX_POWER_RANGE: dict[int, tuple[int, int]] = {1: (-24, 20), 2: (-15, 20)}
TX_POWER_DEFAULT_RANGE = (-24, 20)
# A channel change answers once the NCP tuned (the switch is ≈ 9 s later).
CHANNEL_TIMEOUT = 60.0
SETTINGS_TIMEOUT = 30.0
# hub/info `maintenance` appears once the NCP session is up (API 1.2): how
# long a hub that has not got there yet is asked again, once a second.
MAINTENANCE_WAIT = 120


def _api_at_least(version: Any, major: int, minor: int) -> bool:
    try:
        return tuple(int(x) for x in str(version).split(".")[:2]) >= (major, minor)
    except ValueError:
        return False


class HubSettings:
    """Cached settings of one hub."""

    def __init__(self, hub: Any) -> None:
        """Settings of `hub` (a `ZigbeeRsHub`)."""
        self.hub = hub
        self.mqtt: dict[str, Any] | None = None
        self.mqtt_status: dict[str, Any] | None = None
        self.ncp_log: dict[str, Any] | None = None
        # The channel the select staged for `change_channel`.
        self.channel_target: int | None = None
        self._listeners: list[Callable[[], None]] = []

    def _has(self, feature: str) -> bool:
        return feature in (self.hub.info.get("maintenance") or [])

    @property
    def supports_mqtt(self) -> bool:
        """The hub has the MQTT bridge."""
        return self._has("mqtt")

    @property
    def supports_tx_power(self) -> bool:
        """The NCP sets its TX power."""
        return self._has("tx_power")

    @property
    def supports_channel_change(self) -> bool:
        """The hub moves the network to another channel."""
        return self._has("change_channel")

    @property
    def supports_ncp_log(self) -> bool:
        """`network/ncp_log` answered on the last connect."""
        return self.ncp_log is not None

    @callback
    def add_listener(self, cb: Callable[[], None]) -> Callable[[], None]:
        """`cb` runs when a setting changes."""
        self._listeners.append(cb)
        return lambda: self._listeners.remove(cb)

    @callback
    def _notify(self) -> None:
        for cb in list(self._listeners):
            cb()

    async def async_connected(self, client: HubClient) -> None:
        """Read the settings the hub has; a failure leaves a part out.

        A hub whose NCP session is not up yet has no `maintenance` in
        hub/info: it is asked again until it has, then the settings (and the
        network map) are read and the entities that need them are added.
        """
        await self._load(client)
        if "maintenance" not in self.hub.info and _api_at_least(
            self.hub.info.get("api_version"), 1, 2
        ):
            self.hub.entry.async_create_background_task(
                self.hub.hass, self._await_maintenance(client), f"{DOMAIN} maintenance"
            )

    async def _await_maintenance(self, client: HubClient) -> None:
        for _ in range(MAINTENANCE_WAIT):
            await asyncio.sleep(1)
            if client.closed.is_set() or client is not self.hub.client:
                return
            try:
                info = await client.request("hub/info")
            except HubError:
                return
            if isinstance(info, dict) and isinstance(info.get("maintenance"), list):
                self.hub.info["maintenance"] = info["maintenance"]
                await self._load(client)
                await self.hub.topology.async_connected(client)
                return

    async def _load(self, client: HubClient) -> None:
        self.mqtt = self.mqtt_status = None
        if self.supports_mqtt:
            try:
                self.mqtt = await client.request("mqtt/config/get")
                self.mqtt_status = await client.request("mqtt/status")
            except HubError as err:
                _LOGGER.debug("mqtt/config/get failed: %s", err)
        log = None
        # Maintenance requests (API 1.2) once the NCP session is up.
        if "maintenance" in self.hub.info:
            try:
                log = await client.request("network/ncp_log")
            except HubError as err:
                # `invalid_format` before the request existed, `unsupported` on EZSP.
                _LOGGER.debug("network/ncp_log: %s", err)
        self.ncp_log = log if isinstance(log, dict) else None
        self._notify()

    def tx_power_range(self) -> tuple[int, int]:
        """The TX power limits of the NCP's radio."""
        ncp = self.hub.network.get("ncp")
        chip = ncp.get("chip") if isinstance(ncp, dict) else None
        if not isinstance(chip, int):
            return TX_POWER_DEFAULT_RANGE
        return TX_POWER_RANGE.get(chip, TX_POWER_DEFAULT_RANGE)

    @property
    def tx_power(self) -> int | None:
        """The applied TX power (dBm), `None` while unknown."""
        value = self.hub.network.get("tx_power")
        if not isinstance(value, int) or isinstance(value, bool) or value == TX_POWER_UNKNOWN:
            return None
        return value

    async def async_set_mqtt(self, **fields: Any) -> dict[str, Any]:
        """`mqtt/config/set`: the hub validates, stores and restarts its bridge."""
        result = await self.hub.async_request("mqtt/config/set", timeout=SETTINGS_TIMEOUT, **fields)
        if isinstance(result, dict):
            self.mqtt = result
        self._notify()
        return result

    async def async_refresh_mqtt_status(self) -> None:
        """`mqtt/status`."""
        if not self.supports_mqtt or not self.hub.connected or self.hub.client is None:
            return
        try:
            status = await self.hub.client.request("mqtt/status")
        except HubError as err:
            _LOGGER.debug("mqtt/status failed: %s", err)
            return
        if isinstance(status, dict):
            self.mqtt_status = status
            self._notify()

    async def async_set_ncp_log(self, level: int) -> None:
        """`network/ncp_log {level}` (kept by the hub, sent in every NCP session)."""
        result = await self.hub.async_request(
            "network/ncp_log", timeout=SETTINGS_TIMEOUT, level=level
        )
        if isinstance(result, dict):
            self.ncp_log = result
        self._notify()

    async def async_set_tx_power(self, dbm: int) -> dict[str, Any]:
        """`network/set_tx_power`; the hub keeps it and applies it after NCP restarts."""
        result = await self.hub.async_request(
            "network/set_tx_power", timeout=SETTINGS_TIMEOUT, dbm=dbm
        )
        if isinstance(result, dict) and isinstance(result.get("applied_dbm"), int):
            self.hub.network["tx_power"] = result["applied_dbm"]
            self.hub.network["tx_power_requested"] = dbm
        await self.hub.async_refresh_network()
        return result

    @callback
    def stage_channel(self, channel: int | None) -> None:
        """The channel the next `change_channel` moves to."""
        self.channel_target = channel
        self._notify()

    async def async_change_channel(self, channel: int) -> dict[str, Any]:
        """`network/change_channel`: `Mgmt_NWK_Update_req` to the whole network."""
        result = await self.hub.async_request(
            "network/change_channel", timeout=CHANNEL_TIMEOUT, channel=channel
        )
        self.channel_target = None
        if isinstance(result, dict) and isinstance(result.get("channel"), int):
            self.hub.network["channel"] = result["channel"]
            self.hub.network["nwk_update_id"] = result.get("nwk_update_id")
        await self.hub.async_refresh_network()
        self._notify()
        return result if isinstance(result, dict) else {}

    def as_dict(self) -> dict[str, Any]:
        """For the diagnostics."""
        return {
            "mqtt": self.mqtt,
            "mqtt_status": self.mqtt_status,
            "ncp_log": self.ncp_log,
            "channel_target": self.channel_target,
        }
