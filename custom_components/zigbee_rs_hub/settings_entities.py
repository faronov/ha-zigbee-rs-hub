"""Config entities of the hub's settings on the hub device (hub_settings.py)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.components.select import SelectEntity
from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import SIGNAL_STRENGTH_DECIBELS_MILLIWATT
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import HubSettingEntity
from .hub import ZigbeeRsHub
from .hub_settings import NCP_LOG_LEVELS

CHANNELS = [str(c) for c in range(11, 27)]
CHANNEL_NOTIFICATION = "zigbee_rs_hub_channel_change"


@callback
def add_when_supported(
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
    supported: Callable[[ZigbeeRsHub], bool],
    make: Callable[[ZigbeeRsHub], list[Entity]],
) -> None:
    """Add the entities now if the hub has the setting, else once it reports it.

    A hub whose NCP session comes up after Home Assistant connected tells
    its maintenance features later (also a hub updated to a firmware with
    them, after the reconnect).
    """
    hub: ZigbeeRsHub = entry.runtime_data
    if supported(hub):
        async_add_entities(make(hub))
        return
    unsub: Callable[[], None] | None = None

    @callback
    def check() -> None:
        nonlocal unsub
        if unsub is not None and supported(hub):
            unsub()
            unsub = None
            async_add_entities(make(hub))

    unsub = hub.settings.add_listener(check)

    @callback
    def unload() -> None:
        if unsub is not None:
            unsub()

    entry.async_on_unload(unload)


def has_mqtt(hub: ZigbeeRsHub) -> bool:
    """The MQTT bridge's settings are known."""
    return hub.settings.supports_mqtt and hub.settings.mqtt is not None


class MqttEnabledSwitch(HubSettingEntity, SwitchEntity):
    """The MQTT bridge on or off; its broker and credentials are set in the entry's options."""

    def __init__(self, hub: ZigbeeRsHub) -> None:
        """MQTT bridge switch of `hub`."""
        super().__init__(hub, "mqtt_enabled")

    @property
    def available(self) -> bool:
        """Connected and the settings are known."""
        return self.hub.connected and self.hub.settings.mqtt is not None

    @property
    def is_on(self) -> bool | None:
        """Enabled on the hub."""
        value = (self.hub.settings.mqtt or {}).get("enabled")
        return value if isinstance(value, bool) else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable the bridge (it connects to the broker stored on the hub)."""
        await self.async_check_admin()
        await self.hub.settings.async_set_mqtt(enabled=True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable the bridge."""
        await self.async_check_admin()
        await self.hub.settings.async_set_mqtt(enabled=False)


class TxPowerNumber(HubSettingEntity, NumberEntity):
    """Zigbee TX power (antenna-referred; the NCP rounds down to its steps)."""

    _attr_device_class = NumberDeviceClass.SIGNAL_STRENGTH
    _attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX

    def __init__(self, hub: ZigbeeRsHub) -> None:
        """TX power of `hub`."""
        super().__init__(hub, "tx_power")

    @property
    def native_min_value(self) -> float:
        """The radio's lowest power."""
        return self.hub.settings.tx_power_range()[0]

    @property
    def native_max_value(self) -> float:
        """The radio's highest power."""
        return self.hub.settings.tx_power_range()[1]

    @property
    def native_value(self) -> int | None:
        """The applied power (network/info `tx_power`)."""
        return self.hub.settings.tx_power

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The power last asked for."""
        return {"requested": self.hub.network.get("tx_power_requested")}

    async def async_set_native_value(self, value: float) -> None:
        """`network/set_tx_power`."""
        await self.async_check_admin()
        await self.hub.settings.async_set_tx_power(int(value))


class NcpLogLevelSelect(HubSettingEntity, SelectEntity):
    """How much of the NCP's own log goes into the hub's log (ZRHP NCPs)."""

    _attr_options = NCP_LOG_LEVELS

    def __init__(self, hub: ZigbeeRsHub) -> None:
        """NCP log level of `hub`."""
        super().__init__(hub, "ncp_log_level")

    @property
    def available(self) -> bool:
        """Connected and the hub answered network/ncp_log."""
        return self.hub.connected and self.hub.settings.ncp_log is not None

    @property
    def current_option(self) -> str | None:
        """The level."""
        level = (self.hub.settings.ncp_log or {}).get("level")
        return NCP_LOG_LEVELS[level] if isinstance(level, int) and 0 <= level < 6 else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The NCP log's counters at the last change."""
        log = self.hub.settings.ncp_log or {}
        return {k: log.get(k) for k in ("forwarded", "suppressed", "dropped")}

    async def async_select_option(self, option: str) -> None:
        """`network/ncp_log {level}`."""
        await self.async_check_admin()
        await self.hub.settings.async_set_ncp_log(NCP_LOG_LEVELS.index(option))


class ChannelChangeSelect(HubSettingEntity, SelectEntity):
    """The channel to move the network to: staged here, applied by a service.

    Selecting a channel does not move the network: the admin service
    `zigbee_rs_hub.change_channel` with `confirm: true` does.
    """

    _attr_options = CHANNELS

    def __init__(self, hub: ZigbeeRsHub) -> None:
        """Channel change of `hub`."""
        super().__init__(hub, "channel_change")

    @property
    def current_option(self) -> str | None:
        """The staged channel, else the current one."""
        channel = self.hub.settings.channel_target or self.hub.network.get("channel")
        return str(channel) if str(channel) in CHANNELS else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The network's channel now and whether a change is staged."""
        return {
            "current_channel": self.hub.network.get("channel"),
            "pending": self.hub.settings.channel_target is not None,
        }

    async def async_select_option(self, option: str) -> None:
        """Stage the channel and say how to apply it."""
        await self.async_check_admin()
        channel = int(option)
        if channel == self.hub.network.get("channel"):
            self.hub.settings.stage_channel(None)
            persistent_notification.async_dismiss(self.hass, CHANNEL_NOTIFICATION)
            return
        self.hub.settings.stage_channel(channel)
        notify_channel_staged(self.hass, self.hub, channel)


def notify_channel_staged(hass: HomeAssistant, hub: ZigbeeRsHub, channel: int) -> None:
    """The warning that comes with a staged channel change."""
    persistent_notification.async_create(
        hass,
        f"Channel {channel} is selected for {hub.entry.title}; the network has NOT moved yet "
        f"(it is on channel {hub.network.get('channel')}). To move it, call the action "
        "`zigbee_rs_hub.change_channel` with `confirm: true`.\n\n"
        "**Warning:** every device must follow. Routers move within about 10 s; sleepy "
        "devices find the network when they next wake up, some only after a rejoin or not "
        "at all (then they must be paired again). Wi-Fi on the new channel may disturb the "
        "network. Take a network backup first.",
        title="zigbee-rs hub: channel change",
        notification_id=CHANNEL_NOTIFICATION,
    )
