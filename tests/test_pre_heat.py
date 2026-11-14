"""Test the switch that turns pre-heat of a climate zone on or off."""

from __future__ import annotations

import copy
from datetime import timedelta

import pytest
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
    EntityCategory,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .conftest import (
    APPLIANCE_ID,
    CLIMATE_ZONE_ID,
    entity_id,
    refresh,
    setup_integration,
)

KEY = "preHeat.enabled"
OTHER_ZONE_ID = "00000000-0000-0000-0000-00000000c002"


def switch(hass: HomeAssistant, zone_id: str = CLIMATE_ZONE_ID) -> str | None:
    """Return the entity id of the pre-heat switch of a climate zone."""
    return entity_id(hass, "switch", zone_id, KEY)


async def turn(
    hass: HomeAssistant, service: str, zone_id: str = CLIMATE_ZONE_ID
) -> None:
    """Turn the pre-heat switch of a climate zone on or off."""
    await hass.services.async_call(
        SWITCH_DOMAIN, service, {ATTR_ENTITY_ID: switch(hass, zone_id)}, blocking=True
    )


def add_other_zone(appliance: dict, climate_zone: dict) -> dict:
    """Add a second climate zone to the appliance, and return it."""
    other = copy.deepcopy(climate_zone)
    other.update(climateZoneId=OTHER_ZONE_ID, name="Zolder")
    appliance["climateZones"].append(other)
    return other


async def test_switch(hass: HomeAssistant, config_entry, mock_api) -> None:
    """The switch shows whether pre-heat is on, as a setting of the zone."""
    await setup_integration(hass, config_entry, mock_api)

    state = hass.states.get(switch(hass))
    assert state.state == STATE_OFF
    assert state.attributes["friendly_name"] == "Woonkamer Pre-heat"
    entry = er.async_get(hass).async_get(switch(hass))
    assert entry.entity_category is EntityCategory.CONFIG
    sensor = entity_id(hass, "binary_sensor", CLIMATE_ZONE_ID, "preHeat.active")
    assert hass.states.get(sensor).attributes["friendly_name"] == (
        "Woonkamer Pre-heat active"
    )


@pytest.mark.parametrize(
    ("service", "enabled", "expected"),
    [(SERVICE_TURN_ON, True, STATE_ON), (SERVICE_TURN_OFF, False, STATE_OFF)],
)
async def test_turn_on_and_off(
    hass: HomeAssistant, config_entry, mock_api, climate_zone, service, enabled, expected
) -> None:
    """Turning the switch sends pre-heat for the zone."""
    climate_zone["preHeat"] = {"enabled": not enabled, "active": False}
    await setup_integration(hass, config_entry, mock_api)
    # Like the appliance, the next read reports the new setting
    climate_zone["preHeat"] = {"enabled": enabled, "active": False}

    await turn(hass, service)

    mock_api.async_set_pre_heat.assert_awaited_once_with(
        APPLIANCE_ID, {CLIMATE_ZONE_ID: enabled}
    )
    assert hass.states.get(switch(hass)).state == expected


async def test_other_zones_keep_their_setting(
    hass: HomeAssistant, config_entry, mock_api, appliance, climate_zone
) -> None:
    """The request lists every zone of the appliance, the others unchanged."""
    other = add_other_zone(appliance, climate_zone)
    other["preHeat"] = {"enabled": True, "active": False}
    await setup_integration(hass, config_entry, mock_api)

    await turn(hass, SERVICE_TURN_ON)

    mock_api.async_set_pre_heat.assert_awaited_once_with(
        APPLIANCE_ID, {CLIMATE_ZONE_ID: True, OTHER_ZONE_ID: True}
    )


