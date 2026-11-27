"""Network health sensors: hub-level counts, per-device diagnostics, MQTT bridge.

The hub serves link quality, RSSI and last seen as its own entities
(device-support 4.6); these add what its device records, the network map
and `devices/diagnostics` tell besides. Per-device sensors are disabled by
default.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import timedelta
import logging
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval

from .const import DOMAIN
from .entity import HubEntity
from .hub import ZigbeeRsHub, find_device
from .settings_entities import add_when_supported, has_mqtt

_LOGGER = logging.getLogger(__name__)

# devices/diagnostics carries the last 50 frames: read rarely, one at a time.
DIAGNOSTICS_INTERVAL = timedelta(minutes=30)
MQTT_STATUS_INTERVAL = timedelta(seconds=60)
# Device record `interview`.
INTERVIEW_STATES = ["complete", "partial", "imported", "in_progress"]
# mqtt/status `state`.
MQTT_STATES = [
    "disabled",
    "low_memory",
    "waiting_for_hub",
    "connecting",
    "connected",
    "retrying",
]
# Names listed in the offline sensor's attributes at most.
OFFLINE_LISTED = 50


def device_role(hub: ZigbeeRsHub, dev: dict[str, Any]) -> str | None:
    """`router`, `end_device` or `green_power`; `None` for group devices.

    Device records carry no logical type: the network map's node type when
    the device is in it, else mains-powered Zigbee devices count as routers
    (as the hub's MQTT bridge reports them).
    """
    dev_id = str(dev.get("id", ""))
    if dev_id.startswith("gp:"):
        return "green_power"
    if not dev_id.startswith("zb:"):
        return None
    node = hub.topology.node_of_device(dev_id)
    if node is not None and node.get("type") in ("router", "end_device"):
        return node["type"]
    return "router" if dev.get("power") == "mains" else "end_device"


def ha_device_name(hub: ZigbeeRsHub, device_id: str) -> str | None:
    """The name HA shows for a hub device (the user's, else the hub's)."""
    dev = find_device(dr.async_get(hub.hass), (DOMAIN, device_id), hub.entry.entry_id)
    if dev is not None:
        return dev.name_by_user or dev.name
    rec = hub.devices.get(device_id)
    return str(rec.get("name") or device_id) if rec else None


@callback
def async_setup_sensors(entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """The hub's health sensors, and per-device ones as devices appear."""
    hub: ZigbeeRsHub = entry.runtime_data
    async_add_entities([DeviceCountSensor(hub, key) for key in COUNTS])
    add_when_supported(entry, async_add_entities, has_mqtt, lambda h: [MqttStatusSensor(h)])
    listeners: dict[str, list[Callable[[], None]]] = {}
    created: set[str] = set()
    lock = asyncio.Semaphore(1)

    @callback
    def device_changed(device_id: str) -> None:
        for cb in list(listeners.get(device_id, [])):
            cb()
        if device_id not in hub.devices:
            # Removed: its entities go with the HA device; a new join adds them again.
            created.discard(device_id)
            return
        if device_id in created or not device_id.startswith("zb:"):
            return
        created.add(device_id)
        async_add_entities(
            [
                NetworkAddressSensor(hub, device_id, listeners),
                InterviewSensor(hub, device_id, listeners),
                ParentSensor(hub, device_id, listeners),
                InterviewResumesSensor(hub, device_id, listeners, lock),
            ]
        )

    entry.async_on_unload(hub.add_device_listener(device_changed))
    for device_id in list(hub.devices):
        device_changed(device_id)


# Hub-level counts: key → which devices it counts.
COUNTS: dict[str, Callable[[ZigbeeRsHub, dict[str, Any]], bool]] = {
    "devices": lambda hub, d: device_role(hub, d) is not None,
    "offline_devices": lambda hub, d: (
        device_role(hub, d) is not None and d.get("available") is False
    ),
    "routers": lambda hub, d: device_role(hub, d) == "router",
    "end_devices": lambda hub, d: device_role(hub, d) == "end_device",
}


class DeviceCountSensor(HubEntity, SensorEntity):
    """How many devices the hub has (of a kind)."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hub: ZigbeeRsHub, key: str) -> None:
        """Count `key` of `hub`."""
        super().__init__(hub, key)
        self._key = key
        self._written: tuple[Any, ...] | None = None

    async def async_added_to_hass(self) -> None:
        """Follow devices, the connection and the network map."""
        await super().async_added_to_hass()
        self.async_on_remove(self.hub.add_device_listener(lambda _d: self._hub_update()))
        self.async_on_remove(self.hub.topology.add_listener(self._hub_update))

    def _counted(self) -> list[dict[str, Any]]:
        test = COUNTS[self._key]
        return [d for d in self.hub.devices.values() if test(self.hub, d)]

    @property
    def native_value(self) -> int:
        """The count."""
        return len(self._counted())

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Per kind (devices) or the names (offline devices)."""
        if self._key == "devices":
            roles = [device_role(self.hub, d) for d in self._counted()]
            return {
                "zigbee": sum(r != "green_power" for r in roles),
                "green_power": roles.count("green_power"),
            }
        if self._key == "offline_devices":
            names = sorted(ha_device_name(self.hub, d["id"]) or d["id"] for d in self._counted())
            return {"devices": names[:OFFLINE_LISTED]}
        return None

    @callback
    def _hub_update(self) -> None:
        # A snapshot notifies once per device: write only what changed.
        now = (self.available, self.native_value, repr(self.extra_state_attributes))
        if now != self._written and self.hass is not None and self.entity_id:
            self._written = now
            self.async_write_ha_state()


class MqttStatusSensor(HubEntity, SensorEntity):
    """The state of the hub's MQTT bridge (mqtt/status, read every minute)."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = MQTT_STATES
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hub: ZigbeeRsHub) -> None:
        """Sensor of `hub`."""
        super().__init__(hub, "mqtt_status")

    async def async_added_to_hass(self) -> None:
        """Follow the settings; read the status every minute."""
        await super().async_added_to_hass()
        self.async_on_remove(self.hub.settings.add_listener(self._hub_update))
        self.async_on_remove(async_track_time_interval(self.hass, self._poll, MQTT_STATUS_INTERVAL))

    async def _poll(self, _now: Any = None) -> None:
        await self.hub.settings.async_refresh_mqtt_status()

    @property
    def available(self) -> bool:
        """Connected and the status is known."""
        return self.hub.connected and self.hub.settings.mqtt_status is not None

    @property
    def native_value(self) -> str | None:
        """`state`."""
        state = (self.hub.settings.mqtt_status or {}).get("state")
        return state if state in MQTT_STATES else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Broker, last error and counters."""
        st = self.hub.settings.mqtt_status or {}
        cfg = self.hub.settings.mqtt or {}
        return {
            "broker": f"{cfg.get('host')}:{cfg.get('port')}" if cfg.get("host") else None,
            "base_topic": cfg.get("base_topic"),
            "last_error": st.get("last_error"),
            "retry_in_s": st.get("retry_in_s"),
            "connects": st.get("connects"),
            "published": st.get("published"),
            "received": st.get("received"),
            "dropped": st.get("dropped"),
            "errors": st.get("errors"),
        }


class DeviceHealthSensor(SensorEntity):
    """A diagnostic sensor the integration adds to a Zigbee device."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    key = ""

    def __init__(
        self,
        hub: ZigbeeRsHub,
        device_id: str,
        listeners: dict[str, list[Callable[[], None]]],
    ) -> None:
        """Sensor `key` of hub device `device_id`."""
        self.hub = hub
        self.device_id = device_id
        self._listeners = listeners
        self._attr_translation_key = self.key
        self._attr_unique_id = f"{device_id}_{self.key}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, device_id)})

    @property
    def device(self) -> dict[str, Any]:
        """The hub's device record (empty once removed)."""
        return self.hub.devices.get(self.device_id) or {}

    @property
    def available(self) -> bool:
        """Connected and the hub has the device."""
        return self.hub.connected and self.device_id in self.hub.devices

    async def async_added_to_hass(self) -> None:
        """Follow the device record and the connection."""
        cbs = self._listeners.setdefault(self.device_id, [])
        cbs.append(self._update)
        self.async_on_remove(lambda: cbs.remove(self._update))
        self.async_on_remove(self.hub.add_hub_listener(self._update))

    @callback
    def _update(self) -> None:
        if self.hass is not None and self.entity_id:
            self.async_write_ha_state()


class NetworkAddressSensor(DeviceHealthSensor):
    """The device's short (NWK) address; it changes after an address conflict."""

    key = "network_address"

    @property
    def native_value(self) -> str | None:
        """`0x1A2B`."""
        nwk = self.device.get("nwk")
        return f"0x{nwk:04X}" if isinstance(nwk, int) and not isinstance(nwk, bool) else None


class InterviewSensor(DeviceHealthSensor):
    """Interview state (`imported`: a migrated device the hub has not heard yet)."""

    key = "interview"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = INTERVIEW_STATES

    @property
    def native_value(self) -> str | None:
        """`complete`, `partial`, `imported`, `in_progress`."""
        state = self.device.get("interview")
        return state if state in INTERVIEW_STATES else None


class ParentSensor(DeviceHealthSensor):
    """The device's parent (and the coordinator's route) in the last network map."""

    key = "parent"

    async def async_added_to_hass(self) -> None:
        """Also follow the network map."""
        await super().async_added_to_hass()
        self.async_on_remove(self.hub.topology.add_listener(self._update))

    def _name(self, nwk: Any) -> str | None:
        if not isinstance(nwk, int):
            return None
        node = self.hub.topology.node(nwk)
        if nwk == 0 or (node and node.get("type") == "coordinator"):
            return "Coordinator"
        if node and isinstance(node.get("device"), str):
            return ha_device_name(self.hub, node["device"]) or node.get("name")
        return f"0x{nwk:04X}"

    @property
    def native_value(self) -> str | None:
        """The parent's name, `Coordinator`, or its address."""
        parent = self.hub.topology.parent(self.device_id)
        return self._name(parent.get("nwk")) if parent else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Link quality to the parent, depth, the route and the map's time."""
        topo = self.hub.topology
        parent = topo.parent(self.device_id) or {}
        route = topo.route(self.device_id) or {}
        node = topo.node_of_device(self.device_id) or {}
        nwk = parent.get("nwk")
        mapped = self.hub.hub_time((topo.last or {}).get("time"))
        return {
            "parent_nwk": f"0x{nwk:04X}" if isinstance(nwk, int) else None,
            "lqi": parent.get("lqi"),
            "depth": parent.get("depth"),
            "route_next_hop": self._name(route.get("next_hop")),
            "route_status": route.get("status"),
            "scanned": node.get("scanned"),
            "error": node.get("error"),
            "map_time": mapped.isoformat() if mapped else None,
        }


class InterviewResumesSensor(DeviceHealthSensor):
    """Partial interviews resumed on a rejoin since the hub started (devices/diagnostics)."""

    key = "interview_resumes"

    def __init__(
        self,
        hub: ZigbeeRsHub,
        device_id: str,
        listeners: dict[str, list[Callable[[], None]]],
        lock: asyncio.Semaphore,
    ) -> None:
        """Sensor of `device_id`; `lock` keeps one diagnostics read at a time."""
        super().__init__(hub, device_id, listeners)
        self._lock = lock
        self._value: int | None = None

    async def async_added_to_hass(self) -> None:
        """Read now and every 30 minutes."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_time_interval(self.hass, self._refresh, DIAGNOSTICS_INTERVAL)
        )
        self.hass.async_create_task(self._refresh(), f"{DOMAIN} {self.device_id} diagnostics")

    async def _refresh(self, _now: Any = None) -> None:
        if not self.available:
            return
        async with self._lock:
            try:
                diag = await self.hub.async_request("devices/diagnostics", device=self.device_id)
            except HomeAssistantError as err:
                _LOGGER.debug("devices/diagnostics of %s: %s", self.device_id, err)
                return
        value = diag.get("interview_resumes") if isinstance(diag, dict) else None
        self._value = value if isinstance(value, int) else None
        self._update()

    @property
    def native_value(self) -> int | None:
        """The count from the last read."""
        return self._value
