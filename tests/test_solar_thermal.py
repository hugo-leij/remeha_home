"""Test the sensors of a solar thermal system and its energy."""

from __future__ import annotations

import copy
from datetime import timedelta

from aiohttp import ClientError
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import pytest

from .conftest import entity_id, load_fixture, refresh, setup_integration

SOLAR_THERMAL_ID = "solar-thermal-1"

# The details as reported by a BRÖTJE BLW Eco-R with solar thermal (#54), ids changed
DETAILS = {
    "solarThermalId": SOLAR_THERMAL_ID,
    "applianceId": "appliance",
    "solarThermalDeviceId": "device",
    "status": "Standby",
    "tankTemperature": 38.6,
    "panelTemperature": -1.04,
    "pumpSpeed": 0.0,
    "dhwTankNominalTemperature": 43.0,
    "dhwTankNominalTemperatureMin": 8.0,
    "dhwTankNominalTemperatureMax": 60.0,
    "dhwTankNominalTemperatureIncrement": 1.0,
    "chTankNominalTemperature": None,
    "chTankNominalTemperatureMin": 8.0,
    "chTankNominalTemperatureMax": 60.0,
    "chTankNominalTemperatureIncrement": 1.0,
}

SOLAR_PRODUCER = {
    "instanceWithinDevice": 1,
    "producerType": "SolarThermal",
    "energyType": "SolarThermal",
    "energyConsumptionCH": 0.0,
    "energyConsumptionDHW": 0.0,
    "energyConsumptionCooling": 0.0,
    "energyConsumptionTotal": 0.0,
    "energyProductionCH": None,
    "energyProductionDHW": 4.0,
    "energyProductionCooling": None,
    "energyProductionTotal": 4.0,
}


@pytest.fixture(autouse=True)
def solar_thermal(appliance, mock_api) -> None:
    """Give the appliance a solar thermal system with its details."""
    appliance["solarThermals"] = [
        {"solarThermalId": SOLAR_THERMAL_ID, "status": "Standby"}
    ]
    mock_api.async_get_solar_thermal.return_value = copy.deepcopy(DETAILS)


def sensor(hass: HomeAssistant, key: str):
    """Return the state of a sensor of the solar thermal system."""
    return hass.states.get(entity_id(hass, "sensor", SOLAR_THERMAL_ID, key))


def consumption_with_solar(solar: dict | None) -> dict:
    """Return the energy of today with a solar thermal producer, or without one."""
    response = load_fixture("consumption.json")
    if solar is not None:
        response["data"][0]["producerPerformanceStatistics"]["producers"].append(solar)
    return response


def solar_energy(hass: HomeAssistant):
    """Return the hot water energy sensor of the solar thermal producer."""
    (state,) = [
        state
        for state in hass.states.async_all("sensor")
        if state.attributes.get("friendly_name")
        == "Solar Thermal Hot Water Energy Delivered"
    ]
    return state


async def test_detail_sensors(hass: HomeAssistant, config_entry, mock_api, freezer) -> None:
    """The collector and tank temperature and the pump speed come from the details."""
    await setup_integration(hass, config_entry, mock_api)

    collector = sensor(hass, "details.panelTemperature")
    assert collector.state == "-1.04"
    assert collector.attributes["friendly_name"] == "Solar Thermal System Collector Temperature"
    assert collector.attributes["unit_of_measurement"] == "°C"
    assert sensor(hass, "details.tankTemperature").state == "38.6"
    pump = sensor(hass, "details.pumpSpeed")
    assert pump.state == "0.0"
    assert pump.attributes["unit_of_measurement"] == "%"
    assert sensor(hass, "details.dhwTankNominalTemperature").state == "43.0"
    setpoint = er.async_get(hass).async_get(
        entity_id(hass, "sensor", SOLAR_THERMAL_ID, "details.dhwTankNominalTemperature")
    )
    assert setpoint.entity_category == EntityCategory.DIAGNOSTIC

    # Requested again after 5 minutes, not every minute
    mock_api.async_get_solar_thermal.return_value = {
        **DETAILS,
        "panelTemperature": 45.5,
        "pumpSpeed": 60.0,
    }
    freezer.tick(timedelta(minutes=2))
    await refresh(hass, config_entry)
    assert sensor(hass, "details.panelTemperature").state == "-1.04"
    freezer.tick(timedelta(minutes=3))
    await refresh(hass, config_entry)
    assert sensor(hass, "details.panelTemperature").state == "45.5"
    assert sensor(hass, "details.pumpSpeed").state == "60.0"


