"""Test the daily energy sensors around midnight.

Remeha reports energy about 2 hours late, so it keeps adding the last hours of a day
after midnight. The sensors show yesterday's figures until it is complete.

The coordinator uses the naive local time, which is the frozen time as given here.
"""

from __future__ import annotations

import copy
from datetime import date
from unittest.mock import MagicMock

from aiohttp import ClientResponseError
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import pytest

from custom_components.remeha_home.const import DOMAIN

from .conftest import APPLIANCE_ID, entity_id, load_fixture, refresh, setup_integration

HEAT_PUMP = f"{APPLIANCE_ID}_1"
# The heating energy of the appliance as a whole, disabled by default
APPLIANCE_TOTAL = "consumptionData.heatingEnergyConsumed"

YESTERDAY = "2026-11-10"
TODAY = "2026-11-11"
EMPTY = {"data": []}


def reported(day: str, heat_pump: float, total: float) -> dict:
    """Return a response with the figures of a day.

    heat_pump is the electricity of the heat pump for heating, total the heating
    energy of the appliance.
    """
    response = load_fixture("consumption.json")
    item = response["data"][0]
    item["timeStamp"] = f"{day}T00:00:00+00:00"
    item["heatingEnergyConsumed"] = total
    item["producerPerformanceStatistics"]["producers"][0]["energyConsumptionCH"] = (
        heat_pump
    )
    return response


def not_reported(day: str) -> dict:
    """Return the item of a new day before its figures come in: all 0, no producers."""
    return {
        "data": [
            {
                "isDataReported": False,
                "timeStamp": f"{day}T00:00:00+00:00",
                "heatingEnergyConsumed": 0,
                "hotWaterEnergyConsumed": 0,
                "coolingEnergyConsumed": 0,
                "heatingEnergyDelivered": 0,
                "hotWaterEnergyDelivered": 0,
                "coolingEnergyDelivered": 0,
                "producerPerformanceStatistics": {"producers": []},
            }
        ]
    }


@pytest.fixture
def responses(mock_api) -> dict:
    """Return the responses for today and for the range, which a test can change."""
    responses = {"today": EMPTY, "range": EMPTY}
    mock_api.async_get_consumption_data_for_today.side_effect = (
        lambda appliance_id: copy.deepcopy(responses["today"])
    )
    mock_api.async_get_consumption_data_for_range.side_effect = (
        lambda appliance_id, start, end: copy.deepcopy(responses["range"])
    )
    return responses


async def start(hass: HomeAssistant, config_entry, mock_api) -> None:
    """Set up the integration with the energy total of the appliance enabled."""
    config_entry.add_to_hass(hass)
    er.async_get(hass).async_get_or_create(
        "sensor",
        DOMAIN,
        f"{DOMAIN}_{APPLIANCE_ID}_{APPLIANCE_TOTAL}",
        config_entry=config_entry,
    )
    await setup_integration(hass, config_entry, mock_api)


async def update_at(hass: HomeAssistant, config_entry, freezer, time: str) -> None:
    """Fetch the data at the given time."""
    freezer.move_to(time)
    await refresh(hass, config_entry)


def appliance_total(hass: HomeAssistant) -> float:
    """Return the heating energy of the appliance."""
    return float(
        hass.states.get(entity_id(hass, "sensor", APPLIANCE_ID, APPLIANCE_TOTAL)).state
    )


def figures(hass: HomeAssistant) -> tuple[float, float]:
    """Return the heating electricity of the heat pump and the appliance's total."""
    heat_pump = hass.states.get(
        entity_id(hass, "sensor", HEAT_PUMP, "energyConsumptionCH")
    )
    return float(heat_pump.state), appliance_total(hass)


