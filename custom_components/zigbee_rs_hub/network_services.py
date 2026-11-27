"""Services of network health, bindings, groups, hub settings and the Zigbee2MQTT import."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.service import async_register_admin_service

from . import bindings, z2m_import
from .const import ATTR_CONFIG_ENTRY_ID, DOMAIN
from .hub import ZigbeeRsHub
from .settings_entities import CHANNEL_NOTIFICATION

SERVICE_SCAN_TOPOLOGY = "scan_topology"
SERVICE_BIND = "bind"
SERVICE_UNBIND = "unbind"
SERVICE_READ_BINDINGS = "read_bindings"
SERVICE_GROUP_CREATE = "group_create"
SERVICE_GROUP_REMOVE = "group_remove"
SERVICE_GROUP_ADD_MEMBER = "group_add_member"
SERVICE_GROUP_REMOVE_MEMBER = "group_remove_member"
SERVICE_CHANGE_CHANNEL = "change_channel"
SERVICE_IMPORT_FROM_Z2M = "import_from_z2m"

ATTR_DEVICE_ID = "device_id"
ATTR_ENDPOINT = "endpoint"
ATTR_CLUSTERS = "clusters"
ATTR_TARGET_DEVICE_ID = "target_device_id"
ATTR_TARGET_ENDPOINT = "target_endpoint"
ATTR_GROUP_ID = "group_id"
ATTR_WAIT = "wait"

_ENDPOINT = vol.All(vol.Coerce(int), vol.Range(min=1, max=240))
_WAIT = vol.All(vol.Coerce(float), vol.Range(min=0, max=60))
_GROUP = vol.Any(vol.Coerce(int), cv.string)

SCAN_TOPOLOGY_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Optional("refresh", default=True): cv.boolean,
        vol.Optional("routes", default=True): cv.boolean,
    }
)
BIND_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Required(ATTR_DEVICE_ID): cv.string,
            vol.Optional(ATTR_ENDPOINT): _ENDPOINT,
            vol.Required(ATTR_CLUSTERS): vol.Any(cv.string, vol.Coerce(int), [vol.Any(int, str)]),
            vol.Exclusive(ATTR_TARGET_DEVICE_ID, "target"): cv.string,
            vol.Optional(ATTR_TARGET_ENDPOINT): _ENDPOINT,
            vol.Exclusive(ATTR_GROUP_ID, "target"): _GROUP,
            vol.Optional(ATTR_WAIT, default=bindings.DEFAULT_WAIT): _WAIT,
        }
    ),
    cv.has_at_least_one_key(ATTR_TARGET_DEVICE_ID, ATTR_GROUP_ID),
)
READ_BINDINGS_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Optional(ATTR_WAIT, default=bindings.DEFAULT_WAIT): _WAIT,
    }
)
GROUP_CREATE_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Required("name"): vol.All(cv.string, vol.Length(min=1, max=64)),
        vol.Optional(ATTR_GROUP_ID): _GROUP,
        vol.Optional("hub_member", default=True): cv.boolean,
    }
)
GROUP_REMOVE_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Required(ATTR_GROUP_ID): _GROUP,
        vol.Optional("force", default=False): cv.boolean,
    }
)
GROUP_ADD_MEMBER_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_GROUP_ID): _GROUP,
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Optional(ATTR_ENDPOINT): _ENDPOINT,
        vol.Optional(ATTR_WAIT, default=bindings.DEFAULT_WAIT): _WAIT,
    }
)
GROUP_REMOVE_MEMBER_SCHEMA = GROUP_ADD_MEMBER_SCHEMA.extend(
    {vol.Optional("force", default=False): cv.boolean}
)
CHANGE_CHANNEL_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Optional("channel"): vol.All(vol.Coerce(int), vol.Range(min=11, max=26)),
        vol.Required("confirm"): cv.boolean,
    }
)
IMPORT_FROM_Z2M_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Optional("apply", default=False): cv.boolean,
        vol.Optional("path"): cv.string,
        vol.Optional("force", default=False): cv.boolean,
        vol.Optional("confirm", default=False): cv.boolean,
        vol.Optional("confirm_ieee_write_once", default=False): cv.boolean,
        vol.Optional("keep_ieee", default=False): cv.boolean,
        vol.Optional("skip_backup", default=False): cv.boolean,
        vol.Optional("remove_missing", default=False): cv.boolean,
        vol.Optional("reconfigure", default="auto"): vol.In(["auto", "always", "never"]),
    }
)


def _loaded_hubs(hass: HomeAssistant) -> list[ZigbeeRsHub]:
    return [e.runtime_data for e in hass.config_entries.async_loaded_entries(DOMAIN)]


def _one_hub(hass: HomeAssistant, call: ServiceCall) -> ZigbeeRsHub:
    hubs = _loaded_hubs(hass)
    if entry_id := call.data.get(ATTR_CONFIG_ENTRY_ID):
        hubs = [h for h in hubs if h.entry.entry_id == entry_id]
    if not hubs:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_hub")
    if len(hubs) > 1:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="one_hub")
    return hubs[0]


def _hub_of_device(hass: HomeAssistant, device_id: str) -> tuple[ZigbeeRsHub, str]:
    dev = dr.async_get(hass).async_get(device_id)
    if dev is not None:
        for hub in _loaded_hubs(hass):
            if hub.entry.entry_id not in dev.config_entries:
                continue
            for domain, ident in dev.identifiers:
                if domain == DOMAIN and ident in hub.devices:
                    return hub, ident
    raise ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key="unknown_device",
        translation_placeholders={"device_id": device_id},
    )


def _zigbee_device(hass: HomeAssistant, device_id: str) -> tuple[ZigbeeRsHub, str]:
    hub, ident = _hub_of_device(hass, device_id)
    if not ident.startswith("zb:"):
        raise ServiceValidationError(f"{ident} is not a Zigbee device (bindings need ZDO)")
    return hub, ident


def async_setup_services(hass: HomeAssistant) -> None:
    """Register the services (once, in `async_setup`)."""

    async def scan_topology(call: ServiceCall) -> ServiceResponse:
        hub = _one_hub(hass, call)
        return await hub.topology.async_scan(call.data["refresh"], call.data["routes"])

    async def bind(call: ServiceCall, unbind: bool) -> ServiceResponse:
        hub, src = _zigbee_device(hass, call.data[ATTR_DEVICE_ID])
        target: dict[str, Any]
        if (dst := call.data.get(ATTR_TARGET_DEVICE_ID)) is not None:
            dst_hub, dst_id = _zigbee_device(hass, dst)
            if dst_hub is not hub:
                raise ServiceValidationError("source and target are on different hubs")
            target = {"device": dst_id, "endpoint": call.data.get(ATTR_TARGET_ENDPOINT)}
        else:
            target = {"group": bindings.group_id(call.data[ATTR_GROUP_ID])}
        return await bindings.async_bind(
            hub,
            src,
            call.data.get(ATTR_ENDPOINT),
            bindings.cluster_ids(call.data[ATTR_CLUSTERS]),
            target,
            unbind,
            call.data[ATTR_WAIT],
        )

    async def do_bind(call: ServiceCall) -> ServiceResponse:
        return await bind(call, False)

    async def do_unbind(call: ServiceCall) -> ServiceResponse:
        return await bind(call, True)

    async def read_bindings(call: ServiceCall) -> ServiceResponse:
        hub, dev = _zigbee_device(hass, call.data[ATTR_DEVICE_ID])
        sleepy = hub.devices[dev].get("power") != "mains"
        return await bindings.async_read_bindings(hub, dev, sleepy, call.data[ATTR_WAIT])

    async def group_create(call: ServiceCall) -> ServiceResponse:
        hub = _one_hub(hass, call)
        params: dict[str, Any] = {
            "name": call.data["name"],
            "hub_member": call.data["hub_member"],
        }
        if ATTR_GROUP_ID in call.data:
            params["group"] = bindings.group_id(call.data[ATTR_GROUP_ID])
        return await hub.async_request("groups/create", **params)

    async def group_remove(call: ServiceCall) -> None:
        hub = _one_hub(hass, call)
        params: dict[str, Any] = {"group": bindings.group_id(call.data[ATTR_GROUP_ID])}
        if call.data["force"]:
            params["force"] = True
        await hub.async_request("groups/delete", **params)

    async def group_member(call: ServiceCall, remove: bool) -> ServiceResponse:
        hub, dev = _zigbee_device(hass, call.data[ATTR_DEVICE_ID])
        return await bindings.async_group_member(
            hub,
            bindings.group_id(call.data[ATTR_GROUP_ID]),
            dev,
            call.data.get(ATTR_ENDPOINT),
            remove,
            call.data.get("force", False),
            call.data[ATTR_WAIT],
        )

    async def group_add_member(call: ServiceCall) -> ServiceResponse:
        return await group_member(call, False)

    async def group_remove_member(call: ServiceCall) -> ServiceResponse:
        return await group_member(call, True)

    async def change_channel(call: ServiceCall) -> ServiceResponse:
        hub = _one_hub(hass, call)
        if not hub.settings.supports_channel_change:
            raise ServiceValidationError("this hub cannot change the channel")
        channel = call.data.get("channel") or hub.settings.channel_target
        if channel is None:
            raise ServiceValidationError(
                "no channel: give channel, or select one in the hub's Channel change entity"
            )
        if not call.data["confirm"]:
            raise ServiceValidationError(
                "moving the network to another channel needs confirm: true (every device must "
                "follow; sleepy ones may need a rejoin)"
            )
        if channel == hub.network.get("channel"):
            hub.settings.stage_channel(None)
            raise ServiceValidationError(f"the network is on channel {channel} already")
        result = await hub.settings.async_change_channel(channel)
        persistent_notification.async_dismiss(hass, CHANNEL_NOTIFICATION)
        return result

    async def import_from_z2m(call: ServiceCall) -> ServiceResponse:
        return await z2m_import.async_import_from_z2m(hass, _one_hub(hass, call), dict(call.data))

    hass.services.async_register(
        DOMAIN,
        SERVICE_SCAN_TOPOLOGY,
        scan_topology,
        SCAN_TOPOLOGY_SCHEMA,
        SupportsResponse.OPTIONAL,
    )
    for name, handler, schema in (
        (SERVICE_BIND, do_bind, BIND_SCHEMA),
        (SERVICE_UNBIND, do_unbind, BIND_SCHEMA),
        (SERVICE_READ_BINDINGS, read_bindings, READ_BINDINGS_SCHEMA),
        (SERVICE_GROUP_CREATE, group_create, GROUP_CREATE_SCHEMA),
        (SERVICE_GROUP_ADD_MEMBER, group_add_member, GROUP_ADD_MEMBER_SCHEMA),
        (SERVICE_GROUP_REMOVE_MEMBER, group_remove_member, GROUP_REMOVE_MEMBER_SCHEMA),
    ):
        hass.services.async_register(DOMAIN, name, handler, schema, SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, SERVICE_GROUP_REMOVE, group_remove, GROUP_REMOVE_SCHEMA)
    # The network moves, or keys are restored from files: administrators only.
    async_register_admin_service(
        hass,
        DOMAIN,
        SERVICE_CHANGE_CHANNEL,
        change_channel,
        CHANGE_CHANNEL_SCHEMA,
        SupportsResponse.OPTIONAL,
    )
    async_register_admin_service(
        hass,
        DOMAIN,
        SERVICE_IMPORT_FROM_Z2M,
        import_from_z2m,
        IMPORT_FROM_Z2M_SCHEMA,
        SupportsResponse.OPTIONAL,
    )
