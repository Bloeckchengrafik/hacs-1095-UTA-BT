"""Run real HA platform setup and services over a simulated authenticated device."""

from unittest.mock import patch

from homeassistant.components.climate import HVACMode
from homeassistant.const import CONF_ADDRESS
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.busch_jaeger_1095_bt.const import CONF_SECRET, DOMAIN
from custom_components.busch_jaeger_1095_bt.diagnostics import async_get_config_entry_diagnostics
from custom_components.busch_jaeger_1095_bt.protocol import Thermostat
from tests.test_protocol import TEST_SECRET, Peripheral


async def test_climate_services_and_unload(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Thermostat",
        unique_id="AA:BB:CC:DD:EE:FF",
        data={CONF_ADDRESS: "AA:BB:CC:DD:EE:FF", CONF_SECRET: TEST_SECRET.hex()},
    )
    entry.add_to_hass(hass)

    async def connect():
        peripheral = Peripheral()
        await peripheral.connect()
        return peripheral

    def create(hass, address, secret, destination, on_event):
        return Thermostat(
            address,
            bytes.fromhex(secret),
            destination=destination,
            on_event=on_event,
            connection_factory=connect,
        )

    with patch("custom_components.busch_jaeger_1095_bt.coordinator.create_thermostat", create):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        entity_id = "climate.thermostat"
        state = hass.states.get(entity_id)
        assert state is not None and state.state == HVACMode.HEAT
        # No state is invented or written during connection/setup.
        assert state.attributes.get("temperature") is None
        assert not Peripheral.instances[-1].commands
        await hass.services.async_call(
            "climate",
            "set_temperature",
            {"entity_id": entity_id, "temperature": 21.5},
            blocking=True,
        )
        await hass.async_block_till_done()
        assert hass.states.get(entity_id).attributes["temperature"] == 21.5
        await hass.services.async_call(
            "climate",
            "set_preset_mode",
            {"entity_id": entity_id, "preset_mode": "eco"},
            blocking=True,
        )
        await hass.async_block_till_done()
        assert hass.states.get(entity_id).attributes["preset_mode"] == "eco"
        await hass.services.async_call(
            "climate",
            "set_preset_mode",
            {"entity_id": entity_id, "preset_mode": "comfort"},
            blocking=True,
        )
        await hass.async_block_till_done()
        assert hass.states.get(entity_id).attributes["preset_mode"] == "comfort"
        diagnostics = await async_get_config_entry_diagnostics(hass, entry)
        assert diagnostics["entry"]["data"][CONF_SECRET] == "**REDACTED**"
        assert diagnostics["entry"]["data"][CONF_ADDRESS] == "**REDACTED**"
        assert await hass.config_entries.async_unload(entry.entry_id)
        assert not Peripheral.instances[-1].is_connected