async def test_after_midnight_the_sensors_show_yesterday(
    hass: HomeAssistant, config_entry, mock_api, responses, freezer
) -> None:
    """Remeha keeps adding to yesterday, so the sensors keep counting it."""
    freezer.move_to(f"{YESTERDAY} 23:50:00")
    responses["today"] = reported(YESTERDAY, 10.0, 12.0)
    await start(hass, config_entry, mock_api)
    assert figures(hass) == (10.0, 12.0)
    mock_api.async_get_consumption_data_for_range.assert_not_awaited()

    # Not 0 while Remeha has nothing for today yet
    responses["today"] = EMPTY
    responses["range"] = reported(YESTERDAY, 11.0, 13.5)
    await update_at(hass, config_entry, freezer, f"{TODAY} 00:05:00")
    assert figures(hass) == (11.0, 13.5)
    mock_api.async_get_consumption_data_for_range.assert_awaited_once_with(
        APPLIANCE_ID, date(2026, 11, 10), date(2026, 11, 10)
    )

    # Yesterday's figures staying the same don't make it complete while today isn't
    # reported: Remeha may not have filed the last hours yet
    await update_at(hass, config_entry, freezer, f"{TODAY} 00:20:00")
    assert figures(hass) == (11.0, 13.5)

    responses["today"] = not_reported(TODAY)
    responses["range"] = reported(YESTERDAY, 11.5, 14.0)
    await update_at(hass, config_entry, freezer, f"{TODAY} 00:35:00")
    assert figures(hass) == (11.5, 14.0)

    await update_at(hass, config_entry, freezer, f"{TODAY} 00:50:00")
    assert figures(hass) == (11.5, 14.0)
    assert mock_api.async_get_consumption_data_for_range.await_count == 4


async def test_new_day_once_today_is_reported_and_yesterday_is_complete(
    hass: HomeAssistant, config_entry, mock_api, responses, freezer
) -> None:
    """The sensors go to 0 for one update and then show today's figures."""
    freezer.move_to(f"{YESTERDAY} 23:50:00")
    responses["today"] = reported(YESTERDAY, 10.0, 12.0)
    await start(hass, config_entry, mock_api)

    # Today is reported, but yesterday still changes
    responses["today"] = reported(TODAY, 0.5, 0.6)
    responses["range"] = reported(YESTERDAY, 11.0, 13.5)
    await update_at(hass, config_entry, freezer, f"{TODAY} 01:00:00")
    assert figures(hass) == (11.0, 13.5)

    # Yesterday is complete. Home Assistant needs the 0 to start counting again,
    # also when today's figures are close to yesterday's.
    responses["today"] = reported(TODAY, 10.5, 13.0)
    await update_at(hass, config_entry, freezer, f"{TODAY} 01:15:00")
    assert figures(hass) == (0.0, 0.0)
    assert mock_api.async_get_consumption_data_for_range.await_count == 2

    # Today's figures at the next update, a minute later
    await update_at(hass, config_entry, freezer, f"{TODAY} 01:16:00")
    assert figures(hass) == (10.5, 13.0)

    responses["today"] = reported(TODAY, 11.0, 13.6)
    await update_at(hass, config_entry, freezer, f"{TODAY} 01:31:00")
    assert figures(hass) == (11.0, 13.6)
    assert mock_api.async_get_consumption_data_for_range.await_count == 2


@pytest.mark.parametrize(
    "range_response",
    [EMPTY, reported(TODAY, 1.0, 1.2), not_reported(YESTERDAY)],
    ids=["empty", "today", "not_reported"],
)
async def test_missing_yesterday_keeps_the_values(
    hass: HomeAssistant, config_entry, mock_api, responses, freezer, range_response
) -> None:
    """Without an item of yesterday the sensors keep showing its last figures."""
    freezer.move_to(f"{YESTERDAY} 23:50:00")
    responses["today"] = reported(YESTERDAY, 10.0, 12.0)
    await start(hass, config_entry, mock_api)

    responses["today"] = reported(TODAY, 1.0, 1.2)
    responses["range"] = range_response
    await update_at(hass, config_entry, freezer, f"{TODAY} 02:00:00")
    assert figures(hass) == (10.0, 12.0)

    responses["range"] = reported(YESTERDAY, 11.0, 13.5)
    await update_at(hass, config_entry, freezer, f"{TODAY} 02:15:00")
    assert figures(hass) == (11.0, 13.5)


