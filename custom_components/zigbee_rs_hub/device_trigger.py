"""Device triggers: the event types of a device's event entities (buttons, remotes).

One trigger per event type of each event entity of the device (`event_types`
of its record; no per-device code): `type` is the event type the hub sends
(`single`, `on`, `brightness_step_up`, `arrow_left_click`, …), `subtype` the
entity's key (`action`; with `_<endpoint>` when the device has the key on
several endpoints). They fire on `zigbee_rs_hub_action` (monitor.py), also
while the event entity is disabled.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import voluptuous as vol

from homeassistant.components.device_automation import DEVICE_TRIGGER_BASE_SCHEMA
from homeassistant.components.homeassistant.triggers import event as event_trigger
from homeassistant.const import CONF_DEVICE_ID, CONF_DOMAIN, CONF_PLATFORM, CONF_TYPE
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.trigger import TriggerActionType, TriggerInfo
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN, EVENT_ACTION
from .entity import capability
from .monitor import trigger_subtype

if TYPE_CHECKING:
    from .hub import ZigbeeRsHub

CONF_SUBTYPE = "subtype"

TRIGGER_SCHEMA = DEVICE_TRIGGER_BASE_SCHEMA.extend(
    {vol.Required(CONF_TYPE): cv.string, vol.Required(CONF_SUBTYPE): cv.string}
)


def _event_records(hass: HomeAssistant, device_id: str) -> list[tuple[ZigbeeRsHub, dict[str, Any]]]:
    """(hub, record) of the device's event entities on the loaded hubs."""
    dev = dr.async_get(hass).async_get(device_id)
    if dev is None:
        return []
    out: list[tuple[ZigbeeRsHub, dict[str, Any]]] = []
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        if entry.entry_id not in dev.config_entries:
            continue
        hub = entry.runtime_data
        idents = {i for d, i in dev.identifiers if d == DOMAIN}
        out.extend(
            (hub, rec)
            for rec in hub.entities.values()
            if rec.get("device") in idents
            and rec.get("platform") == "event"
            and not rec.get("orphaned")
        )
    return out


async def async_get_triggers(hass: HomeAssistant, device_id: str) -> list[dict[str, str]]:
    """The device's triggers: every event type of its event entities."""
    triggers: list[dict[str, str]] = []
    for hub, rec in _event_records(hass, device_id):
        subtype = trigger_subtype(hub, rec)
        for event_type in capability(rec, "event_types", []):
            trigger = {
                CONF_PLATFORM: "device",
                CONF_DOMAIN: DOMAIN,
                CONF_DEVICE_ID: device_id,
                CONF_TYPE: str(event_type),
                CONF_SUBTYPE: subtype,
            }
            if trigger not in triggers:
                triggers.append(trigger)
    return triggers


async def async_attach_trigger(
    hass: HomeAssistant,
    config: ConfigType,
    action: TriggerActionType,
    trigger_info: TriggerInfo,
) -> CALLBACK_TYPE:
    """Listen for the device's action events of this type and subtype."""
    event_config = event_trigger.TRIGGER_SCHEMA(
        {
            event_trigger.CONF_PLATFORM: "event",
            event_trigger.CONF_EVENT_TYPE: EVENT_ACTION,
            event_trigger.CONF_EVENT_DATA: {
                CONF_DEVICE_ID: config[CONF_DEVICE_ID],
                CONF_TYPE: config[CONF_TYPE],
                CONF_SUBTYPE: config[CONF_SUBTYPE],
            },
        }
    )
    return await event_trigger.async_attach_trigger(
        hass, event_config, action, trigger_info, platform_type="device"
    )
