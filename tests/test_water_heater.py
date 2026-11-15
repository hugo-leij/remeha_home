"""Test the hot water temperature of the water heater."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from aiohttp import ClientResponseError
from homeassistant.components.water_heater import (
    ATTR_MAX_TEMP,
    ATTR_MIN_TEMP,
    ATTR_OPERATION_MODE,
    ATTR_TEMPERATURE,
    DOMAIN as WATER_HEATER_DOMAIN,
    SERVICE_SET_OPERATION_MODE,
    SERVICE_SET_TEMPERATURE,
    STATE_ECO,
)
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
import pytest

from custom_components.remeha_home.const import DOMAIN

from .conftest import HOT_WATER_ZONE_ID, setup_integration

WATER_HEATER = "water_heater.dhw"


async def set_temperature(hass: HomeAssistant, temperature: float) -> None:
    """Set the target temperature of the water heater."""
    await hass.services.async_call(
        WATER_HEATER_DOMAIN,
        SERVICE_SET_TEMPERATURE,
        {ATTR_ENTITY_ID: WATER_HEATER, ATTR_TEMPERATURE: temperature},
        blocking=True,
    )
    await hass.async_block_till_done()


async def test_combi_in_eco_shows_the_comfort_temperature(
    hass: HomeAssistant, config_entry, mock_api, hot_water_zone
) -> None:
    """A combi boiler in Eco shows its comfort temperature, with the comfort range.

    A combi boiler has no eco temperature: in Eco it still heats the tap water to
    the comfort setpoint, which Remeha reports as its target setpoint. The eco
    setpoint (10 on the measured combi boiler) isn't used.
    """
    hot_water_zone["dhwZoneMode"] = "Off"
    await setup_integration(hass, config_entry, mock_api)

    state = hass.states.get(WATER_HEATER)
    assert state.state == STATE_ECO
    assert state.attributes[ATTR_TEMPERATURE] == 60.0
    assert state.attributes[ATTR_MIN_TEMP] == 40.0
    assert state.attributes[ATTR_MAX_TEMP] == 65.0


async def test_combi_in_eco_sets_the_comfort_temperature(
    hass: HomeAssistant, config_entry, mock_api, hot_water_zone
) -> None:
    """Setting the temperature of a combi boiler in Eco changes its comfort setpoint."""
    hot_water_zone["dhwZoneMode"] = "Off"

    def set_comfort_setpoint(_zone_id, setpoint: float) -> None:
        hot_water_zone.update(comfortSetPoint=setpoint, targetSetpoint=setpoint)

    mock_api.async_set_dhw_comfort_setpoint.side_effect = set_comfort_setpoint
    await setup_integration(hass, config_entry, mock_api)

    await set_temperature(hass, 55.0)

    mock_api.async_set_dhw_comfort_setpoint.assert_awaited_once_with(
        HOT_WATER_ZONE_ID, 55.0
    )
    mock_api.async_set_dhw_reduced_setpoint.assert_not_awaited()
    assert hass.states.get(WATER_HEATER).attributes[ATTR_TEMPERATURE] == 55.0


async def test_combi_switched_to_eco_keeps_the_comfort_temperature(
    hass: HomeAssistant, config_entry, mock_api
) -> None:
    """Switching a combi boiler to Eco keeps the comfort temperature as its target.

    Until the next update, the water heater shows the target that Remeha reports
    for a combi boiler in Eco: the comfort setpoint.
    """
    await setup_integration(hass, config_entry, mock_api)
    coordinator = hass.data[DOMAIN][config_entry.entry_id]["coordinator"]

    # Without the update after the switch, to see the state before it
    with patch.object(coordinator, "async_request_refresh"):
        await hass.services.async_call(
            WATER_HEATER_DOMAIN,
            SERVICE_SET_OPERATION_MODE,
            {ATTR_ENTITY_ID: WATER_HEATER, ATTR_OPERATION_MODE: STATE_ECO},
            blocking=True,
        )

    mock_api.async_set_dhw_mode_eco.assert_awaited_once_with(HOT_WATER_ZONE_ID)
    state = hass.states.get(WATER_HEATER)
    assert state.state == STATE_ECO
    assert state.attributes[ATTR_TEMPERATURE] == 60.0
    assert state.attributes[ATTR_MIN_TEMP] == 40.0
    assert state.attributes[ATTR_MAX_TEMP] == 65.0
    assert coordinator.get_by_id(HOT_WATER_ZONE_ID)["targetSetpoint"] == 60.0


async def test_tank_in_eco_shows_and_sets_the_eco_temperature(
    hass: HomeAssistant, config_entry, mock_api, hot_water_zone
) -> None:
    """A hot water tank in Eco shows and sets its eco setpoint, with the eco range."""
    hot_water_zone.update(
        dhwType="Tank",
        dhwZoneMode="Off",
        dhwTemperature=45.0,
        targetSetpoint=40.0,
        reducedSetpoint=40.0,
    )
    await setup_integration(hass, config_entry, mock_api)

    state = hass.states.get(WATER_HEATER)
    assert state.attributes[ATTR_TEMPERATURE] == 40.0
    assert state.attributes[ATTR_MIN_TEMP] == 10.0
    assert state.attributes[ATTR_MAX_TEMP] == 60.0

    await set_temperature(hass, 35.0)

    mock_api.async_set_dhw_reduced_setpoint.assert_awaited_once_with(
        HOT_WATER_ZONE_ID, 35.0
    )
    mock_api.async_set_dhw_comfort_setpoint.assert_not_awaited()


async def test_failed_temperature_change_keeps_the_old_temperature(
    hass: HomeAssistant, config_entry, mock_api
) -> None:
    """When the API refuses the new temperature, the old one is shown again."""
    error = ClientResponseError(MagicMock(), (), status=500)
    mock_api.async_set_dhw_comfort_setpoint.side_effect = error
    mock_api.async_set_dhw_reduced_setpoint.side_effect = error
    await setup_integration(hass, config_entry, mock_api)
    old_temperature = hass.states.get(WATER_HEATER).attributes[ATTR_TEMPERATURE]

    with pytest.raises(ClientResponseError):
        await hass.services.async_call(
            WATER_HEATER_DOMAIN,
            SERVICE_SET_TEMPERATURE,
            {ATTR_ENTITY_ID: WATER_HEATER, ATTR_TEMPERATURE: old_temperature - 5},
            blocking=True,
        )
    await hass.async_block_till_done()

    assert hass.states.get(WATER_HEATER).attributes[ATTR_TEMPERATURE] == old_temperature
    # Also in the data of the coordinator, which the next state update shows
    entity = hass.data[WATER_HEATER_DOMAIN].get_entity(WATER_HEATER)
    assert entity.target_temperature == old_temperature
