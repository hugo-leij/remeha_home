"""Platform for number entities (DHW setpoints and heating curve)."""

from __future__ import annotations
import logging

from homeassistant.components.number import NumberDeviceClass, NumberEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, PRECISION_HALVES, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import RemehaHomeAPI
from .const import DOMAIN
from .coordinator import RemehaHomeUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up number entities from a config entry."""
    api = hass.data[DOMAIN][entry.entry_id]["api"]
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

    entities = []

    for appliance in coordinator.data["appliances"]:
        # DHW setpoint numbers
        for hot_water_zone in appliance["hotWaterZones"]:
            hot_water_zone_id = hot_water_zone["hotWaterZoneId"]
            entities.append(
                RemehaHomeDhwSetpointNumber(
                    api,
                    coordinator,
                    hot_water_zone_id,
                    setpoint_type="comfort",
                    name="Comfort Temperature",
                    key="comfortSetPoint",
                    icon="mdi:water-thermometer",
                )
            )
            entities.append(
                RemehaHomeDhwSetpointNumber(
                    api,
                    coordinator,
                    hot_water_zone_id,
                    setpoint_type="eco",
                    name="Eco Temperature",
                    key="reducedSetpoint",
                    icon="mdi:water-thermometer-outline",
                )
            )

        # Heating curve numbers
        for climate_zone in appliance["climateZones"]:
            climate_zone_id = climate_zone["climateZoneId"]
            if coordinator.get_heating_curve(climate_zone_id) is not None:
                entities.append(
                    RemehaHomeHeatingCurveNumber(
                        api, coordinator, climate_zone_id, "slope"
                    )
                )
                entities.append(
                    RemehaHomeHeatingCurveNumber(
                        api, coordinator, climate_zone_id, "base_setpoint"
                    )
                )

    async_add_entities(entities)


class RemehaHomeDhwSetpointNumber(CoordinatorEntity, NumberEntity):
    """Number entity for DHW setpoints."""

    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_native_step = PRECISION_HALVES

    def __init__(
        self,
        api: RemehaHomeAPI,
        coordinator: RemehaHomeUpdateCoordinator,
        hot_water_zone_id: str,
        *,
        setpoint_type: str,
        name: str,
        key: str,
        icon: str,
    ) -> None:
        """Create a DHW setpoint number."""
        super().__init__(coordinator)
        self.api = api
        self.hot_water_zone_id = hot_water_zone_id
        self.setpoint_type = setpoint_type
        self._attr_name = name
        self.key = key
        self._attr_unique_id = "_".join([DOMAIN, self.hot_water_zone_id, key])
        self._attr_icon = icon

    @property
    def _data(self):
        """Return zone data."""
        return self.coordinator.get_by_id(self.hot_water_zone_id)

    @property
    def native_value(self) -> float | None:
        """Return current setpoint value."""
        return self._data.get(self.key)

    @property
    def native_min_value(self) -> float | None:
        """Return minimum allowed value for this setpoint."""
        ranges = self._data.get("setPointRanges") or {}
        if self.setpoint_type == "comfort":
            return ranges.get("comfortSetpointMin", self._data.get("setPointMin"))
        return ranges.get("reducedSetpointMin", self._data.get("setPointMin"))

    @property
    def native_max_value(self) -> float | None:
        """Return maximum allowed value for this setpoint."""
        ranges = self._data.get("setPointRanges") or {}
        if self.setpoint_type == "comfort":
            return ranges.get("comfortSetpointMax", self._data.get("setPointMax"))
        return ranges.get("reducedSetpointMax", self._data.get("setPointMax"))

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info for this device."""
        return self.coordinator.get_device_info(self.hot_water_zone_id)

    async def async_set_native_value(self, value: float) -> None:
        """Update the setpoint."""
        if self.setpoint_type == "comfort":
            await self.api.async_set_dhw_comfort_setpoint(
                self.hot_water_zone_id, value
            )
        else:
            await self.api.async_set_dhw_reduced_setpoint(
                self.hot_water_zone_id, value
            )
        await self.coordinator.async_request_refresh()


# Mapping from our parameter names to API response field names
HEATING_CURVE_PARAMS = {
    "slope": {
        "value_key": "slope",
        "min_key": "minimumSlope",
        "max_key": "maximumSlope",
        "step_key": "incrementSlope",
        "name": "Heating Curve Slope",
        "icon": "mdi:chart-line",
        "unit": None,
        "device_class": None,
    },
    "base_setpoint": {
        "value_key": "baseSetpoint",
        "min_key": "minimumBaseSetpoint",
        "max_key": "maximumBaseSetpoint",
        "step_key": "incrementBaseSetpoint",
        "name": "Heating Curve Base Setpoint",
        "icon": None,
        "unit": UnitOfTemperature.CELSIUS,
        "device_class": NumberDeviceClass.TEMPERATURE,
    },
}


class RemehaHomeHeatingCurveNumber(CoordinatorEntity, NumberEntity):
    """Representation of a heating curve number entity."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        api: RemehaHomeAPI,
        coordinator: RemehaHomeUpdateCoordinator,
        climate_zone_id: str,
        parameter: str,
    ) -> None:
        """Create a Remeha Home heating curve number entity."""
        super().__init__(coordinator)
        self.api = api
        self.climate_zone_id = climate_zone_id
        self.parameter = parameter
        self._param_config = HEATING_CURVE_PARAMS[parameter]

        key = f"heating_curve_{parameter}"
        self._attr_unique_id = "_".join([DOMAIN, self.climate_zone_id, key])
        self._attr_name = self._param_config["name"]
        self._attr_icon = self._param_config["icon"]
        self._attr_native_unit_of_measurement = self._param_config["unit"]
        self._attr_device_class = self._param_config["device_class"]

    @property
    def _heating_curve_data(self) -> dict | None:
        """Return the heating curve data from the coordinator."""
        return self.coordinator.get_heating_curve(self.climate_zone_id)

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return super().available and self._heating_curve_data is not None

    @property
    def native_value(self) -> float | None:
        """Return the current value."""
        data = self._heating_curve_data
        if data is None:
            return None
        return data.get(self._param_config["value_key"])

    @property
    def native_min_value(self) -> float:
        """Return the minimum value."""
        data = self._heating_curve_data
        if data is None:
            return 0.0
        return data.get(self._param_config["min_key"], 0.0)

    @property
    def native_max_value(self) -> float:
        """Return the maximum value."""
        data = self._heating_curve_data
        if data is None:
            return 100.0
        return data.get(self._param_config["max_key"], 100.0)

    @property
    def native_step(self) -> float:
        """Return the step value."""
        data = self._heating_curve_data
        if data is None:
            return 0.1
        return data.get(self._param_config["step_key"], 0.1)

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info for this device."""
        return self.coordinator.get_device_info(self.climate_zone_id)

    async def async_set_native_value(self, value: float) -> None:
        """Set the heating curve parameter value."""
        data = self._heating_curve_data
        if data is None:
            return

        # The API requires both slope and baseSetpoint in every POST
        if self.parameter == "slope":
            slope = value
            base_setpoint = data["baseSetpoint"]
        else:
            slope = data["slope"]
            base_setpoint = value

        _LOGGER.debug(
            "Setting heating curve for zone %s: slope=%s, base_setpoint=%s",
            self.climate_zone_id,
            slope,
            base_setpoint,
        )
        await self.api.async_set_heating_curve(
            self.climate_zone_id, slope, base_setpoint
        )

        # Invalidate cache to force re-fetch on next update
        self.coordinator.heating_curve_data.pop(self.climate_zone_id, None)
        await self.coordinator.async_request_refresh()
