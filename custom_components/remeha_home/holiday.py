"""Restore the modes of the zones after a holiday.

While a holiday is active, the appliance sets its climate zones to frost protection
and its hot water to off. When the holiday is over, it doesn't always restore them
right away: on a Remeha Elga Ace, a zone that followed its clock program sometimes
stayed in frost protection for minutes, and on a BAXI heat pump the zones and the hot
water were reported to stay changed until their modes were set again.

So the modes are saved when a holiday starts, and set again when it is over: after
a cancellation in Home Assistant or in the app, and when the holiday ends by itself.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timedelta
from functools import partial
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store
import homeassistant.util.dt as dt_util

from .api import RemehaHomeAPI
from .const import DOMAIN
from .coordinator import RemehaHomeUpdateCoordinator
from .util import remeha_time

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
# The API applies a mode change a few seconds after the request
REFRESH_DELAY = 15
# Requests per appliance, after which the restore is given up
RESTORE_ATTEMPTS = 3

# Climate zone modes that follow the clock program
SCHEDULE_ZONE_MODES = ("Scheduling", "TemporaryOverride")


def storage_key(entry: ConfigEntry) -> str:
    """Return the key of the stored modes of a config entry."""
    return f"{DOMAIN}.{entry.entry_id}.holiday"


def holiday_in_effect(appliance: dict) -> bool:
    """Return whether a holiday is active, or should be by its dates.

    The dashboard shows the dates of a new holiday a few seconds before the
    appliance reports it as active, and the zones can already be in frost
    protection by then.
    """
    schedule = appliance.get("holidaySchedule") or {}
    if schedule.get("active"):
        return True
    start = remeha_time(schedule.get("startTime"))
    end = remeha_time(schedule.get("endTime"))
    return start is not None and end is not None and start <= dt_util.now() < end


def zone_modes(appliance: dict) -> dict[str, dict[str, dict[str, Any]]]:
    """Return the modes of the zones of an appliance."""
    return {
        "climateZones": {
            zone["climateZoneId"]: {
                "zoneMode": zone.get("zoneMode"),
                "setPoint": zone.get("setPoint"),
                "program": zone.get("activeHeatingClimateTimeProgramNumber"),
            }
            for zone in appliance.get("climateZones", [])
        },
        "hotWaterZones": {
            zone["hotWaterZoneId"]: {"dhwZoneMode": zone.get("dhwZoneMode")}
            for zone in appliance.get("hotWaterZones", [])
        },
    }


class HolidayModeRestorer:
    """Save the modes of the zones before a holiday, and restore them after it."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        coordinator: RemehaHomeUpdateCoordinator,
        api: RemehaHomeAPI,
    ) -> None:
        """Create the restorer for the appliances of a config entry."""
        self.hass = hass
        self.entry = entry
        self.coordinator = coordinator
        self.api = api
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, storage_key(entry)
        )
        # The modes before the holiday, kept until they are restored. They are
        # stored, so a restart during the holiday doesn't lose them.
        self._saved: dict[str, dict] = {}
        # The modes at the last update outside a holiday
        self._last_modes: dict[str, dict] = {}
        self._attempts: dict[str, int] = {}
        self._next_attempt: dict[str, datetime] = {}
        self._zone_locks: dict[str, asyncio.Lock] = {}

    async def async_load(self) -> None:
        """Load the modes saved before a holiday."""
        data = await self._store.async_load() or {}
        self._saved = data.get("appliances", {})

    @asynccontextmanager
    async def async_zone_command(
        self, zone_id: str, preserve_during_holiday: bool
    ) -> AsyncIterator[None]:
        """Record a successful mode command while the restore holds off on this zone.

        During the holiday, returning to its mode makes the original mode eligible
        again. After the holiday, an explicit mode wins while recovery is pending.
        The saved decision stays intact if the API rejects the command.
        """
        async with self._zone_locks.setdefault(zone_id, asyncio.Lock()):
            appliance = next(
                (
                    appliance
                    for appliance in self.coordinator.data["appliances"]
                    if (saved := self._saved.get(appliance["applianceId"])) is not None
                    and (zone_id in saved["climateZones"] or zone_id in saved["hotWaterZones"])
                ),
                None,
            )
            preserve = preserve_during_holiday or (
                appliance is not None and not holiday_in_effect(appliance)
            )
            yield
            if appliance is None:
                return
            opt_out = self._saved[appliance["applianceId"]].setdefault("excludedZones", [])
            if preserve == (zone_id in opt_out):
                return
            if preserve:
                opt_out.append(zone_id)
            else:
                opt_out.remove(zone_id)
            # Persist the per-zone decision before a queued restore can continue.
            await self._store.async_save({"appliances": deepcopy(self._saved)})

    @callback
    def async_update(self) -> None:
        """Check the holidays after an update of the coordinator."""
        if not self.coordinator.last_update_success:
            return
        for appliance in self.coordinator.data["appliances"]:
            appliance_id = appliance["applianceId"]
            if holiday_in_effect(appliance):
                if appliance_id not in self._saved and appliance_id in self._last_modes:
                    self._saved[appliance_id] = deepcopy(self._last_modes[appliance_id])
                    self._async_save()
            elif appliance_id in self._saved:
                self._async_restore(appliance)
            else:
                self._last_modes[appliance_id] = zone_modes(appliance)

    @callback
    def _async_restore(self, appliance: dict) -> None:
        """Set the modes of the zones that the holiday left changed."""
        appliance_id = appliance["applianceId"]
        saved = self._saved[appliance_id]
        # A poll can see an optimistic mode while a command is still pending.
        # Keep the saved modes and retry after the command releases its lock.
        if any(
            lock.locked() and (zone_id in saved["climateZones"] or zone_id in saved["hotWaterZones"])
            for zone_id, lock in self._zone_locks.items()
        ):
            return
        next_attempt = self._next_attempt.get(appliance_id)
        if next_attempt is not None and dt_util.utcnow() < next_attempt:
            # The API hasn't applied the last requests yet
            return

        attempt = self._attempts.get(appliance_id, 0) + 1
        requests = self._restore_requests(
            appliance, saved, first=attempt == 1
        )
        if not requests or attempt > RESTORE_ATTEMPTS:
            if requests:
                _LOGGER.warning(
                    "Could not restore the modes of the zones of appliance %s after the holiday",
                    appliance_id,
                )
            self._async_done(appliance_id)
            return

        _LOGGER.debug(
            "Restoring the modes of the zones of appliance %s after the holiday",
            appliance_id,
        )
        self._attempts[appliance_id] = attempt
        self._next_attempt[appliance_id] = dt_util.utcnow() + timedelta(
            seconds=REFRESH_DELAY
        )
        self.entry.async_create_background_task(
            self.hass,
            self._async_send(appliance_id, requests),
            f"{DOMAIN} restore the modes after a holiday",
        )

    def _restore_requests(
        self, appliance: dict, saved: dict, first: bool
    ) -> list[tuple[str, Callable[[], Awaitable[None]]]]:
        """Return the requests that set the saved modes again."""
        requests: list[tuple[str, Callable[[], Awaitable[None]]]] = []
        excluded = set(saved.get("excludedZones", ()))
        for zone in appliance.get("climateZones", []):
            zone_id = zone["climateZoneId"]
            before = saved["climateZones"].get(zone_id)
            if before is None or zone_id in excluded:
                continue
            mode = zone.get("zoneMode")
            if before["zoneMode"] in SCHEDULE_ZONE_MODES:
                # The zone can also read Scheduling again, but keep the frost
                # protection setpoint until the schedule mode is set again
                if mode == "FrostProtection" or (first and mode == "Scheduling"):
                    program = before["program"] or zone.get(
                        "activeHeatingClimateTimeProgramNumber"
                    )
                    requests.append(
                        (
                            zone_id,
                            partial(self.api.async_set_schedule, zone_id, program),
                        )
                    )
            elif before["zoneMode"] == "Manual" and mode == "FrostProtection":
                requests.append(
                    (
                        zone_id,
                        partial(self.api.async_set_manual, zone_id, before["setPoint"]),
                    )
                )

        for zone in appliance.get("hotWaterZones", []):
            zone_id = zone["hotWaterZoneId"]
            before = saved["hotWaterZones"].get(zone_id)
            if before is None or zone_id in excluded or zone.get("dhwZoneMode") != "Off":
                continue
            if before["dhwZoneMode"] in ("Scheduling", "Boost"):
                requests.append(
                    (zone_id, partial(self.api.async_set_dhw_mode_schedule, zone_id))
                )
            elif before["dhwZoneMode"] == "ContinuousComfort":
                requests.append(
                    (zone_id, partial(self.api.async_set_dhw_mode_comfort, zone_id))
                )
        return requests

    async def _async_send(
        self,
        appliance_id: str,
        requests: list[tuple[str, Callable[[], Awaitable[None]]]],
    ) -> None:
        """Send the requests, and check the result once the API has applied them."""
        for zone_id, request in requests:
            async with self._zone_locks.setdefault(zone_id, asyncio.Lock()):
                # A user action may have excluded the zone since this batch was queued.
                saved = self._saved.get(appliance_id, {})
                if not saved or zone_id in saved.get("excludedZones", ()):
                    continue
                try:
                    await request()
                except Exception as err:  # noqa: BLE001
                    # The next attempt sends it again.
                    _LOGGER.warning(
                        "Failed to restore a zone mode after the holiday: %s", err
                    )

        async def _refresh(_now: datetime) -> None:
            await self.coordinator.async_request_refresh()

        self.entry.async_on_unload(
            async_call_later(self.hass, REFRESH_DELAY, _refresh)
        )

    @callback
    def _async_done(self, appliance_id: str) -> None:
        """Forget the saved modes of an appliance."""
        self._saved.pop(appliance_id, None)
        self._attempts.pop(appliance_id, None)
        self._next_attempt.pop(appliance_id, None)
        self._async_save()

    @callback
    def _async_save(self) -> None:
        """Store the saved modes."""
        self._store.async_delay_save(lambda: {"appliances": self._saved}, 1)
