"""Config flow: host, port and API token of a zigbee-rs hub."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_TOKEN
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import CannotConnect, HubClient, HubError, InvalidAuth, hub_url
from .const import CONF_PATH, CONF_SYNC_TIME_ZONE, DEFAULT_PATH, DEFAULT_PORT, DOMAIN
from .hub import async_fetch_info

_LOGGER = logging.getLogger(__name__)


def _schema(defaults: Mapping[str, Any], with_token: bool = True) -> vol.Schema:
    fields: dict[Any, Any] = {
        vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): str,
        vol.Required(CONF_PORT, default=defaults.get(CONF_PORT, DEFAULT_PORT)): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=65535)
        ),
    }
    if with_token:
        fields[vol.Required(CONF_TOKEN, default=defaults.get(CONF_TOKEN, ""))] = str
    return vol.Schema(fields)


class ZigbeeRsHubConfigFlow(ConfigFlow, domain=DOMAIN):
    """Manual set-up (the hub does not advertise itself over mDNS yet)."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Options of an entry."""
        return ZigbeeRsHubOptionsFlow()

    async def _probe(self, data: Mapping[str, Any]) -> dict[str, Any]:
        """Connect, authenticate and read the identity; raises `HubError`."""
        client = HubClient(
            async_get_clientsession(self.hass),
            hub_url(data[CONF_HOST], data[CONF_PORT], data.get(CONF_PATH, DEFAULT_PATH)),
            data[CONF_TOKEN],
        )
        await client.connect()
        try:
            return await async_fetch_info(client)
        finally:
            await client.close()

    async def _try(self, data: Mapping[str, Any], errors: dict[str, str]) -> dict[str, Any] | None:
        try:
            return await self._probe(data)
        except InvalidAuth:
            errors["base"] = "invalid_auth"
        except CannotConnect:
            errors["base"] = "cannot_connect"
        except HubError:
            _LOGGER.exception("Unexpected answer from the hub")
            errors["base"] = "unknown"
        return None

    @staticmethod
    def _unique_id(data: Mapping[str, Any], info: Mapping[str, Any]) -> str:
        return info.get("coordinator_ieee") or f"{data[CONF_HOST]}:{data[CONF_PORT]}"

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Host, port and token."""
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input[CONF_HOST] = user_input[CONF_HOST].strip()
            info = await self._try(user_input, errors)
            if info is not None:
                await self.async_set_unique_id(self._unique_id(user_input, info))
                self._abort_if_unique_id_configured(
                    updates={CONF_HOST: user_input[CONF_HOST], CONF_PORT: user_input[CONF_PORT]}
                )
                return self.async_create_entry(
                    title=f"zigbee-rs hub ({user_input[CONF_HOST]})",
                    data={**user_input, CONF_PATH: DEFAULT_PATH},
                    options={CONF_SYNC_TIME_ZONE: True},
                )
        return self.async_show_form(
            step_id="user", data_schema=_schema(user_input or {}), errors=errors
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """The hub refused the stored token."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a new token."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**entry.data, CONF_TOKEN: user_input[CONF_TOKEN]}
            if await self._try(data, errors) is not None:
                return self.async_update_reload_and_abort(entry, data=data)
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_TOKEN): str}),
            errors=errors,
            description_placeholders={"host": entry.data[CONF_HOST]},
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """New address or token of the same hub."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**entry.data, **user_input, CONF_HOST: user_input[CONF_HOST].strip()}
            info = await self._try(data, errors)
            if info is not None:
                ieee = info.get("coordinator_ieee")
                if ieee and entry.unique_id and ":" not in entry.unique_id:
                    await self.async_set_unique_id(ieee)
                    self._abort_if_unique_id_mismatch(reason="wrong_hub")
                return self.async_update_reload_and_abort(entry, data=data)
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_schema(user_input or entry.data),
            errors=errors,
        )


# MQTT bridge fields of the options (mqtt/config/set); the password is
# write-only: never read back, never stored in the entry.
MQTT_FIELDS = ("enabled", "host", "port", "username", "base_topic")
MQTT_PASSWORD = "password"
MQTT_CLEAR_PASSWORD = "clear_password"


class ZigbeeRsHubOptionsFlow(OptionsFlow):
    """The hub's time zone from Home Assistant; the hub's MQTT bridge.

    Options flows are for administrators and their input is not fired on
    the event bus: the broker's address and credentials go to the hub
    from here, not through entities.
    """

    def _mqtt_hub(self) -> Any:
        """The loaded hub, when it has the MQTT bridge."""
        entry = self.config_entry
        if entry.state is not ConfigEntryState.LOADED:
            return None
        hub = entry.runtime_data
        return hub if hub.settings.supports_mqtt and hub.settings.mqtt is not None else None

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """A menu with the MQTT bridge, else the time zone form."""
        if user_input is None and self._mqtt_hub() is not None:
            return self.async_show_menu(step_id="init", menu_options=["time_zone", "mqtt"])
        return await self._time_zone("init", user_input)

    async def async_step_time_zone(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Whether the hub takes Home Assistant's time zone."""
        return await self._time_zone("time_zone", user_input)

    async def _time_zone(self, step_id: str, user_input: dict[str, Any] | None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data={**self.config_entry.options, **user_input})
        current = self.config_entry.options.get(CONF_SYNC_TIME_ZONE, False)
        return self.async_show_form(
            step_id=step_id,
            data_schema=vol.Schema({vol.Required(CONF_SYNC_TIME_ZONE, default=current): bool}),
        )

    async def async_step_mqtt(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Broker, credentials and base topic of the hub's MQTT bridge."""
        hub = self._mqtt_hub()
        if hub is None:
            return self.async_abort(reason="not_loaded")
        current: dict[str, Any] = hub.settings.mqtt
        errors: dict[str, str] = {}
        placeholders = {"error": ""}
        if user_input is not None:
            user_input["port"] = int(user_input["port"])
            changes = {
                k: user_input[k]
                for k in MQTT_FIELDS
                if k in user_input and user_input[k] != current.get(k)
            }
            password = user_input.get(MQTT_PASSWORD) or ""
            if password:
                changes[MQTT_PASSWORD] = password
            elif user_input.get(MQTT_CLEAR_PASSWORD) or (
                # The hub keeps a password the request leaves out: it must not
                # go to another broker or with another user name.
                current.get("password_set") and ("host" in changes or "username" in changes)
            ):
                changes[MQTT_PASSWORD] = ""
            try:
                if changes:
                    await hub.settings.async_set_mqtt(**changes)
            except HomeAssistantError as err:
                errors["base"] = "mqtt_rejected"
                placeholders["error"] = str(err)
            else:
                return self.async_create_entry(data=dict(self.config_entry.options))
        values = {**current, **(user_input or {})}
        schema = vol.Schema(
            {
                vol.Required("enabled", default=bool(values.get("enabled"))): bool,
                vol.Optional("host", default=str(values.get("host") or "")): TextSelector(),
                vol.Required("port", default=int(values.get("port") or 1883)): NumberSelector(
                    NumberSelectorConfig(min=1, max=65535, step=1, mode=NumberSelectorMode.BOX)
                ),
                vol.Optional("username", default=str(values.get("username") or "")): TextSelector(),
                vol.Optional(MQTT_PASSWORD): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.PASSWORD)
                ),
                vol.Optional(MQTT_CLEAR_PASSWORD, default=False): bool,
                vol.Required(
                    "base_topic", default=str(values.get("base_topic") or "zigbee2mqtt")
                ): TextSelector(),
            }
        )
        return self.async_show_form(
            step_id="mqtt",
            data_schema=schema,
            errors=errors,
            description_placeholders=placeholders,
        )
