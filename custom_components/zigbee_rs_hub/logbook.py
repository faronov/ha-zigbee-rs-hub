"""Logbook entries of the hub's life-cycle events (`zigbee_rs_hub_event`)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.components.logbook import LOGBOOK_ENTRY_MESSAGE, LOGBOOK_ENTRY_NAME
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN, EVENT_HUB

_MESSAGES = {
    "device_joined": "joined the network",
    "device_added": "was added to the hub",
    "device_left": "left the network",
    "device_rejoined": "rejoined the network",
    "device_removed": "was removed from the network",
    "interview_started": "interview started",
    "interview_complete": "interview complete",
    "interview_partial": "interview partial: the device did not answer everything (wake it up)",
    "definition_missing": "has no definition in the catalog: generic support only",
}


def describe(data: dict[str, Any]) -> str:
    """The logbook message of an event's data."""
    kind = str(data.get("type"))
    if kind == "definition_resolved":
        return f"uses the definition {data.get('definition')}"
    if kind == "definition_removed":
        return f"lost the definition {data.get('previous')}: generic support only"
    if kind == "definition_missing" and data.get("reason"):
        return f"has no definition it can run: {data['reason']}"
    if kind == "hub_crashed":
        return f"restarted after a crash: {data.get('reason')}"
    message = _MESSAGES.get(kind, kind.replace("_", " "))
    if kind == "device_rejoined" and data.get("nwk"):
        message += f" (address {data['nwk']})"
    return message


@callback
def async_describe_events(
    hass: HomeAssistant,
    async_describe_event: Callable[[str, str, Callable[[Event], dict[str, str]]], None],
) -> None:
    """Describe the hub's events."""

    @callback
    def describe_event(event: Event) -> dict[str, str]:
        data = event.data
        name = data.get("name") or "zigbee-rs hub"
        if isinstance(ident := data.get("device_id"), str) and (
            dev := dr.async_get(hass).async_get(ident)
        ):
            name = dev.name_by_user or dev.name or name
        return {LOGBOOK_ENTRY_NAME: str(name), LOGBOOK_ENTRY_MESSAGE: describe(data)}

    async_describe_event(DOMAIN, EVENT_HUB, describe_event)
