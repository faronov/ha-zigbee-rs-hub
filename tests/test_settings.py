"""Hub settings from HA: MQTT bridge, TX power, channel change, NCP log level."""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockUser,
    async_capture_events,
)
import voluptuous as vol

from custom_components.zigbee_rs_hub.const import DOMAIN
from homeassistant.auth.const import GROUP_ID_ADMIN, GROUP_ID_USER
from homeassistant.const import (
    ATTR_ENTITY_ID,
    EVENT_CALL_SERVICE,
    STATE_OFF,
    STATE_ON,
    STATE_UNKNOWN,
)
from homeassistant.core import Context, HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ServiceValidationError, Unauthorized
from homeassistant.helpers import entity_registry as er

from .fake_hub import SWITCH_UID
from .fake_network import NetworkFakeHub
from .test_init import wait_for


@pytest.fixture
async def fake_hub() -> AsyncGenerator[NetworkFakeHub]:
    """The fake hub with the settings API."""
    hub = NetworkFakeHub()
    await hub.start()
    yield hub
    await hub.stop()


def hub_eid(hass: HomeAssistant, entry: MockConfigEntry, platform: str, key: str) -> str | None:
    return er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{entry.entry_id}_{key}")


async def _user(hass: HomeAssistant, admin: bool) -> MockUser:
    group = await hass.auth.async_get_group(GROUP_ID_ADMIN if admin else GROUP_ID_USER)
    return MockUser(groups=[group]).add_to_hass(hass)


async def _mqtt_form(hass: HomeAssistant, entry: MockConfigEntry) -> dict:
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["time_zone", "mqtt"]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "mqtt"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "mqtt"
    return result


MQTT_FORM = {
    "enabled": False,
    "host": "",
    "port": 1883,
    "username": "",
    "base_topic": "zigbee2mqtt",
}


