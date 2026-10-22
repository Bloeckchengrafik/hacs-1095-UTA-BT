"""Prove HA supplies the proxy-aware BLEDevice rather than a raw MAC connection."""

from unittest.mock import AsyncMock, patch, sentinel

import pytest
from homeassistant.const import CONF_ADDRESS
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.busch_jaeger_1095_bt.connection import create_thermostat
from custom_components.busch_jaeger_1095_bt.const import CONF_SECRET, DOMAIN
from custom_components.busch_jaeger_1095_bt.coordinator import ThermostatCoordinator
from custom_components.busch_jaeger_1095_bt.protocol import (
    AuthenticationError,
    ProtocolError,
    ReadbackError,
)


async def test_proxy_device_lookup(hass):
    with (
        patch(
            "custom_components.busch_jaeger_1095_bt.connection.bluetooth.async_ble_device_from_address",
            return_value=sentinel.ble_device,
        ) as lookup,
        patch(
            "custom_components.busch_jaeger_1095_bt.connection.establish_connection",
            new=AsyncMock(return_value=sentinel.client),
        ) as connect,
    ):
        device = create_thermostat(hass, "AA:BB:CC:DD:EE:FF", "ab" * 32, 0x0201)
        assert await device._connection_factory() is sentinel.client
        lookup.assert_called_once_with(hass, "AA:BB:CC:DD:EE:FF", connectable=True)
        assert connect.call_args.args[1] is sentinel.ble_device


async def test_no_connectable_proxy(hass):
    with patch(
        "custom_components.busch_jaeger_1095_bt.connection.bluetooth.async_ble_device_from_address",
        return_value=None,
    ):
        device = create_thermostat(hass, "AA:BB:CC:DD:EE:FF", "ab" * 32, 0x0201)
        with pytest.raises(ProtocolError, match="proxy"):
            await device._connection_factory()


@pytest.mark.parametrize("command, value", [("temperature", 21.5), ("eco", True)])
async def test_reconnect_does_not_replay_command(hass, command, value):
    from homeassistant.exceptions import HomeAssistantError

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="AA:BB:CC:DD:EE:FF",
        data={CONF_ADDRESS: "AA:BB:CC:DD:EE:FF", CONF_SECRET: "ab" * 32},
    )
    coordinator = ThermostatCoordinator(hass, entry)
    coordinator.device = AsyncMock()
    method = (
        coordinator.device.set_temperature
        if command == "temperature"
        else coordinator.device.set_eco
    )
    method.side_effect = TimeoutError
    coordinator.data = {"eco": False}
    coordinator.connected = True
    with pytest.raises(HomeAssistantError):
        await coordinator.async_command(command, value)
    method.assert_awaited_once_with(value)
    assert coordinator.data == {"eco": False}
    assert not coordinator.connected
    assert coordinator._reconnect.is_set()


async def test_disconnect_cleanup_on_auth_failure(hass):
    from homeassistant.exceptions import ConfigEntryAuthFailed

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="AA:BB:CC:DD:EE:FF",
        data={CONF_ADDRESS: "AA:BB:CC:DD:EE:FF", CONF_SECRET: "ab" * 32},
    )
    device = AsyncMock()
    device.connect.side_effect = AuthenticationError("failed")
    with patch(
        "custom_components.busch_jaeger_1095_bt.coordinator.create_thermostat", return_value=device
    ):
        coordinator = ThermostatCoordinator(hass, entry)
        with pytest.raises(ConfigEntryAuthFailed):
            await coordinator.async_start()
    device.close.assert_awaited_once()


async def test_reconnect_uses_fresh_session_and_clears_stale_state(hass):
    import asyncio

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="AA:BB:CC:DD:EE:FF",
        data={CONF_ADDRESS: "AA:BB:CC:DD:EE:FF", CONF_SECRET: "ab" * 32},
    )
    first, second = AsyncMock(), AsyncMock()
    reconnected = asyncio.Event()
    hold_connection = asyncio.Event()
    drop_first = asyncio.Event()

    async def first_listen(seconds):
        await drop_first.wait()

    first.listen.side_effect = first_listen

    async def second_connect():
        reconnected.set()

    async def second_listen(seconds):
        await hold_connection.wait()

    second.connect.side_effect = second_connect
    second.listen.side_effect = second_listen
    with (
        patch(
            "custom_components.busch_jaeger_1095_bt.coordinator.create_thermostat",
            side_effect=[first, second],
        ) as factory,
    ):
        coordinator = ThermostatCoordinator(hass, entry)
        await coordinator.async_start()
        coordinator.data = {"setpoint": 21.5, "eco": True}
        coordinator._reconnect.set()
        drop_first.set()
        try:
            await asyncio.wait_for(reconnected.wait(), timeout=10)
            assert coordinator.device is second
            assert coordinator.connected
            assert coordinator.data == {}
            assert factory.call_count == 2
            first.close.assert_awaited_once()
            second.set_temperature.assert_not_awaited()
            second.set_eco.assert_not_awaited()
        finally:
            await coordinator.async_stop()
    second.close.assert_awaited_once()


@pytest.mark.parametrize(
    "error, reconnect", [(ReadbackError("unsupported"), False), (TimeoutError(), True)]
)
async def test_readback_failure_preserves_accepted_preset_without_replay(hass, error, reconnect):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="AA:BB:CC:DD:EE:FF",
        data={CONF_ADDRESS: "AA:BB:CC:DD:EE:FF", CONF_SECRET: "ab" * 32},
    )
    coordinator = ThermostatCoordinator(hass, entry)
    coordinator.device = AsyncMock()
    coordinator.device.read_setpoint.side_effect = error
    coordinator.connected = True
    coordinator.data = {"eco": False, "setpoint": 21.5, "measured_temperature": 20.0}
    await coordinator.async_command("eco", True)
    coordinator.device.set_eco.assert_awaited_once_with(True)
    coordinator.device.set_temperature.assert_not_awaited()
    coordinator.device.read_setpoint.assert_awaited_once()
    assert coordinator.data == {"eco": True, "measured_temperature": 20.0}
    assert coordinator.connected is not reconnect
    assert coordinator._reconnect.is_set() is reconnect


async def test_preset_refresh_preserves_report_received_during_mode_writes(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="AA:BB:CC:DD:EE:FF",
        data={CONF_ADDRESS: "AA:BB:CC:DD:EE:FF", CONF_SECRET: "ab" * 32},
    )
    coordinator = ThermostatCoordinator(hass, entry)
    coordinator.device = AsyncMock()
    coordinator.connected = True
    coordinator.data = {"eco": False, "setpoint": 21.5}

    async def set_eco(enabled):
        coordinator._on_event({"kind": "state", "name": "setpoint", "value": 18.5})

    coordinator.device.set_eco.side_effect = set_eco
    coordinator.device.read_setpoint.side_effect = ReadbackError("unsupported")
    await coordinator.async_command("eco", True)
    assert coordinator.data == {"eco": True, "setpoint": 18.5}
    assert coordinator.connected
