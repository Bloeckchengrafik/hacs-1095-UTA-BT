"""Use Home Assistant's adapter selection, including ESPHome Bluetooth proxies."""

from __future__ import annotations

from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant

from .const import NAME
from .protocol import ProtocolError, Thermostat


def create_thermostat(
    hass: HomeAssistant, address: str, secret: str, destination: int, on_event=None
) -> Thermostat:
    """Create a fresh session. Never start a scanner or connect by raw address."""

    async def connect():
        device = bluetooth.async_ble_device_from_address(hass, address, connectable=True)
        if device is None:
            raise ProtocolError(
                "No connectable Bluetooth adapter/proxy can currently reach the thermostat"
            )
        return await establish_connection(BleakClientWithServiceCache, device, NAME, max_attempts=3)

    return Thermostat(
        address,
        bytes.fromhex(secret),
        destination=destination,
        on_event=on_event,
        connection_factory=connect,
    )