async def test_mqtt_settings_in_the_options(
    hass: HomeAssistant, fake_hub: NetworkFakeHub, setup_entry: MockConfigEntry
) -> None:
    """Broker and credentials only in the options flow: admins, off the event bus."""
    events = async_capture_events(hass, EVENT_CALL_SERVICE)
    switch = hub_eid(hass, setup_entry, "switch", "mqtt_enabled")
    status = hub_eid(hass, setup_entry, "sensor", "mqtt_status")
    assert er.async_get(hass).async_get(switch).entity_category is er.EntityCategory.CONFIG
    assert hass.states.get(switch).state == STATE_OFF
    assert hass.states.get(status).state == "disabled"
    assert hass.states.get(status).attributes["password_set"] is False
    # No entity takes the broker, the user name or the password.
    for platform, key in (
        ("text", "mqtt_host"),
        ("text", "mqtt_password"),
        ("number", "mqtt_port"),
    ):
        assert hub_eid(hass, setup_entry, platform, key) is None, key

    result = await _mqtt_form(hass, setup_entry)
    # The hub refuses enabling without a broker.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**MQTT_FORM, "enabled": True}
    )
    assert result["errors"] == {"base": "mqtt_rejected"}
    assert "host: required" in result["description_placeholders"]["error"]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            **MQTT_FORM,
            "enabled": True,
            "host": "192.168.1.2",
            "port": 1884.0,
            "username": "ha",
            "password": "s3cret",
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    sets = [
        {k: v for k, v in r.items() if k not in ("id", "type")}
        for r in fake_hub.calls("mqtt/config/set")
    ]
    assert sets == [
        {"enabled": True},
        {
            "enabled": True,
            "host": "192.168.1.2",
            "port": 1884,
            "username": "ha",
            "password": "s3cret",
        },
    ]
    assert fake_hub.mqtt_password == "s3cret"
    # Not stored in the entry, never on the event bus.
    assert "s3cret" not in repr(setup_entry.options) + repr(setup_entry.data)
    assert not [e for e in events if "s3cret" in repr(e.data)]
    await hass.async_block_till_done()
    assert hass.states.get(switch).state == STATE_ON
    assert hass.states.get(status).attributes["password_set"] is True
    await setup_entry.runtime_data.settings.async_refresh_mqtt_status()
    await wait_for(hass, lambda: hass.states.get(status).state == "connected")
    assert hass.states.get(status).attributes["broker"] == "192.168.1.2:1884"

    # Another broker without a new password: the stored one is removed.
    result = await _mqtt_form(hass, setup_entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {**MQTT_FORM, "enabled": True, "host": "broker.lan", "port": 1884, "username": "ha"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert fake_hub.calls("mqtt/config/set")[-1]["host"] == "broker.lan"
    assert fake_hub.calls("mqtt/config/set")[-1]["password"] == ""
    assert fake_hub.mqtt["password_set"] is False
    # Only the port: the password is kept.
    fake_hub.mqtt["password_set"] = True
    result = await _mqtt_form(hass, setup_entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {**MQTT_FORM, "enabled": True, "host": "broker.lan", "port": 1885, "username": "ha"},
    )
    assert {
        k: v for k, v in fake_hub.calls("mqtt/config/set")[-1].items() if k not in ("id", "type")
    } == {"port": 1885}

    from custom_components.zigbee_rs_hub.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    diag = await async_get_config_entry_diagnostics(hass, setup_entry)
    assert diag["settings"]["mqtt"]["host"] == "broker.lan"
    assert diag["settings"]["mqtt"]["username"] == "**REDACTED**"

    # The frontend leaves out an optional text field the user emptied: the
    # form suggests the current values instead of defaulting to them.
    result = await _mqtt_form(hass, setup_entry)
    keys = {str(k): k for k in result["data_schema"].schema}
    for key, value in (("host", "broker.lan"), ("username", "ha")):
        assert keys[key].default is vol.UNDEFINED, key
        assert keys[key].description == {"suggested_value": value}
    assert "suggested_value" not in (keys["password"].description or {})
    # User name emptied: anonymous, and the stored password goes with it.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"enabled": True, "host": "broker.lan", "port": 1885, "base_topic": "zigbee2mqtt"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert {
        k: v for k, v in fake_hub.calls("mqtt/config/set")[-1].items() if k not in ("id", "type")
    } == {"username": "", "password": ""}
    assert (fake_hub.mqtt["username"], fake_hub.mqtt["password_set"]) == ("", False)
    # The broker emptied (with the bridge off).
    result = await _mqtt_form(hass, setup_entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"enabled": False, "port": 1885, "base_topic": "zigbee2mqtt"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert {
        k: v for k, v in fake_hub.calls("mqtt/config/set")[-1].items() if k not in ("id", "type")
    } == {"enabled": False, "host": ""}


async def test_settings_entities_are_for_admins(
    hass: HomeAssistant, fake_hub: NetworkFakeHub, setup_entry: MockConfigEntry
) -> None:
    """Every user may control entities: the hub's settings check for an administrator."""
    user = await _user(hass, admin=False)
    admin = await _user(hass, admin=True)
    calls = [
        ("switch", "turn_on", hub_eid(hass, setup_entry, "switch", "mqtt_enabled"), {}),
        ("number", "set_value", hub_eid(hass, setup_entry, "number", "tx_power"), {"value": 10}),
        (
            "select",
            "select_option",
            hub_eid(hass, setup_entry, "select", "ncp_log_level"),
            {"option": "debug"},
        ),
        (
            "select",
            "select_option",
            hub_eid(hass, setup_entry, "select", "channel_change"),
            {"option": "20"},
        ),
    ]
    fake_hub.mqtt["host"] = "192.168.1.2"
    # The user controls other entities (HA's own permission check passes).
    await hass.services.async_call(
        "switch",
        "turn_on",
        {ATTR_ENTITY_ID: er.async_get(hass).async_get_entity_id("switch", DOMAIN, SWITCH_UID)},
        blocking=True,
        context=Context(user_id=user.id),
    )
    for domain, service, entity_id, data in calls:
        with pytest.raises(Unauthorized):
            await hass.services.async_call(
                domain,
                service,
                {ATTR_ENTITY_ID: entity_id, **data},
                blocking=True,
                context=Context(user_id=user.id),
            )
    assert not fake_hub.calls("mqtt/config/set")
    assert not fake_hub.calls("network/set_tx_power")
    assert [r for r in fake_hub.calls("network/ncp_log") if "level" in r] == []
    assert setup_entry.runtime_data.settings.channel_target is None
    for domain, service, entity_id, data in calls:
        await hass.services.async_call(
            domain,
            service,
            {ATTR_ENTITY_ID: entity_id, **data},
            blocking=True,
            context=Context(user_id=admin.id),
        )
    assert fake_hub.calls("mqtt/config/set")[0]["enabled"] is True
    assert fake_hub.calls("network/set_tx_power")[0]["dbm"] == 10
    assert setup_entry.runtime_data.settings.channel_target == 20


async def test_tx_power(
    hass: HomeAssistant, fake_hub: NetworkFakeHub, setup_entry: MockConfigEntry
) -> None:
    tx = hub_eid(hass, setup_entry, "number", "tx_power")
    # network/info `tx_power` 127: unknown.
    assert hass.states.get(tx).state == STATE_UNKNOWN
    assert hass.states.get(tx).attributes["unit_of_measurement"] == "dBm"
    assert hass.states.get(tx).attributes["min"] == -24
    await hass.services.async_call(
        "number", "set_value", {ATTR_ENTITY_ID: tx, "value": 10}, blocking=True
    )
    assert fake_hub.calls("network/set_tx_power")[0]["dbm"] == 10
    await wait_for(hass, lambda: hass.states.get(tx).state == "10")
    assert hass.states.get(tx).attributes["requested"] == 10


async def test_channel_change(
    hass: HomeAssistant,
    fake_hub: NetworkFakeHub,
    setup_entry: MockConfigEntry,
    hass_read_only_user: MockUser,
) -> None:
    select = hub_eid(hass, setup_entry, "select", "channel_change")
    assert hass.states.get(select).state == "17"
    # Selecting only stages it (and warns).
    await hass.services.async_call(
        "select", "select_option", {ATTR_ENTITY_ID: select, "option": "20"}, blocking=True
    )
    assert hass.states.get(select).state == "20"
    assert hass.states.get(select).attributes["pending"] is True
    assert not fake_hub.calls("network/change_channel")

    with pytest.raises(ServiceValidationError, match="confirm"):
        await hass.services.async_call(
            DOMAIN, "change_channel", {"confirm": False}, blocking=True, return_response=True
        )
    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            DOMAIN,
            "change_channel",
            {"confirm": True},
            blocking=True,
            context=Context(user_id=hass_read_only_user.id),
        )
    result = await hass.services.async_call(
        DOMAIN, "change_channel", {"confirm": True}, blocking=True, return_response=True
    )
    assert result == {"channel": 20, "nwk_update_id": 3}
    assert fake_hub.calls("network/change_channel")[0]["channel"] == 20
    channel = hub_eid(hass, setup_entry, "sensor", "channel")
    await wait_for(hass, lambda: hass.states.get(channel).state == "20")
    assert hass.states.get(select).state == "20"
    assert hass.states.get(select).attributes["pending"] is False

    with pytest.raises(ServiceValidationError, match="already"):
        await hass.services.async_call(
            DOMAIN, "change_channel", {"channel": 20, "confirm": True}, blocking=True
        )


async def test_ncp_log_level(
    hass: HomeAssistant, fake_hub: NetworkFakeHub, setup_entry: MockConfigEntry
) -> None:
    select = hub_eid(hass, setup_entry, "select", "ncp_log_level")
    assert hass.states.get(select).state == "info"
    await hass.services.async_call(
        "select", "select_option", {ATTR_ENTITY_ID: select, "option": "debug"}, blocking=True
    )
    assert fake_hub.calls("network/ncp_log")[-1]["level"] == 4
    assert hass.states.get(select).state == "debug"


async def test_settings_once_the_ncp_session_is_up(
    hass: HomeAssistant, fake_hub: NetworkFakeHub, config_entry: MockConfigEntry
) -> None:
    """A hub that reports `maintenance` only after HA connected: entities follow."""
    fake_hub.info_without_maintenance = 2
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert hub_eid(hass, config_entry, "switch", "mqtt_enabled") is None
    assert hub_eid(hass, config_entry, "number", "tx_power") is None
    await wait_for(
        hass, lambda: hub_eid(hass, config_entry, "switch", "mqtt_enabled") is not None, 5
    )
    for platform, key in (
        ("number", "tx_power"),
        ("select", "channel_change"),
        ("select", "ncp_log_level"),
        ("sensor", "mqtt_status"),
    ):
        assert hub_eid(hass, config_entry, platform, key) is not None, key
    assert config_entry.runtime_data.topology.supported
    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_hub_without_settings(
    hass: HomeAssistant, fake_hub: NetworkFakeHub, config_entry: MockConfigEntry
) -> None:
    fake_hub.maintenance = ["diagnostics"]
    fake_hub.ncp_log = None
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    for platform, key in (
        ("switch", "mqtt_enabled"),
        ("sensor", "mqtt_status"),
        ("number", "tx_power"),
        ("select", "channel_change"),
        ("select", "ncp_log_level"),
    ):
        assert hub_eid(hass, config_entry, platform, key) is None, key
    assert not fake_hub.calls("mqtt/config/get")
    with pytest.raises(ServiceValidationError, match="cannot change the channel"):
        await hass.services.async_call(
            DOMAIN, "change_channel", {"channel": 20, "confirm": True}, blocking=True
        )
    await hass.config_entries.async_unload(config_entry.entry_id)
