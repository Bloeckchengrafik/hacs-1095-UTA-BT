"""UI setup, Bluetooth discovery, reauthentication, and climate range options."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from bleak.exc import BleakError

try:
    import probatio as vol
except ImportError:  # Home Assistant before the schema library migration.
    import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.components.file_upload import process_uploaded_file
from homeassistant.const import CONF_ADDRESS, CONF_NAME, CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.helpers import selector

from .connection import create_thermostat
from .const import (
    CONF_DESTINATION,
    CONF_SECRET,
    CONF_UPLOAD,
    DEFAULT_DESTINATION,
    DEFAULT_MAX_TEMP,
    DEFAULT_MIN_TEMP,
    DOMAIN,
    NAME,
)
from .credentials import MAX_EXPORT_SIZE, derive_secret, validate_secret
from .protocol import AuthenticationError, ProtocolError


def _derive_upload(hass, upload_id: str, password: str) -> str:
    """Read and remove the temporary upload entirely on HA's executor."""
    with process_uploaded_file(hass, upload_id) as path:
        if Path(path).stat().st_size > MAX_EXPORT_SIZE:
            raise ValueError("Oversized installation export")
        return derive_secret(Path(path).read_bytes(), password)


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._address = ""
        self._name = NAME
        self._secret: str | None = None
        self._destination = DEFAULT_DESTINATION

    async def async_step_bluetooth(self, discovery_info: BluetoothServiceInfoBleak):
        if not discovery_info.connectable:
            return self.async_abort(reason="not_connectable")
        self._address = discovery_info.address.upper()
        await self.async_set_unique_id(self._address)
        self._abort_if_unique_id_configured()
        self.context["title_placeholders"] = {"name": f"{NAME} ({self._address})"}
        return await self.async_step_user()

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            address = user_input[CONF_ADDRESS].strip().upper()
            if not re.fullmatch(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}", address):
                errors[CONF_ADDRESS] = "invalid_address"
            else:
                self._address = address
                self._name = user_input.get(CONF_NAME, NAME).strip() or NAME
                await self.async_set_unique_id(address)
                self._abort_if_unique_id_configured()
                return await self.async_step_credentials()
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADDRESS, default=self._address): str,
                    vol.Optional(CONF_NAME, default=self._name): str,
                }
            ),
            errors=errors,
        )

    async def async_step_credentials(self, user_input=None):
        return self.async_show_menu(step_id="credentials", menu_options=["installation", "secret"])

    async def async_step_installation(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                self._secret = await self.hass.async_add_executor_job(
                    _derive_upload,
                    self.hass,
                    user_input[CONF_UPLOAD],
                    user_input[CONF_PASSWORD],
                )
            except (ValueError, OSError):
                errors["base"] = "invalid_export"
            else:
                # Keep only the derived secret. Do not put the upload/password
                # in flow context, config-entry data, errors, or logs.
                return await self.async_step_connect()
        return self.async_show_form(
            step_id="installation",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_UPLOAD): selector.FileSelector(
                        selector.FileSelectorConfig(accept=".gepinstallation")
                    ),
                    vol.Required(CONF_PASSWORD): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_secret(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                self._secret = validate_secret(user_input[CONF_SECRET])
            except ValueError:
                errors[CONF_SECRET] = "invalid_secret"
            else:
                return await self.async_step_connect()
        return self.async_show_form(
            step_id="secret",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SECRET): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_connect(self, user_input=None):
        device = create_thermostat(self.hass, self._address, self._secret, self._destination)
        error = None
        try:
            async with asyncio.timeout(60), device:
                pass  # Authentication validates setup without sending climate writes.
        except AuthenticationError:
            error = "invalid_auth"
        except (BleakError, ProtocolError, TimeoutError, OSError):
            error = "cannot_connect"
        if error:
            # The empty form retries with the in-memory secret without making
            # the user upload a file again after a transient connection failure.
            return self.async_show_form(
                step_id="connect", errors={"base": error}, data_schema=vol.Schema({})
            )
        data = {
            CONF_ADDRESS: self._address,
            CONF_SECRET: self._secret,
            CONF_DESTINATION: self._destination,
        }
        if self.source == config_entries.SOURCE_REAUTH:
            return self.async_update_reload_and_abort(self._get_reauth_entry(), data_updates=data)
        return self.async_create_entry(title=self._name, data=data)

    async def async_step_reauth(self, entry_data):
        entry = self._get_reauth_entry()
        self._address = entry_data[CONF_ADDRESS]
        self._name = entry.title
        self._destination = entry_data.get(CONF_DESTINATION, DEFAULT_DESTINATION)
        return await self.async_step_credentials()

    async def async_step_reauth_confirm(self, user_input=None):
        return await self.async_step_credentials()

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return OptionsFlow()


class OptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(self, user_input=None):
        errors = {}
        if user_input is not None:
            if user_input["min_temp"] >= user_input["max_temp"]:
                errors["base"] = "invalid_range"
            else:
                return self.async_create_entry(title="", data=user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "min_temp",
                        default=self.config_entry.options.get("min_temp", DEFAULT_MIN_TEMP),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=5,
                            max=35,
                            step=0.5,
                            mode=selector.NumberSelectorMode.BOX,
                            unit_of_measurement="°C",
                        )
                    ),
                    vol.Required(
                        "max_temp",
                        default=self.config_entry.options.get("max_temp", DEFAULT_MAX_TEMP),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=5,
                            max=35,
                            step=0.5,
                            mode=selector.NumberSelectorMode.BOX,
                            unit_of_measurement="°C",
                        )
                    ),
                }
            ),
            errors=errors,
        )
