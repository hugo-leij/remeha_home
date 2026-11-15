"""Test the heating curve entities."""

from __future__ import annotations

from datetime import timedelta
import logging
from unittest.mock import MagicMock

from aiohttp import ClientResponseError
import pytest
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .conftest import CLIMATE_ZONE_ID, entity_id, load_fixture, refresh, setup_integration

SLOPE = "heating_curve_slope"
BASE_SETPOINT = "heating_curve_base_setpoint"
MAX_FLOW = "heatingCurve.maxFlowTemperature"


def api_error(status: int) -> ClientResponseError:
    """Return an error response of the API."""
    return ClientResponseError(MagicMock(), (), status=status)


def number(hass: HomeAssistant, key: str):
    """Return the state of a heating curve number."""
    return hass.states.get(entity_id(hass, "number", CLIMATE_ZONE_ID, key))


async def set_value(hass: HomeAssistant, key: str, value: float) -> None:
    """Set a heating curve number."""
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": entity_id(hass, "number", CLIMATE_ZONE_ID, key), "value": value},
        blocking=True,
    )
    await hass.async_block_till_done()


async def test_entities(hass: HomeAssistant, config_entry, mock_api) -> None:
    """The heating curve gets numbers and a maximum flow temperature sensor."""
    await setup_integration(hass, config_entry, mock_api)

    slope = number(hass, SLOPE)
    assert slope.state == "0.0"
    assert slope.attributes["friendly_name"] == "Woonkamer Heating curve slope"
    assert number(hass, BASE_SETPOINT).state == "15.0"
    max_flow = hass.states.get(entity_id(hass, "sensor", CLIMATE_ZONE_ID, MAX_FLOW))
    assert max_flow.state == "35.0"
    assert max_flow.attributes["friendly_name"] == (
        "Woonkamer Heating curve maximum flow temperature"
    )


async def test_refreshed_every_15_minutes(
    hass: HomeAssistant, config_entry, mock_api, freezer
) -> None:
    """Changes made in the Remeha Home app show up within 15 minutes."""
    await setup_integration(hass, config_entry, mock_api)
    assert mock_api.async_get_heating_curve.call_count == 1

    changed = {**load_fixture("heating_curve.json"), "slope": 1.2}
    mock_api.async_get_heating_curve.return_value = changed

    freezer.tick(timedelta(minutes=5))
    await refresh(hass, config_entry)
    assert mock_api.async_get_heating_curve.call_count == 1
    assert number(hass, SLOPE).state == "0.0"

    freezer.tick(timedelta(minutes=10))
    await refresh(hass, config_entry)
    assert mock_api.async_get_heating_curve.call_count == 2
    assert number(hass, SLOPE).state == "1.2"


async def test_zone_without_heating_curve(
    hass: HomeAssistant, config_entry, mock_api, freezer, caplog
) -> None:
    """A zone without a heating curve is asked once, without a warning."""
    mock_api.async_get_heating_curve.side_effect = api_error(404)
    await setup_integration(hass, config_entry, mock_api)

    for _ in range(3):
        freezer.tick(timedelta(minutes=15))
        await refresh(hass, config_entry)

    assert mock_api.async_get_heating_curve.call_count == 1
    assert entity_id(hass, "number", CLIMATE_ZONE_ID, SLOPE) is None
    assert entity_id(hass, "sensor", CLIMATE_ZONE_ID, MAX_FLOW) is None
    assert not [
        r
        for r in caplog.records
        if r.name.startswith("custom_components.remeha_home")
        and r.levelno >= logging.WARNING
    ]


async def test_server_error_is_retried(
    hass: HomeAssistant, config_entry, mock_api, freezer
) -> None:
    """A server error is not taken as a zone without a heating curve."""
    await setup_integration(hass, config_entry, mock_api)

    mock_api.async_get_heating_curve.side_effect = api_error(500)
    freezer.tick(timedelta(minutes=15))
    await refresh(hass, config_entry)
    assert mock_api.async_get_heating_curve.call_count == 2
    # The last known heating curve stays available
    assert number(hass, SLOPE).state == "0.0"

    mock_api.async_get_heating_curve.side_effect = None
    freezer.tick(timedelta(minutes=15))
    await refresh(hass, config_entry)
    assert mock_api.async_get_heating_curve.call_count == 3


@pytest.mark.parametrize(
    ("key", "value", "expected"),
    [
        (SLOPE, 1.2000000000000002, (1.2, 15.0)),
        (BASE_SETPOINT, 20.499999999999996, (0.0, 20.5)),
    ],
)
async def test_set_value(
    hass: HomeAssistant, config_entry, mock_api, key, value, expected
) -> None:
    """A new value is rounded, sent with the other value, and shown at once."""
    await setup_integration(hass, config_entry, mock_api)

    await set_value(hass, key, value)

    mock_api.async_set_heating_curve.assert_called_once_with(CLIMATE_ZONE_ID, *expected)
    assert number(hass, SLOPE).state == str(expected[0])
    assert number(hass, BASE_SETPOINT).state == str(expected[1])


