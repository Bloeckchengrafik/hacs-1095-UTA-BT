"""Diagnostics omit authentication credentials and Bluetooth addresses."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.helpers.redact import async_redact_data

from .const import CONF_SECRET, CONF_UPLOAD


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict:
    coordinator = entry.runtime_data
    return {
        "entry": async_redact_data(
            entry.as_dict(), {CONF_SECRET, CONF_PASSWORD, CONF_UPLOAD, CONF_ADDRESS, "unique_id"}
        ),
        "connected": coordinator.connected,
        "state": dict(coordinator.data),
        "protocol_revision": 10,
    }
