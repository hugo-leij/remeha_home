"""Test the error status, operating mode and override end sensors."""

from __future__ import annotations

import logging

import pytest
from homeassistant.const import STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from custom_components.remeha_home.const import DOMAIN

from .conftest import (
    APPLIANCE_ID,
    CLIMATE_ZONE_ID,
    entity_id,
    refresh,
    setup_integration,
)

OVERRIDE_END = "temporaryOverride.endTime"


@pytest.fixture(autouse=True)
async def amsterdam(hass: HomeAssistant) -> None:
    """Use the time zone of the fixtures, east of UTC."""
    await hass.config.async_set_time_zone("Europe/Amsterdam")


async def test_error_status(
    hass: HomeAssistant, config_entry, mock_api, appliance
) -> None:
    """The Error Status sensor shows the status reported by Remeha."""
    await setup_integration(hass, config_entry, mock_api)
    sensor = entity_id(hass, "sensor", APPLIANCE_ID, "errorStatus")
    assert hass.states.get(sensor).state == "Running"

    appliance["errorStatus"] = "Error"
    await refresh(hass, config_entry)
    assert hass.states.get(sensor).state == "Error"


async def test_operating_mode(
    hass: HomeAssistant, config_entry, mock_api, appliance, caplog
) -> None:
    """The Operating Mode sensor shows what the appliance is set to do.

    The thermal mode shows what it is doing now: the fixture is idle in AutomaticHeating.
    """
    await setup_integration(hass, config_entry, mock_api)
    sensor = entity_id(hass, "sensor", APPLIANCE_ID, "operatingMode")
    assert hass.states.get(sensor).state == "AutomaticHeating"
    thermal_mode = entity_id(hass, "sensor", APPLIANCE_ID, "activeThermalMode")
    assert hass.states.get(thermal_mode).state == "Idle"

    appliance["operatingMode"] = "Off"
    await refresh(hass, config_entry)
    assert hass.states.get(sensor).state == "Off"

    appliance["operatingMode"] = None
    await refresh(hass, config_entry)
    assert hass.states.get(sensor).state == STATE_UNKNOWN

    del appliance["operatingMode"]
    await refresh(hass, config_entry)
    assert hass.states.get(sensor).state == STATE_UNKNOWN
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


async def test_no_override(hass: HomeAssistant, config_entry, mock_api) -> None:
    """Without a temporary override, the end is unknown and the attribute None."""
    await setup_integration(hass, config_entry, mock_api)

    sensor = entity_id(hass, "sensor", CLIMATE_ZONE_ID, OVERRIDE_END)
    assert hass.states.get(sensor).state == STATE_UNKNOWN
    assert hass.states.get("climate.woonkamer").attributes["override_end_time"] is None


async def test_override(
    hass: HomeAssistant, config_entry, mock_api, climate_zone
) -> None:
    """The end of a temporary override is in UTC, unlike the next setpoint time.

    Measured on a live system: an override until the schedule resumes at 22:00 CEST
    ends at 20:00:00Z, while the next setpoint time of 22:00 CEST is 22:00:00Z.
    """
    climate_zone["zoneMode"] = "TemporaryOverride"
    climate_zone["temporaryOverride"] = {"endTime": "2026-10-22T20:00:00Z"}
    climate_zone["nextSwitchTime"] = "2026-10-22T22:00:00Z"
    await setup_integration(hass, config_entry, mock_api)

    override_end = hass.states.get(entity_id(hass, "sensor", CLIMATE_ZONE_ID, OVERRIDE_END))
    next_switch = hass.states.get(
        entity_id(hass, "sensor", CLIMATE_ZONE_ID, "nextSwitchTime")
    )
    assert override_end.state == "2026-10-22T20:00:00+00:00"
    assert override_end.state == next_switch.state
    climate = hass.states.get("climate.woonkamer")
    assert climate.state == "auto"
    assert climate.attributes["override_end_time"] == "2026-10-22T22:00:00+02:00"


async def test_override_null(
    hass: HomeAssistant, config_entry, mock_api, climate_zone, caplog
) -> None:
    """A temporary override that becomes null or missing is unknown, not an error."""
    climate_zone["temporaryOverride"] = {"endTime": "2026-10-22T20:00:00Z"}
    await setup_integration(hass, config_entry, mock_api)
    sensor = entity_id(hass, "sensor", CLIMATE_ZONE_ID, OVERRIDE_END)
    assert hass.states.get(sensor).state == "2026-10-22T20:00:00+00:00"

    climate_zone["temporaryOverride"] = None
    await refresh(hass, config_entry)
    assert hass.states.get(sensor).state == STATE_UNKNOWN
    assert hass.states.get("climate.woonkamer").attributes["override_end_time"] is None

    del climate_zone["temporaryOverride"]
    await refresh(hass, config_entry)
    assert hass.states.get(sensor).state == STATE_UNKNOWN
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


async def test_zone_mode(hass: HomeAssistant, config_entry, mock_api, climate_zone) -> None:
    """The zone mode tells a temporary override apart from the clock program.

    Both show as auto, and an override until the next switch of the clock program
    doesn't need an end time.
    """
    climate_zone["zoneMode"] = "Scheduling"
    await setup_integration(hass, config_entry, mock_api)
    climate = hass.states.get("climate.woonkamer")
    assert climate.state == "auto"
    assert climate.attributes["zone_mode"] == "Scheduling"

    climate_zone["zoneMode"] = "TemporaryOverride"
    climate_zone["temporaryOverride"] = {"endTime": "0001-01-01T00:00:00Z"}
    await refresh(hass, config_entry)
    climate = hass.states.get("climate.woonkamer")
    assert climate.state == "auto"
    assert climate.attributes["zone_mode"] == "TemporaryOverride"
    assert climate.attributes["override_end_time"] is None


SOLAR_THERMAL_ID = "solar-thermal-1"


async def test_solar_thermal_status(
    hass: HomeAssistant, config_entry, mock_api, appliance
) -> None:
    """A solar thermal system gets a device with its status."""
    appliance["solarThermals"] = [
        {"solarThermalId": SOLAR_THERMAL_ID, "status": "Standby"}
    ]
    await setup_integration(hass, config_entry, mock_api)

    sensor = entity_id(hass, "sensor", SOLAR_THERMAL_ID, "status")
    state = hass.states.get(sensor)
    assert state.state == "Standby"
    assert state.attributes["friendly_name"] == "Solar Thermal System Status"
    registry = dr.async_get(hass)
    (device,) = [
        device
        for device in dr.async_entries_for_config_entry(registry, config_entry.entry_id)
        if (DOMAIN, SOLAR_THERMAL_ID) in device.identifiers
    ]
    appliance_device = registry.async_get(device.via_device_id)
    assert (DOMAIN, appliance["applianceId"]) in appliance_device.identifiers

    appliance["solarThermals"][0]["status"] = "Charging"
    await refresh(hass, config_entry)
    assert hass.states.get(sensor).state == "Charging"


async def test_no_solar_thermal(
    hass: HomeAssistant, config_entry, mock_api, appliance
) -> None:
    """Without a solar thermal system, or with an invalid one, nothing is added."""
    appliance["solarThermals"] = [{"status": "Standby"}, "invalid"]
    await setup_integration(hass, config_entry, mock_api)

    assert not [
        state
        for state in hass.states.async_all("sensor")
        if state.attributes.get("friendly_name", "").startswith("Solar Thermal")
    ]
