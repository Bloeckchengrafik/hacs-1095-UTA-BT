"""Maintain an authenticated session and publish device-reported state."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from bleak.exc import BleakError
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .connection import create_thermostat
from .const import CONF_DESTINATION, CONF_SECRET, DEFAULT_DESTINATION, DOMAIN
from .protocol import AuthenticationError, ProtocolError, ReadbackError, Thermostat

_LOGGER = logging.getLogger(__name__)


class ThermostatCoordinator(DataUpdateCoordinator):
    """One connection per thermostat, with fresh key exchange after a reconnect."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN)
        self.entry = entry
        self.device: Thermostat | None = None
        self.connected = False
        self._stopping = False
        self._task: asyncio.Task | None = None
        self._reconnect = asyncio.Event()
        self._last_state_at = 0.0
        self._setpoint_revision = 0
        self.data = {}

    @callback
    def _on_event(self, event: dict) -> None:
        kind = event["kind"]
        if kind == "state":
            if event["name"] == "setpoint":
                self._setpoint_revision += 1
            self._last_state_at = time.monotonic()
            self.async_set_updated_data({**self.data, event["name"]: event["value"]})
        elif kind == "receive_error":
            self.connected = False
            self.async_set_update_error(UpdateFailed("Bluetooth protocol receive failed"))
            self._reconnect.set()

    async def _connect(self) -> None:
        device = create_thermostat(
            self.hass,
            self.entry.data[CONF_ADDRESS],
            self.entry.data[CONF_SECRET],
            self.entry.data.get(CONF_DESTINATION, DEFAULT_DESTINATION),
            self._on_event,
        )
        try:
            async with asyncio.timeout(60):
                await device.connect()
        except BaseException:
            await device.close()
            raise
        self.device = device
        self.connected = True
        self._last_state_at = time.monotonic()
        # Reports belong to this authenticated session. Do not restore stale values.
        self.async_set_updated_data({})

    async def async_start(self) -> None:
        try:
            await self._connect()
        except AuthenticationError as exc:
            raise ConfigEntryAuthFailed("Thermostat authentication failed") from exc
        except (BleakError, ProtocolError, TimeoutError, OSError) as exc:
            raise ConfigEntryNotReady("Cannot reach thermostat through Bluetooth") from exc
        self._task = self.hass.async_create_background_task(
            self._maintain(), f"{DOMAIN} connection"
        )

    async def _maintain(self) -> None:
        delay = 5
        while not self._stopping:
            try:
                while not self._reconnect.is_set():
                    await self.device.listen(5)
                    if time.monotonic() - self._last_state_at > 90:
                        raise ProtocolError("No temperature reports for 90 seconds")
            except (BleakError, ProtocolError, TimeoutError, OSError):
                self.connected = False
                self.async_set_update_error(UpdateFailed("Bluetooth connection interrupted"))
            finally:
                if self.device:
                    await self.device.close()
                self.device = None
                self.connected = False
            self._reconnect.clear()
            while not self._stopping:
                await asyncio.sleep(delay)
                try:
                    await self._connect()
                except AuthenticationError:
                    self.async_set_update_error(UpdateFailed("Thermostat authentication failed"))
                    self.entry.async_start_reauth(self.hass)
                    return
                except (BleakError, ProtocolError, TimeoutError, OSError):
                    delay = min(delay * 2, 60)
                else:
                    delay = 5
                    break

    async def async_stop(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self.device:
            await self.device.close()
            self.device = None
        self.connected = False

    async def async_command(self, command: str, value) -> None:
        device = self.device
        if device is None or not self.connected:
            raise HomeAssistantError("Thermostat is disconnected; wait for Bluetooth reconnection")
        setpoint_revision = self._setpoint_revision
        try:
            if command == "temperature":
                await device.set_temperature(value)
            elif command == "eco":
                await device.set_eco(value)
                # Some firmware does not emit a mode report after these writes.
                # Both application replies have succeeded at this point; publish
                # the accepted preset so HA can select the opposite one again.
                # Unsolicited device reports still replace this value normally.
                data = {**self.data, "eco": bool(value)}
                # Preserve reports received during the writes; otherwise the
                # old mode's effective target is stale until refreshed.
                if self._setpoint_revision == setpoint_revision:
                    data.pop("setpoint", None)
                self.async_set_updated_data(data)
            else:
                raise ValueError("Unsupported thermostat command")
        except (BleakError, ProtocolError, TimeoutError, OSError) as exc:
            self.connected = False
            self.async_set_update_error(UpdateFailed("Thermostat command failed"))
            self._reconnect.set()
            # A timed-out write may already have been applied; never replay it.
            raise HomeAssistantError(
                "Command could not be confirmed; reconnecting without replaying it"
            ) from exc

        if command == "eco":
            # Allow the controller's control cycle to apply the accepted mode.
            await asyncio.sleep(0.3)
            try:
                await device.read_setpoint()
            except ReadbackError as exc:
                # Unsupported reads must not undo or repeat an accepted preset.
                _LOGGER.warning("Preset accepted, but effective setpoint read failed: %s", exc)
            except (BleakError, ProtocolError, TimeoutError, OSError):
                self.connected = False
                self.async_set_update_error(UpdateFailed("Setpoint refresh connection failed"))
                self._reconnect.set()
                _LOGGER.warning(
                    "Preset accepted, but setpoint refresh lost the connection; reconnecting"
                )