async def test_written_setting_is_kept(
    hass: HomeAssistant, config_entry, mock_api, appliance, climate_zone, freezer
) -> None:
    """A change of another zone sends what was written, not a read that lags behind.

    The read right after a write can answer with the setting from before it. Every
    request lists all zones, so a request built from that read sends the old setting
    back for the zone that was just changed.
    """
    climate_zone["preHeat"] = {"enabled": True, "active": False}
    add_other_zone(appliance, climate_zone)
    await setup_integration(hass, config_entry, mock_api)
    # The dashboard keeps answering with pre-heat on for both zones

    await turn(hass, SERVICE_TURN_OFF)
    await refresh(hass, config_entry)
    assert hass.states.get(switch(hass)).state == STATE_OFF

    freezer.tick(timedelta(minutes=1))
    await turn(hass, SERVICE_TURN_OFF, OTHER_ZONE_ID)
    mock_api.async_set_pre_heat.assert_awaited_with(
        APPLIANCE_ID, {CLIMATE_ZONE_ID: False, OTHER_ZONE_ID: False}
    )
    assert hass.states.get(switch(hass)).state == STATE_OFF
    assert hass.states.get(switch(hass, OTHER_ZONE_ID)).state == STATE_OFF

    # After the settle time the dashboard wins: the appliance didn't take the change
    freezer.tick(timedelta(minutes=1))
    await refresh(hass, config_entry)
    assert hass.states.get(switch(hass)).state == STATE_ON
    assert hass.states.get(switch(hass, OTHER_ZONE_ID)).state == STATE_OFF


async def test_written_setting_is_kept_by_a_read_in_flight(
    hass: HomeAssistant, config_entry, mock_api, appliance, climate_zone, freezer
) -> None:
    """A read that started before a write doesn't undo it.

    An update reads the dashboard first and the heating curve and other data after
    it, so a write can land while that update is still running. Its dashboard is
    then from before the write.
    """
    climate_zone["preHeat"] = {"enabled": True, "active": False}
    add_other_zone(appliance, climate_zone)
    await setup_integration(hass, config_entry, mock_api)
    # The dashboard keeps answering with pre-heat on for both zones

    async def turn_off_while_reading(climate_zone_id: str) -> dict:
        if climate_zone_id == CLIMATE_ZONE_ID:
            freezer.tick(timedelta(seconds=1))
            await turn(hass, SERVICE_TURN_OFF)
        return mock_api.async_get_heating_curve.return_value

    freezer.tick(timedelta(minutes=15))
    mock_api.async_get_heating_curve.side_effect = turn_off_while_reading
    await refresh(hass, config_entry)
    assert hass.states.get(switch(hass)).state == STATE_OFF

    mock_api.async_get_heating_curve.side_effect = None
    freezer.tick(timedelta(seconds=30))
    await refresh(hass, config_entry)
    assert hass.states.get(switch(hass)).state == STATE_OFF
    await turn(hass, SERVICE_TURN_OFF, OTHER_ZONE_ID)
    mock_api.async_set_pre_heat.assert_awaited_with(
        APPLIANCE_ID, {CLIMATE_ZONE_ID: False, OTHER_ZONE_ID: False}
    )


async def test_written_setting_ends_when_reported(
    hass: HomeAssistant, config_entry, mock_api, climate_zone, freezer
) -> None:
    """Once the dashboard reports the written setting, it follows the dashboard again."""
    climate_zone["preHeat"] = {"enabled": True, "active": False}
    await setup_integration(hass, config_entry, mock_api)

    await turn(hass, SERVICE_TURN_OFF)
    climate_zone["preHeat"] = {"enabled": False, "active": False}
    await refresh(hass, config_entry)
    assert hass.states.get(switch(hass)).state == STATE_OFF

    # Turned on again in the Remeha Home app, within the settle time of the write
    climate_zone["preHeat"] = {"enabled": True, "active": False}
    freezer.tick(timedelta(seconds=30))
    await refresh(hass, config_entry)
    assert hass.states.get(switch(hass)).state == STATE_ON


async def test_refused_change_is_reverted(
    hass: HomeAssistant, config_entry, mock_api
) -> None:
    """When the API refuses, the switch shows the old setting again."""
    await setup_integration(hass, config_entry, mock_api)
    mock_api.async_set_pre_heat.side_effect = RuntimeError("refused")

    with pytest.raises(RuntimeError):
        await turn(hass, SERVICE_TURN_ON)

    assert hass.states.get(switch(hass)).state == STATE_OFF


async def test_no_switch_without_capability(
    hass: HomeAssistant, config_entry, mock_api, appliance
) -> None:
    """An appliance without pre-heat gets no switch."""
    appliance["capabilityPreHeat"] = False
    await setup_integration(hass, config_entry, mock_api)

    assert switch(hass) is None


async def test_no_switch_without_pre_heat_data(
    hass: HomeAssistant, config_entry, mock_api, climate_zone
) -> None:
    """A zone that doesn't report pre-heat gets no switch."""
    del climate_zone["preHeat"]
    await setup_integration(hass, config_entry, mock_api)

    assert switch(hass) is None
