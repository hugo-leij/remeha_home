"""Test that the modes of the zones are restored after a holiday."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import homeassistant.util.dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.remeha_home.holiday import (
    REFRESH_DELAY,
    RESTORE_ATTEMPTS,
    storage_key,
)

from .conftest import CLIMATE_ZONE_ID, HOT_WATER_ZONE_ID, refresh, setup_integration

NOT_SET = "0001-01-01T00:00:01Z"


def set_holiday(appliance: dict, active: bool) -> None:
    """Schedule a holiday from today to tomorrow."""
    today = dt_util.now().date()
    appliance["holidaySchedule"] = {
        "startTime": f"{today}T00:00:01Z",
        "endTime": f"{today + timedelta(days=1)}T00:00:01Z",
        "active": active,
    }


def start_holiday(appliance: dict, climate_zone: dict, hot_water_zone: dict) -> None:
    """Start a holiday, like the appliance does."""
    set_holiday(appliance, active=True)
    climate_zone["zoneMode"] = "FrostProtection"
    climate_zone["setPoint"] = 6.0
    hot_water_zone["dhwZoneMode"] = "Off"


def end_holiday(appliance: dict) -> None:
    """End the holiday, leaving the zones as they are."""
    appliance["holidaySchedule"] = {"startTime": NOT_SET, "endTime": NOT_SET, "active": False}


async def wait(hass: HomeAssistant, freezer, seconds: float) -> None:
    """Let time pass, and run what was planned for it."""
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


async def update(hass: HomeAssistant, config_entry) -> None:
    """Fetch the dashboard again and run the restore it starts."""
    await refresh(hass, config_entry)
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_restore_schedule(
    hass: HomeAssistant,
    config_entry,
    mock_api,
    appliance,
    climate_zone,
    hot_water_zone,
    freezer,
    hass_storage,
) -> None:
    """A zone that followed its clock program gets the schedule mode again.

    The appliance can leave such a zone in frost protection for minutes, or until
    the schedule mode is set again.
    """
    climate_zone["zoneMode"] = "Scheduling"
    await setup_integration(hass, config_entry, mock_api)

    start_holiday(appliance, climate_zone, hot_water_zone)
    await update(hass, config_entry)
    await wait(hass, freezer, 2)
    assert hass_storage[storage_key(config_entry)]["data"]["appliances"]

    end_holiday(appliance)
    await update(hass, config_entry)
    mock_api.async_set_schedule.assert_awaited_once_with(CLIMATE_ZONE_ID, 1)
    mock_api.async_set_dhw_mode_schedule.assert_awaited_once_with(HOT_WATER_ZONE_ID)
    mock_api.async_set_manual.assert_not_awaited()

    # The API has applied the modes when the integration checks them again
    climate_zone["zoneMode"] = "Scheduling"
    climate_zone["setPoint"] = 20.0
    hot_water_zone["dhwZoneMode"] = "Scheduling"
    await wait(hass, freezer, REFRESH_DELAY)
    await wait(hass, freezer, 2)

    mock_api.async_set_schedule.assert_awaited_once()
    mock_api.async_set_dhw_mode_schedule.assert_awaited_once()
    assert hass_storage[storage_key(config_entry)]["data"] == {"appliances": {}}


async def test_schedule_set_again_when_reported(
    hass: HomeAssistant, config_entry, mock_api, appliance, climate_zone, hot_water_zone
) -> None:
    """The schedule mode is set again once when the zone already reports it.

    Such a zone can keep the frost protection setpoint until the schedule mode
    is set again.
    """
    climate_zone["zoneMode"] = "Scheduling"
    await setup_integration(hass, config_entry, mock_api)
    start_holiday(appliance, climate_zone, hot_water_zone)
    await update(hass, config_entry)

    end_holiday(appliance)
    climate_zone["zoneMode"] = "Scheduling"
    hot_water_zone["dhwZoneMode"] = "Scheduling"
    await update(hass, config_entry)

    mock_api.async_set_schedule.assert_awaited_once_with(CLIMATE_ZONE_ID, 1)
    mock_api.async_set_dhw_mode_schedule.assert_not_awaited()


async def test_manual_restored_by_appliance(
    hass: HomeAssistant, config_entry, mock_api, appliance, climate_zone, hot_water_zone
) -> None:
    """Nothing is sent when the appliance has restored the zones itself.

    Measured on a live system: a zone in manual mode and the hot water come back.
    """
    assert climate_zone["zoneMode"] == "Manual"
    await setup_integration(hass, config_entry, mock_api)
    start_holiday(appliance, climate_zone, hot_water_zone)
    await update(hass, config_entry)

    end_holiday(appliance)
    climate_zone["zoneMode"] = "Manual"
    climate_zone["setPoint"] = 20.0
    hot_water_zone["dhwZoneMode"] = "Scheduling"
    await update(hass, config_entry)

    mock_api.async_set_manual.assert_not_awaited()
    mock_api.async_set_schedule.assert_not_awaited()
    mock_api.async_set_dhw_mode_schedule.assert_not_awaited()


async def test_restore_manual(
    hass: HomeAssistant, config_entry, mock_api, appliance, climate_zone, hot_water_zone
) -> None:
    """A zone in manual mode that stays in frost protection gets its setpoint back."""
    climate_zone["setPoint"] = 20.5
    await setup_integration(hass, config_entry, mock_api)
    start_holiday(appliance, climate_zone, hot_water_zone)
    await update(hass, config_entry)

    end_holiday(appliance)
    await update(hass, config_entry)

    mock_api.async_set_manual.assert_awaited_once_with(CLIMATE_ZONE_ID, 20.5)


async def test_restore_continuous_comfort(
    hass: HomeAssistant, config_entry, mock_api, appliance, climate_zone, hot_water_zone
) -> None:
    """Hot water in continuous comfort gets that mode back."""
    hot_water_zone["dhwZoneMode"] = "ContinuousComfort"
    await setup_integration(hass, config_entry, mock_api)
    start_holiday(appliance, climate_zone, hot_water_zone)
    await update(hass, config_entry)

    end_holiday(appliance)
    await update(hass, config_entry)

    mock_api.async_set_dhw_mode_comfort.assert_awaited_once_with(HOT_WATER_ZONE_ID)
    mock_api.async_set_dhw_mode_schedule.assert_not_awaited()


async def test_frost_protection_before_holiday(
    hass: HomeAssistant, config_entry, mock_api, appliance, climate_zone, hot_water_zone
) -> None:
    """Zones that were off before the holiday stay off."""
    climate_zone["zoneMode"] = "FrostProtection"
    hot_water_zone["dhwZoneMode"] = "Off"
    await setup_integration(hass, config_entry, mock_api)
    start_holiday(appliance, climate_zone, hot_water_zone)
    await update(hass, config_entry)

    end_holiday(appliance)
    await update(hass, config_entry)

    mock_api.async_set_schedule.assert_not_awaited()
    mock_api.async_set_manual.assert_not_awaited()
    mock_api.async_set_dhw_mode_schedule.assert_not_awaited()


async def test_modes_before_holiday_reported_active(
    hass: HomeAssistant, config_entry, mock_api, appliance, climate_zone, hot_water_zone
) -> None:
    """The modes are saved before the dates of a holiday that starts today.

    The appliance can switch the zones before it reports the holiday as active.
    """
    climate_zone["zoneMode"] = "Scheduling"
    await setup_integration(hass, config_entry, mock_api)

    start_holiday(appliance, climate_zone, hot_water_zone)
    appliance["holidaySchedule"]["active"] = False
    await update(hass, config_entry)
    appliance["holidaySchedule"]["active"] = True
    await update(hass, config_entry)

    end_holiday(appliance)
    await update(hass, config_entry)

    mock_api.async_set_schedule.assert_awaited_once_with(CLIMATE_ZONE_ID, 1)


async def test_future_holiday(
    hass: HomeAssistant, config_entry, mock_api, appliance, climate_zone, hot_water_zone
) -> None:
    """The modes are saved when a holiday starts, not when it is scheduled."""
    tomorrow = dt_util.now().date() + timedelta(days=1)
    appliance["holidaySchedule"] = {
        "startTime": f"{tomorrow}T00:00:01Z",
        "endTime": f"{tomorrow + timedelta(days=1)}T00:00:01Z",
        "active": False,
    }
    await setup_integration(hass, config_entry, mock_api)

    # Changed after the holiday was scheduled
    climate_zone["zoneMode"] = "Scheduling"
    await update(hass, config_entry)
    start_holiday(appliance, climate_zone, hot_water_zone)
    await update(hass, config_entry)

    end_holiday(appliance)
    await update(hass, config_entry)

    mock_api.async_set_schedule.assert_awaited_once_with(CLIMATE_ZONE_ID, 1)
    mock_api.async_set_manual.assert_not_awaited()


async def test_restart_during_holiday(
    hass: HomeAssistant,
    config_entry,
    mock_api,
    appliance,
    climate_zone,
    hot_water_zone,
    freezer,
) -> None:
    """The saved modes are kept when Home Assistant restarts during the holiday."""
    climate_zone["zoneMode"] = "Scheduling"
    await setup_integration(hass, config_entry, mock_api)
    start_holiday(appliance, climate_zone, hot_water_zone)
    await update(hass, config_entry)
    await wait(hass, freezer, 2)

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    await setup_integration(hass, config_entry, mock_api)

    end_holiday(appliance)
    await update(hass, config_entry)

    mock_api.async_set_schedule.assert_awaited_once_with(CLIMATE_ZONE_ID, 1)


async def test_no_modes_saved(
    hass: HomeAssistant, config_entry, mock_api, appliance, climate_zone, hot_water_zone
) -> None:
    """Nothing is restored when the modes before the holiday are unknown.

    That is the case when the integration was set up during the holiday.
    """
    start_holiday(appliance, climate_zone, hot_water_zone)
    await setup_integration(hass, config_entry, mock_api)

    end_holiday(appliance)
    await update(hass, config_entry)

    mock_api.async_set_schedule.assert_not_awaited()
    mock_api.async_set_manual.assert_not_awaited()
    mock_api.async_set_dhw_mode_schedule.assert_not_awaited()


async def test_give_up(
    hass: HomeAssistant,
    config_entry,
    mock_api,
    appliance,
    climate_zone,
    hot_water_zone,
    freezer,
    caplog,
) -> None:
    """The restore is given up when the zone doesn't change."""
    climate_zone["zoneMode"] = "Scheduling"
    mock_api.async_set_schedule.side_effect = RuntimeError("rejected")
    await setup_integration(hass, config_entry, mock_api)
    start_holiday(appliance, climate_zone, hot_water_zone)
    await update(hass, config_entry)

    end_holiday(appliance)
    await update(hass, config_entry)
    for _ in range(RESTORE_ATTEMPTS + 1):
        await wait(hass, freezer, REFRESH_DELAY)

    assert mock_api.async_set_schedule.await_count == RESTORE_ATTEMPTS
    assert "Failed to restore a zone mode after the holiday: rejected" in caplog.text
    assert "Could not restore the modes of the zones" in caplog.text

    await wait(hass, freezer, REFRESH_DELAY)
    await update(hass, config_entry)
    assert mock_api.async_set_schedule.await_count == RESTORE_ATTEMPTS