async def test_failed_request_of_yesterday_is_retried(
    hass: HomeAssistant, config_entry, mock_api, responses, freezer
) -> None:
    """Like a failed request of today: the values are kept and it's tried again."""
    freezer.move_to(f"{YESTERDAY} 23:50:00")
    responses["today"] = reported(YESTERDAY, 10.0, 12.0)
    await start(hass, config_entry, mock_api)

    side_effect = mock_api.async_get_consumption_data_for_range.side_effect
    mock_api.async_get_consumption_data_for_range.side_effect = ClientResponseError(
        MagicMock(), (), status=500, message="Internal Server Error"
    )
    await update_at(hass, config_entry, freezer, f"{TODAY} 00:05:00")
    assert figures(hass) == (10.0, 12.0)

    mock_api.async_get_consumption_data_for_range.side_effect = side_effect
    responses["range"] = reported(YESTERDAY, 11.0, 13.5)
    await update_at(hass, config_entry, freezer, f"{TODAY} 00:06:00")
    assert figures(hass) == (11.0, 13.5)


@pytest.mark.parametrize("today", [EMPTY, not_reported(TODAY)])
async def test_new_day_at_six_without_today(
    hass: HomeAssistant, config_entry, mock_api, responses, freezer, today
) -> None:
    """When Remeha hasn't reported today by 06:00, today starts at 0 anyway."""
    freezer.move_to(f"{YESTERDAY} 23:50:00")
    responses["today"] = reported(YESTERDAY, 10.0, 12.0)
    await start(hass, config_entry, mock_api)

    responses["today"] = today
    responses["range"] = reported(YESTERDAY, 11.0, 13.5)
    await update_at(hass, config_entry, freezer, f"{TODAY} 05:50:00")
    assert figures(hass) == (11.0, 13.5)

    await update_at(hass, config_entry, freezer, f"{TODAY} 06:05:00")
    assert figures(hass) == (0.0, 0.0)
    await update_at(hass, config_entry, freezer, f"{TODAY} 06:06:00")
    assert figures(hass) == (0.0, 0.0)
    assert mock_api.async_get_consumption_data_for_range.await_count == 1


async def test_new_day_at_six_while_yesterday_still_changes(
    hass: HomeAssistant, config_entry, mock_api, responses, freezer
) -> None:
    """At 06:00 yesterday is taken as complete, and the sensors go through 0."""
    freezer.move_to(f"{YESTERDAY} 23:50:00")
    responses["today"] = reported(YESTERDAY, 10.0, 12.0)
    await start(hass, config_entry, mock_api)

    responses["today"] = reported(TODAY, 3.0, 3.5)
    responses["range"] = reported(YESTERDAY, 11.0, 13.5)
    await update_at(hass, config_entry, freezer, f"{TODAY} 05:50:00")
    assert figures(hass) == (11.0, 13.5)

    responses["today"] = reported(TODAY, 3.2, 3.8)
    responses["range"] = reported(YESTERDAY, 11.2, 13.7)
    await update_at(hass, config_entry, freezer, f"{TODAY} 06:05:00")
    assert figures(hass) == (0.0, 0.0)

    await update_at(hass, config_entry, freezer, f"{TODAY} 06:06:00")
    assert figures(hass) == (3.2, 3.8)
    assert mock_api.async_get_consumption_data_for_range.await_count == 1


async def test_no_yesterday_after_a_start_at_night(
    hass: HomeAssistant, config_entry, mock_api, responses, freezer
) -> None:
    """After a start the sensors may already have shown today's figures before it.

    Showing yesterday's figures again would count yesterday twice.
    """
    freezer.move_to(f"{TODAY} 01:00:00")
    responses["range"] = reported(YESTERDAY, 11.0, 13.5)
    await start(hass, config_entry, mock_api)
    assert appliance_total(hass) == 0.0

    responses["today"] = reported(TODAY, 1.0, 1.2)
    await update_at(hass, config_entry, freezer, f"{TODAY} 01:15:00")
    assert figures(hass) == (1.0, 1.2)
    mock_api.async_get_consumption_data_for_range.assert_not_awaited()
