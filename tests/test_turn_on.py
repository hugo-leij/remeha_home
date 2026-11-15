"""Test turning a climate zone on and off."""

from __future__ import annotations

import pytest
from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN, HVACMode
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant

from .conftest import CLIMATE_ZONE_ID, setup_integration

CLIMATE = "climate.woonkamer"


async def turn(hass: HomeAssistant, service: str) -> None:
    """Turn the climate zone on or off."""
    await hass.services.async_call(
        CLIMATE_DOMAIN, service, {ATTR_ENTITY_ID: CLIMATE}, blocking=True
    )


async def test_turn_on_follows_the_clock_program(
    hass: HomeAssistant, config_entry, mock_api, climate_zone
) -> None:
    """Turning on a zone that is off makes it follow its clock program again.

    Without this, Home Assistant selects heat (or heat/cool): manual mode, which
    holds the setpoint the zone has in frost protection.
    """
    climate_zone.update(
        zoneMode="FrostProtection",
        setPoint=6.0,
        activeHeatingClimateTimeProgramNumber=2,
    )
    await setup_integration(hass, config_entry, mock_api)
    mock_api.async_set_schedule.side_effect = lambda *args: climate_zone.update(
        zoneMode="Scheduling"
    )

    await turn(hass, SERVICE_TURN_ON)

    mock_api.async_set_schedule.assert_awaited_once_with(CLIMATE_ZONE_ID, 2)
    mock_api.async_set_manual.assert_not_awaited()
    assert hass.states.get(CLIMATE).state == HVACMode.AUTO


@pytest.mark.parametrize(
    ("zone_mode", "state"),
    [
        ("Scheduling", HVACMode.AUTO),
        ("TemporaryOverride", HVACMode.AUTO),
        ("Manual", HVACMode.HEAT_COOL),
        ("SomeNewMode", STATE_UNKNOWN),
    ],
)
async def test_turn_on_keeps_the_mode(
    hass: HomeAssistant, config_entry, mock_api, climate_zone, zone_mode, state
) -> None:
    """Turning on a zone that is on, or in a mode that isn't known, changes nothing."""
    climate_zone["zoneMode"] = zone_mode
    await setup_integration(hass, config_entry, mock_api)

    await turn(hass, SERVICE_TURN_ON)

    mock_api.async_set_schedule.assert_not_awaited()
    mock_api.async_set_manual.assert_not_awaited()
    mock_api.async_set_temporary_override.assert_not_awaited()
    mock_api.async_set_off.assert_not_awaited()
    assert hass.states.get(CLIMATE).state == state


async def test_turn_off(
    hass: HomeAssistant, config_entry, mock_api, climate_zone
) -> None:
    """Turning off sets the zone to frost protection."""
    climate_zone["zoneMode"] = "Scheduling"
    await setup_integration(hass, config_entry, mock_api)
    mock_api.async_set_off.side_effect = lambda *args: climate_zone.update(
        zoneMode="FrostProtection"
    )

    await turn(hass, SERVICE_TURN_OFF)

    mock_api.async_set_off.assert_awaited_once_with(CLIMATE_ZONE_ID)
    assert hass.states.get(CLIMATE).state == HVACMode.OFF
