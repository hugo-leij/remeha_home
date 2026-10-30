"""The Remeha Home integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

from .api import RemehaHomeOAuth2Implementation, RemehaHomeAPI
from .config_flow import RemehaHomeLoginFlowHandler
from .const import DOMAIN
from .coordinator import RemehaHomeUpdateCoordinator
from .holiday import STORAGE_VERSION, HolidayModeRestorer, storage_key
from .producer_ids import ProducerIdResolver

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.CLIMATE,
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.WATER_HEATER,
]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up Remeha Home."""
    hass.data.setdefault(DOMAIN, {})

    RemehaHomeLoginFlowHandler.async_register_implementation(
        hass,
        RemehaHomeOAuth2Implementation(async_get_clientsession(hass)),
    )

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up Remeha Home from a config entry."""
    implementation = (
        await config_entry_oauth2_flow.async_get_config_entry_implementation(
            hass, entry
        )
    )

    oauth_session = config_entry_oauth2_flow.OAuth2Session(hass, entry, implementation)
    api = RemehaHomeAPI(oauth_session)
    coordinator = RemehaHomeUpdateCoordinator(hass, api, entry)
    producer_store = Store(hass, 1, f"{DOMAIN}_producer_ids_{entry.entry_id}")
    saved_producer_ids = await producer_store.async_load() or {}
    coordinator.producer_id_resolver = ProducerIdResolver(saved_producer_ids)

    def save_producer_ids() -> None:
        """Persist assigned producer IDs without affecting climate updates."""
        hass.async_create_task(
            producer_store.async_save(dict(coordinator.producer_id_resolver.assignments))
        )

    coordinator.on_producer_ids_changed = save_producer_ids
    holiday_restorer = HolidayModeRestorer(hass, entry, coordinator, api)
    await holiday_restorer.async_load()

    await coordinator.async_config_entry_first_refresh()
    entry.async_on_unload(coordinator.async_add_listener(holiday_restorer.async_update))
    holiday_restorer.async_update()

    hass.data[DOMAIN][entry.entry_id] = {
        "api": api,
        "coordinator": coordinator,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove the modes saved before a holiday."""
    await Store(hass, STORAGE_VERSION, storage_key(entry)).async_remove()
    await Store(hass, 1, f"{DOMAIN}_producer_ids_{entry.entry_id}").async_remove()