async def test_written_curve_is_kept(
    hass: HomeAssistant, config_entry, mock_api, freezer
) -> None:
    """A change is built from what was written before, not from a read that lags behind.

    The read right after a write can answer with the values from before it; a change
    built from that read sends the old value back for the parameter it did not change.
    """
    await setup_integration(hass, config_entry, mock_api)
    # Every later read answers with the heating curve from before any change
    mock_api.async_get_heating_curve.side_effect = lambda _zone: load_fixture(
        "heating_curve.json"
    )
    reads = mock_api.async_get_heating_curve.await_count

    await set_value(hass, SLOPE, 1.2)
    await refresh(hass, config_entry)
    assert mock_api.async_get_heating_curve.await_count == reads
    assert number(hass, SLOPE).state == "1.2"

    freezer.tick(timedelta(minutes=5))
    await set_value(hass, BASE_SETPOINT, 20.5)
    await refresh(hass, config_entry)
    assert mock_api.async_set_heating_curve.await_args_list[-1].args == (
        CLIMATE_ZONE_ID,
        1.2,
        20.5,
    )
    assert mock_api.async_get_heating_curve.await_count == reads
    assert number(hass, SLOPE).state == "1.2"
    assert number(hass, BASE_SETPOINT).state == "20.5"

    # 15 minutes after the last change the heating curve is read again
    mock_api.async_get_heating_curve.side_effect = lambda _zone: {
        **load_fixture("heating_curve.json"),
        "slope": 1.2,
        "baseSetpoint": 20.5,
        "maxFlowTemperature": 30.0,
    }
    freezer.tick(timedelta(minutes=14))
    await refresh(hass, config_entry)
    assert mock_api.async_get_heating_curve.await_count == reads

    freezer.tick(timedelta(minutes=1))
    await refresh(hass, config_entry)
    assert mock_api.async_get_heating_curve.await_count == reads + 1
    assert number(hass, SLOPE).state == "1.2"
    assert number(hass, BASE_SETPOINT).state == "20.5"
    max_flow = hass.states.get(entity_id(hass, "sensor", CLIMATE_ZONE_ID, MAX_FLOW))
    assert max_flow.state == "30.0"


@pytest.mark.parametrize(
    ("missing", "unavailable", "available"),
    [
        ("maximumSlope", SLOPE, BASE_SETPOINT),
        ("minimumSlope", SLOPE, BASE_SETPOINT),
        ("incrementSlope", SLOPE, BASE_SETPOINT),
        ("maximumBaseSetpoint", BASE_SETPOINT, SLOPE),
    ],
)
@pytest.mark.parametrize("how", ["absent", "null"])
async def test_missing_limit(
    hass: HomeAssistant, config_entry, mock_api, missing, unavailable, available, how
) -> None:
    """Without its limits a number is unavailable instead of guessing them."""
    curve = load_fixture("heating_curve.json")
    if how == "absent":
        del curve[missing]
    else:
        curve[missing] = None
    mock_api.async_get_heating_curve.return_value = curve
    await setup_integration(hass, config_entry, mock_api)

    assert number(hass, unavailable).state == STATE_UNAVAILABLE
    assert number(hass, available).state != STATE_UNAVAILABLE


@pytest.mark.parametrize("missing", ["slope", "baseSetpoint"])
async def test_missing_value(
    hass: HomeAssistant, config_entry, mock_api, missing
) -> None:
    """A change sends both values, so both numbers need both values."""
    curve = load_fixture("heating_curve.json")
    del curve[missing]
    mock_api.async_get_heating_curve.return_value = curve
    await setup_integration(hass, config_entry, mock_api)

    for key in (SLOPE, BASE_SETPOINT):
        state = number(hass, key)
        assert state.state == STATE_UNAVAILABLE

        await hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": state.entity_id, "value": 1.0},
            blocking=True,
        )

    mock_api.async_set_heating_curve.assert_not_called()


async def test_limits_disappear_before_the_change(
    hass: HomeAssistant, config_entry, mock_api
) -> None:
    """A change is refused when the limits are gone by the time it runs."""
    await setup_integration(hass, config_entry, mock_api)
    slope = hass.data["entity_components"]["number"].get_entity(
        entity_id(hass, "number", CLIMATE_ZONE_ID, SLOPE)
    )
    del slope._heating_curve_data["maximumSlope"]

    with pytest.raises(HomeAssistantError) as error:
        await slope.async_set_native_value(1.0)

    assert error.value.translation_key == "heating_curve_unavailable"
    mock_api.async_set_heating_curve.assert_not_called()
