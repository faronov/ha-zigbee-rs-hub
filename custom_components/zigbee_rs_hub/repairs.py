"""Fix flows of the hub's repair issues (monitor.py creates and clears them)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import voluptuous as vol

from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow, RepairsFlowResult
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN
from .monitor import ISSUE_CATALOG_FAILED, ISSUE_INTERVIEW_PARTIAL

if TYPE_CHECKING:
    from .hub import ZigbeeRsHub

REINTERVIEW_TIMEOUT = 60.0


def _hub(hass: HomeAssistant, entry_id: object) -> ZigbeeRsHub | None:
    entry = hass.config_entries.async_get_entry(str(entry_id)) if entry_id else None
    if entry is None or entry.domain != DOMAIN or entry.state is not ConfigEntryState.LOADED:
        return None
    return entry.runtime_data


class _HubActionFlow(RepairsFlow):
    """Confirm, then one request to the hub; its error is shown on the form."""

    def __init__(self, data: dict[str, str | int | float | None]) -> None:
        """Flow for the issue's data (`entry_id`, `device`)."""
        self._data = data

    async def async_step_init(self, user_input: dict[str, str] | None = None) -> RepairsFlowResult:
        """The first step."""
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        """Show the issue's text; on submit run the action."""
        issue = ir.async_get(self.hass).async_get_issue(DOMAIN, self.issue_id)
        placeholders = dict(issue.translation_placeholders or {}) if issue else {}
        errors: dict[str, str] = {}
        if user_input is not None:
            hub = _hub(self.hass, self._data.get("entry_id"))
            if hub is None or not hub.connected:
                errors["base"] = "not_connected"
            else:
                try:
                    await self._run(hub)
                except HomeAssistantError as err:
                    errors["base"] = "failed"
                    placeholders["error"] = str(err)
                else:
                    return self.async_create_entry(data={})
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def _run(self, hub: ZigbeeRsHub) -> None:
        raise NotImplementedError


class ReinterviewFlow(_HubActionFlow):
    """An interview stayed partial: re-interview the device (wake it first)."""

    async def _run(self, hub: ZigbeeRsHub) -> None:
        device = str(self._data.get("device"))
        await hub.async_request("devices/reinterview", timeout=REINTERVIEW_TIMEOUT, device=device)
        hub.monitor.restarted_interview(device)


class CatalogRetryFlow(_HubActionFlow):
    """The catalog update failed: run it again."""

    async def _run(self, hub: ZigbeeRsHub) -> None:
        await hub.catalog.async_update()


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, str | int | float | None] | None
) -> RepairsFlow:
    """The fix flow of a fixable issue."""
    data = data or {}
    if issue_id.startswith(f"{ISSUE_INTERVIEW_PARTIAL}_"):
        return ReinterviewFlow(data)
    if issue_id.startswith(f"{ISSUE_CATALOG_FAILED}_"):
        return CatalogRetryFlow(data)
    return ConfirmRepairFlow()