async def test_remove_entry(
    hass: HomeAssistant,
    config_entry,
    mock_api,
    appliance,
    climate_zone,
    hot_water_zone,
    freezer,
    hass_storage,
) -> None:
    """The saved modes are removed with the integration."""
    await setup_integration(hass, config_entry, mock_api)
    start_holiday(appliance, climate_zone, hot_water_zone)
    await update(hass, config_entry)
    await wait(hass, freezer, 2)
    assert storage_key(config_entry) in hass_storage

    assert await hass.config_entries.async_remove(config_entry.entry_id)
    await hass.async_block_till_done()

    assert storage_key(config_entry) not in hass_storage


@pytest.mark.parametrize(
    ("control", "mode", "api_name", "preserve", "fails"),
    [
        ("heating", "heat_cool", "async_set_manual", True, False),
        ("hot_water", "performance", "async_set_dhw_mode_comfort", True, False),
        ("heating", "off", "async_set_off", False, False),
        ("preset", "anti_frost", "async_set_off", False, False),
        ("hot_water", "eco", "async_set_dhw_mode_eco", False, False),
        ("heating", "heat_cool", "async_set_manual", False, True),
        ("hot_water", "performance", "async_set_dhw_mode_comfort", False, True),
    ],
)
async def test_zone_commands_across_holiday_reload(
    hass: HomeAssistant,
    config_entry,
    mock_api,
    appliance,
    climate_zone,
    hot_water_zone,
    freezer,
    hass_storage,
    control,
    mode,
    api_name,
    preserve,
    fails,
) -> None:
    """Only a successful mode change beyond the holiday mode prevents restoration."""
    climate_zone["zoneMode"] = "Scheduling"
    await setup_integration(hass, config_entry, mock_api)
    start_holiday(appliance, climate_zone, hot_water_zone)
    await update(hass, config_entry)
    await wait(hass, freezer, 2)
    registry = er.async_get(hass)
    if control != "hot_water":
        target = registry.async_get_entity_id(
            "climate", "remeha_home", f"remeha_home_{CLIMATE_ZONE_ID}"
        )
        domain, zone_id = "climate", CLIMATE_ZONE_ID
        if control == "preset":
            action, field = "set_preset_mode", "preset_mode"
        else:
            action, field = "set_hvac_mode", "hvac_mode"
    else:
        target = registry.async_get_entity_id(
            "water_heater",
            "remeha_home",
            f"remeha_home_{HOT_WATER_ZONE_ID}_water_heater",
        )
        domain, zone_id = "water_heater", HOT_WATER_ZONE_ID
        action, field = "set_operation_mode", "operation_mode"

    api_call = getattr(mock_api, api_name)
    if fails:
        api_call.side_effect = RuntimeError("rejected")
        with pytest.raises(RuntimeError, match="rejected"):
            await hass.services.async_call(
                domain, action, {"entity_id": target, field: mode}, blocking=True
            )
    else:
        await hass.services.async_call(
            domain, action, {"entity_id": target, field: mode}, blocking=True
        )
    assert api_call.await_count == 1

    saved = next(
        iter(hass_storage[storage_key(config_entry)]["data"]["appliances"].values())
    )
    assert (zone_id in saved.get("excludedZones", ())) is preserve
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    await setup_integration(hass, config_entry, mock_api)
    # The appliance can still report its holiday modes when the holiday ends.
    climate_zone["zoneMode"] = "FrostProtection"
    hot_water_zone["dhwZoneMode"] = "Off"
    end_holiday(appliance)
    await update(hass, config_entry)

    if preserve and control == "heating":
        mock_api.async_set_schedule.assert_not_awaited()
    else:
        mock_api.async_set_schedule.assert_awaited_once_with(CLIMATE_ZONE_ID, 1)
    if preserve and control == "hot_water":
        mock_api.async_set_dhw_mode_schedule.assert_not_awaited()
    else:
        mock_api.async_set_dhw_mode_schedule.assert_awaited_once_with(HOT_WATER_ZONE_ID)


