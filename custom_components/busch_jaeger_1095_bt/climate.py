"""Temperature and Eco controls using only reported thermostat state."""

from __future__ import annotations

from homeassistant.components.climate import ClimateEntity, ClimateEntityFeature, HVACMode
from homeassistant.components.climate.const import PRESET_COMFORT, PRESET_ECO
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, CONF_ADDRESS, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DEFAULT_MAX_TEMP, DEFAULT_MIN_TEMP, DOMAIN, NAME
from .coordinator import ThermostatCoordinator


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([BuschJaegerClimate(entry, entry.runtime_data)])


class BuschJaegerClimate(CoordinatorEntity[ThermostatCoordinator], ClimateEntity):
    """A heating thermostat with an independently controlled Eco preset."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = 0.5
    _attr_precision = 0.1
    _attr_hvac_modes = [HVACMode.HEAT]
    _attr_preset_modes = [PRESET_COMFORT, PRESET_ECO]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.PRESET_MODE
    )

    def __init__(self, entry: ConfigEntry, coordinator: ThermostatCoordinator) -> None:
        super().__init__(coordinator)
        self.entry = entry
        self._attr_unique_id = entry.unique_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.unique_id)},
            connections={(CONNECTION_BLUETOOTH, entry.data[CONF_ADDRESS])},
            name=entry.title or NAME,
            manufacturer="Busch-Jaeger / ABB",
            model="1095 UTA-BT",
        )

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.connected

    @property
    def current_temperature(self) -> float | None:
        return self.coordinator.data.get("measured_temperature")

    @property
    def target_temperature(self) -> float | None:
        return self.coordinator.data.get("setpoint")

    @property
    def preset_mode(self) -> str | None:
        if "eco" not in self.coordinator.data:
            return None
        return PRESET_ECO if self.coordinator.data["eco"] else PRESET_COMFORT

    @property
    def hvac_mode(self) -> HVACMode:
        # Only the validated heating/preset functions are advertised. No guessed
        # relay demand, HVAC action, or untested power-off behavior is exposed.
        return HVACMode.HEAT

    @property
    def min_temp(self) -> float:
        return self.entry.options.get("min_temp", DEFAULT_MIN_TEMP)

    @property
    def max_temp(self) -> float:
        return self.entry.options.get("max_temp", DEFAULT_MAX_TEMP)

    async def async_set_temperature(self, **kwargs) -> None:
        value = kwargs.get(ATTR_TEMPERATURE)
        if not isinstance(value, (int, float)) or not self.min_temp <= value <= self.max_temp:
            raise HomeAssistantError(
                f"Temperature must be between {self.min_temp} and {self.max_temp} °C"
            )
        await self.coordinator.async_command("temperature", float(value))

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        if preset_mode not in self.preset_modes:
            raise HomeAssistantError("Unsupported thermostat preset")
        await self.coordinator.async_command("eco", preset_mode == PRESET_ECO)
