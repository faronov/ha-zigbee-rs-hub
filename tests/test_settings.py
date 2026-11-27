"""Hub settings from HA: MQTT bridge, TX power, channel change, NCP log level."""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, MockUser

from custom_components.zigbee_rs_hub.const import DOMAIN
from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON, STATE_UNKNOWN
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError, Unauthorized
from homeassistant.helpers import entity_registry as er

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


async def test_mqtt_settings(
    hass: HomeAssistant, fake_hub: NetworkFakeHub, setup_entry: MockConfigEntry
) -> None:
    switch = hub_eid(hass, setup_entry, "switch", "mqtt_enabled")
    host = hub_eid(hass, setup_entry, "text", "mqtt_host")
    password = hub_eid(hass, setup_entry, "text", "mqtt_password")
    port = hub_eid(hass, setup_entry, "number", "mqtt_port")
    topic = hub_eid(hass, setup_entry, "text", "mqtt_base_topic")
    status = hub_eid(hass, setup_entry, "sensor", "mqtt_status")
    assert er.async_get(hass).async_get(switch).entity_category is er.EntityCategory.CONFIG
    assert hass.states.get(switch).state == STATE_OFF
    assert hass.states.get(topic).state == "zigbee2mqtt"
    assert float(hass.states.get(port).state) == 1883
    assert hass.states.get(status).state == "disabled"
    assert hass.states.get(password).state == STATE_UNKNOWN
    assert hass.states.get(password).attributes["password_set"] is False

    # The hub refuses enabling without a broker.
    with pytest.raises(HomeAssistantError, match="host: required"):
        await hass.services.async_call("switch", "turn_on", {ATTR_ENTITY_ID: switch}, blocking=True)
    await hass.services.async_call(
        "text", "set_value", {ATTR_ENTITY_ID: host, "value": "192.168.1.2"}, blocking=True
    )
    await hass.services.async_call(
        "text",
        "set_value",
        {ATTR_ENTITY_ID: hub_eid(hass, setup_entry, "text", "mqtt_username"), "value": "ha"},
        blocking=True,
    )
    await hass.services.async_call(
        "text", "set_value", {ATTR_ENTITY_ID: password, "value": "s3cret"}, blocking=True
    )
    await hass.services.async_call(
        "number", "set_value", {ATTR_ENTITY_ID: port, "value": 1884}, blocking=True
    )
    await hass.services.async_call("switch", "turn_on", {ATTR_ENTITY_ID: switch}, blocking=True)
    sets = fake_hub.calls("mqtt/config/set")
    assert [{k: v for k, v in r.items() if k not in ("id", "type")} for r in sets] == [
        {"enabled": True},
        {"host": "192.168.1.2"},
        {"username": "ha"},
        {"password": "s3cret"},
        {"port": 1884},
        {"enabled": True},
    ]
    assert fake_hub.mqtt_password == "s3cret"
    assert hass.states.get(switch).state == STATE_ON
    assert hass.states.get(host).state == "192.168.1.2"
    # Write-only: never shown, only that one is set.
    assert hass.states.get(password).state == STATE_UNKNOWN
    assert hass.states.get(password).attributes["password_set"] is True
    await setup_entry.runtime_data.settings.async_refresh_mqtt_status()
    await wait_for(hass, lambda: hass.states.get(status).state == "connected")
    assert hass.states.get(status).attributes["broker"] == "192.168.1.2:1884"

    from custom_components.zigbee_rs_hub.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    diag = await async_get_config_entry_diagnostics(hass, setup_entry)
    assert diag["settings"]["mqtt"]["host"] == "192.168.1.2"
    assert diag["settings"]["mqtt"]["username"] == "**REDACTED**"


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
        ("text", "mqtt_host"),
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
        ("text", "mqtt_host"),
        ("number", "mqtt_port"),
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
