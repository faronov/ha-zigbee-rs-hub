"""Selects (enum attributes, DPs)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import ZigbeeRsHubConfigEntry
from .entity import ZigbeeRsEntity, capability, setup_platform
from .settings_entities import ChannelChangeSelect, NcpLogLevelSelect, add_when_supported


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ZigbeeRsHubConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Selects of the hub's devices; the hub's channel change and NCP log level."""
    add_when_supported(
        entry,
        async_add_entities,
        lambda hub: hub.settings.supports_channel_change and hub.supports_network,
        lambda hub: [ChannelChangeSelect(hub)],
    )
    add_when_supported(
        entry,
        async_add_entities,
        lambda hub: hub.settings.supports_ncp_log,
        lambda hub: [NcpLogLevelSelect(hub)],
    )
    setup_platform(entry, "select", async_add_entities, ZigbeeRsSelect)


class ZigbeeRsSelect(ZigbeeRsEntity, SelectEntity):
    """A hub select."""

    def _apply_record(self, record: dict[str, Any]) -> None:
        self._attr_options = [str(o) for o in capability(record, "options", [])]

    @property
    def current_option(self) -> str | None:
        """The hub's label."""
        value = self.hub_state
        return str(value) if value is not None and str(value) in self.options else None

    async def async_select_option(self, option: str) -> None:
        """`select_option`."""
        await self.command("select_option", option=option)
