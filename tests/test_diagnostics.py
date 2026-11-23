"""Test the diagnostics download."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock

from aiohttp import ClientResponseError
import pytest
from homeassistant.components.diagnostics import REDACTED
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)

from custom_components.remeha_home.const import DOMAIN

from .conftest import (
    APPLIANCE_ID,
    CLIMATE_ZONE_ID,
    HOT_WATER_ZONE_ID,
    load_fixture,
    refresh,
    setup_integration,
)

EMAIL = "user@example.com"
TOKEN = {
    "access_token": "secret-access-token",
    "refresh_token": "secret-refresh-token",
    "id_token": "secret-id-token",
    "token_type": "Bearer",
    "expires_at": 4102444800,
}


@pytest.fixture
def config_entry() -> MockConfigEntry:
    """Return a config entry titled with the email address, like the config flow."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=EMAIL,
        data={"auth_implementation": DOMAIN, "token": TOKEN},
    )


def string_values(data) -> list[str]:
    """Return all strings in the data, without the keys."""
    if isinstance(data, dict):
        return [text for value in data.values() for text in string_values(value)]
    if isinstance(data, list):
        return [text for value in data for text in string_values(value)]
    return [data] if isinstance(data, str) else []


async def test_sections(
    hass: HomeAssistant, hass_client, config_entry, mock_api
) -> None:
    """The download holds the data that the integration uses."""
    await setup_integration(hass, config_entry, mock_api)

    diagnostics = await get_diagnostics_for_config_entry(
        hass, hass_client, config_entry
    )

    assert diagnostics["entry"]["domain"] == DOMAIN
    appliance = diagnostics["dashboard"]["appliances"][0]
    assert appliance["applianceId"] == APPLIANCE_ID
    assert appliance["waterPressure"] == 1.7
    assert appliance["consumptionData"]["heatingEnergyConsumed"] == 5.0
    climate_zone = appliance["climateZones"][0]
    assert climate_zone["climateZoneId"] == CLIMATE_ZONE_ID
    assert climate_zone["heatingCurve"] == load_fixture("heating_curve.json")
    assert appliance["hotWaterZones"][0]["hotWaterZoneId"] == HOT_WATER_ZONE_ID

    technical_info = diagnostics["technical_info"][APPLIANCE_ID]
    assert technical_info["applianceName"] == "Elga Ace"
    gateway = technical_info["internetConnectedGateways"][0]
    assert gateway["softwareVersion"] == "2.28.6"

    activities = diagnostics["activities"][CLIMATE_ZONE_ID]
    assert [
        (activity["type"], activity["activityNumber"], activity["temperature"])
        for activity in activities
    ] == [
        (activity["type"], activity["activityNumber"], activity["temperature"])
        for activity in load_fixture("activities.json")
    ]


async def test_personal_data_redacted(
    hass: HomeAssistant, hass_client, config_entry, mock_api
) -> None:
    """The login, the names typed in the app and the serial numbers are left out."""
    technical = load_fixture("technical.json")
    technical["internetConnectedGateways"][0]["controlledZoneName"] = "Woonkamer"
    mock_api.async_get_appliance_technical_information.return_value = technical
    await setup_integration(hass, config_entry, mock_api)

    diagnostics = await get_diagnostics_for_config_entry(
        hass, hass_client, config_entry
    )

    assert diagnostics["entry"]["title"] == REDACTED
    assert diagnostics["entry"]["data"]["token"] == REDACTED
    appliance = diagnostics["dashboard"]["appliances"][0]
    assert appliance["houseName"] == REDACTED
    assert appliance["climateZones"][0]["name"] == REDACTED
    assert appliance["hotWaterZones"][0]["name"] == REDACTED
    statistics = appliance["consumptionData"]["producerPerformanceStatistics"]
    assert {producer["deviceIndex"] for producer in statistics["producers"]} == {
        REDACTED
    }
    technical_info = diagnostics["technical_info"][APPLIANCE_ID]
    gateway = technical_info["internetConnectedGateways"][0]
    assert gateway["name"] == REDACTED
    assert gateway["serialNumber"] == REDACTED
    assert gateway["controlledZoneName"] == REDACTED
    activities = diagnostics["activities"][CLIMATE_ZONE_ID]
    assert {activity["name"] for activity in activities} == {REDACTED}

    values = string_values(diagnostics)
    for private in (
        EMAIL,
        TOKEN["access_token"],
        TOKEN["refresh_token"],
        TOKEN["id_token"],
        "Home",
        "Woonkamer",
        "00000000",
        "000000000000",
        "Thuis",
        "Werken",
    ):
        assert private not in values, private

    # The data of the integration itself is not changed
    coordinator = hass.data[DOMAIN][config_entry.entry_id]["coordinator"]
    assert coordinator.data["appliances"][0]["houseName"] == "Home"


SOLAR_THERMAL_ID = "solar-thermal-1"


async def test_solar_thermal_details(
    hass: HomeAssistant, hass_client, config_entry, mock_api, appliance, freezer
) -> None:
    """The details of a solar thermal system are in the diagnostics.

    Its response isn't known yet, so the diagnostics are the way to find out what
    it reports. It is requested every 15 minutes.
    """
    appliance["solarThermals"] = [
        {"solarThermalId": SOLAR_THERMAL_ID, "status": "Standby"}
    ]
    mock_api.async_get_solar_thermal.return_value = {
        "solarThermalId": SOLAR_THERMAL_ID,
        "name": "Solar",
        "collectorTemperature": 6.0,
    }
    await setup_integration(hass, config_entry, mock_api)
    mock_api.async_get_solar_thermal.assert_awaited_once_with(SOLAR_THERMAL_ID)

    diagnostics = await get_diagnostics_for_config_entry(
        hass, hass_client, config_entry
    )
    details = diagnostics["solar_thermals"][SOLAR_THERMAL_ID]
    assert details["collectorTemperature"] == 6.0
    assert details["name"] == REDACTED

    freezer.tick(timedelta(minutes=5))
    await refresh(hass, config_entry)
    assert mock_api.async_get_solar_thermal.await_count == 1
    freezer.tick(timedelta(minutes=10))
    await refresh(hass, config_entry)
    assert mock_api.async_get_solar_thermal.await_count == 2


async def test_solar_thermal_details_fail(
    hass: HomeAssistant, hass_client, config_entry, mock_api, appliance, freezer
) -> None:
    """A failed request doesn't break the update, and isn't repeated every minute."""
    appliance["solarThermals"] = [
        {"solarThermalId": SOLAR_THERMAL_ID, "status": "Standby"}
    ]
    mock_api.async_get_solar_thermal.side_effect = ClientResponseError(
        MagicMock(), (), status=404
    )
    await setup_integration(hass, config_entry, mock_api)

    assert hass.states.get("climate.woonkamer") is not None
    freezer.tick(timedelta(minutes=1))
    await refresh(hass, config_entry)
    assert mock_api.async_get_solar_thermal.await_count == 1

    diagnostics = await get_diagnostics_for_config_entry(
        hass, hass_client, config_entry
    )
    assert diagnostics["solar_thermals"] == {}