async def test_cancel_boost_during_holiday_restores_hot_water(
    hass: HomeAssistant,
    config_entry,
    mock_api,
    appliance,
    climate_zone,
    hot_water_zone,
    hass_storage,
) -> None:
    """Cancelling a boost during the holiday makes the original DHW mode eligible again."""
    await setup_integration(hass, config_entry, mock_api)
    start_holiday(appliance, climate_zone, hot_water_zone)
    await update(hass, config_entry)
    # Remeha only accepts boost in schedule mode.
    hot_water_zone["dhwZoneMode"] = "Scheduling"
    await update(hass, config_entry)
    target = {"entity_id": "switch.dhw_boost"}
    await hass.services.async_call("switch", "turn_on", target, blocking=True)
    mock_api.async_set_hot_water_boost.assert_awaited_once_with(HOT_WATER_ZONE_ID)
    saved = next(
        iter(hass_storage[storage_key(config_entry)]["data"]["appliances"].values())
    )
    assert HOT_WATER_ZONE_ID in saved["excludedZones"]

    hot_water_zone["dhwZoneMode"] = "Boost"
    await update(hass, config_entry)
    await hass.services.async_call("switch", "turn_off", target, blocking=True)
    mock_api.async_set_dhw_mode_schedule.assert_awaited_once_with(HOT_WATER_ZONE_ID)

    mock_api.async_set_dhw_mode_schedule.reset_mock()
    hot_water_zone["dhwZoneMode"] = "Off"
    end_holiday(appliance)
    await update(hass, config_entry)
    mock_api.async_set_dhw_mode_schedule.assert_awaited_once_with(HOT_WATER_ZONE_ID)