async def test_only_reported_details(hass: HomeAssistant, config_entry, mock_api) -> None:
    """A detail that isn't reported gets no sensor."""
    details = copy.deepcopy(DETAILS)
    del details["pumpSpeed"]
    details["tankTemperature"] = None
    mock_api.async_get_solar_thermal.return_value = details
    await setup_integration(hass, config_entry, mock_api)

    assert sensor(hass, "details.panelTemperature") is not None
    assert entity_id(hass, "sensor", SOLAR_THERMAL_ID, "details.pumpSpeed") is None
    assert entity_id(hass, "sensor", SOLAR_THERMAL_ID, "details.tankTemperature") is None


async def test_failed_details_keep_values(
    hass: HomeAssistant, config_entry, mock_api, freezer
) -> None:
    """A failed request keeps the last details; without details only the status."""
    await setup_integration(hass, config_entry, mock_api)
    mock_api.async_get_solar_thermal.side_effect = ClientError("down")

    freezer.tick(timedelta(minutes=5))
    await refresh(hass, config_entry)
    assert sensor(hass, "details.panelTemperature").state == "-1.04"


async def test_no_details(hass: HomeAssistant, config_entry, mock_api) -> None:
    """Without details the solar thermal system only has its status."""
    mock_api.async_get_solar_thermal.side_effect = ClientError("down")
    await setup_integration(hass, config_entry, mock_api)

    assert sensor(hass, "status").state == "Standby"
    assert entity_id(hass, "sensor", SOLAR_THERMAL_ID, "details.panelTemperature") is None


async def test_solar_energy(hass: HomeAssistant, config_entry, mock_api, freezer) -> None:
    """The solar thermal producer gets its hot water energy, for the Energy dashboard.

    Its yield is hot water energy delivered; its heating energy is null.
    """
    freezer.move_to("2026-09-11 12:00:00")
    mock_api.async_get_consumption_data_for_today.return_value = consumption_with_solar(
        SOLAR_PRODUCER
    )
    await setup_integration(hass, config_entry, mock_api)

    energy = solar_energy(hass)
    assert energy.state == "4.0"
    assert energy.attributes["unit_of_measurement"] == "kWh"
    assert energy.attributes["state_class"] == "total_increasing"
    # Only the hot water energy: no electric or gas sensors for the sun
    assert not [
        state
        for state in hass.states.async_all("sensor")
        if state.attributes.get("friendly_name", "").startswith("Solar Thermal Heating")
    ]


async def test_missing_solar_producer_keeps_energy(
    hass: HomeAssistant, config_entry, mock_api, freezer
) -> None:
    """A solar thermal producer that disappears during the day keeps its energy.

    Remeha has left it out on whole days (#54). During the day a drop to 0 would
    be seen as a reset and count the day twice.
    """
    freezer.move_to("2026-09-11 12:00:00")
    mock_api.async_get_consumption_data_for_today.return_value = consumption_with_solar(
        SOLAR_PRODUCER
    )
    await setup_integration(hass, config_entry, mock_api)
    assert solar_energy(hass).state == "4.0"

    mock_api.async_get_consumption_data_for_today.return_value = consumption_with_solar(
        None
    )
    freezer.tick(timedelta(minutes=15))
    await refresh(hass, config_entry)
    assert solar_energy(hass).state == "4.0"

    mock_api.async_get_consumption_data_for_today.return_value = consumption_with_solar(
        {**SOLAR_PRODUCER, "energyProductionDHW": 6.0, "energyProductionTotal": 6.0}
    )
    freezer.tick(timedelta(minutes=15))
    await refresh(hass, config_entry)
    assert solar_energy(hass).state == "6.0"
