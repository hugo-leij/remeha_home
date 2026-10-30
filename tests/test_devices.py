"""Test the devices that the integration registers."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from custom_components.remeha_home.const import DOMAIN
from custom_components.remeha_home.producer_ids import ProducerIdResolver

from .conftest import (
    APPLIANCE_ID,
    CLIMATE_ZONE_ID,
    HOT_WATER_ZONE_ID,
    entity_id,
    setup_integration,
)


async def test_devices_linked_to_appliance(
    hass: HomeAssistant, config_entry, mock_api, caplog
) -> None:
    """Link the zones and the producers to the appliance by its device id."""
    await setup_integration(hass, config_entry, mock_api)

    registry = dr.async_get(hass)
    appliance = registry.async_get_device_by_identifier(
        (DOMAIN, APPLIANCE_ID), config_entry.entry_id
    )
    assert appliance is not None
    assert appliance.via_device_id is None

    children = [
        device
        for device in dr.async_entries_for_config_entry(registry, config_entry.entry_id)
        if device.id != appliance.id
    ]
    child_ids = {item_id for device in children for _, item_id in device.identifiers}
    assert {CLIMATE_ZONE_ID, HOT_WATER_ZONE_ID} <= child_ids
    # The climate zone, the hot water zone, the heat pump and the gas boiler
    assert len(children) == 4
    assert all(device.via_device_id == appliance.id for device in children)

    # Home Assistant 2027.8 no longer accepts the deprecated via_device
    assert "deprecated `via_device`" not in caplog.text


async def test_hybrid_producers_with_same_instance_keep_separate_devices_and_energy(
    hass: HomeAssistant, config_entry, mock_api, appliance
) -> None:
    """Elga Ace can report instance 1 for both its heat pump and gas boiler."""
    consumption = mock_api.async_get_consumption_data_for_today.return_value["data"][0]
    heat_pump, boiler = consumption["producerPerformanceStatistics"]["producers"]
    boiler["instanceWithinDevice"] = heat_pump["instanceWithinDevice"]

    await setup_integration(hass, config_entry, mock_api)

    resolver = hass.data[DOMAIN][config_entry.entry_id]["coordinator"].producer_id_resolver
    heat_pump_id = resolver.get_id(APPLIANCE_ID, heat_pump)
    boiler_id = resolver.get_id(APPLIANCE_ID, boiler)
    assert heat_pump_id != boiler_id

    coordinator = hass.data[DOMAIN][config_entry.entry_id]["coordinator"]
    assert coordinator.get_by_id(heat_pump_id)["producerType"] == "HeatPumpAirSource"
    assert coordinator.get_by_id(boiler_id)["producerType"] == "GasBoiler"

    registry = dr.async_get(hass)
    pump_device = registry.async_get_device_by_identifier(
        (DOMAIN, heat_pump_id), config_entry.entry_id
    )
    boiler_device = registry.async_get_device_by_identifier(
        (DOMAIN, boiler_id), config_entry.entry_id
    )
    assert pump_device is not None and boiler_device is not None
    assert pump_device.id != boiler_device.id

    pump_entity = entity_id(hass, "sensor", heat_pump_id, "energyConsumptionCH")
    boiler_entity = entity_id(hass, "sensor", boiler_id, "energyConsumptionCH")
    assert pump_entity is not None and boiler_entity is not None
    assert float(hass.states.get(pump_entity).state) == 4.0
    assert float(hass.states.get(boiler_entity).state) == round(
        1.0 / appliance["gasCalorificValue"], 2
    )
    assert hass.states.get(pump_entity).attributes["unit_of_measurement"] == "kWh"
    assert hass.states.get(boiler_entity).attributes["unit_of_measurement"] == "m³"


def test_producer_ids_remain_unchanged_without_collisions() -> None:
    """Stand-alone, conventional hybrid and multi-producer IDs remain stable."""
    heat_pump = {
        "producerType": "HeatPumpAirSource",
        "energyType": "Electric",
        "instanceWithinDevice": 1,
    }
    boiler = {
        "producerType": "GasBoiler",
        "energyType": "NaturalGas",
        "instanceWithinDevice": 2,
    }
    resolver = ProducerIdResolver()
    assert resolver.resolve(APPLIANCE_ID, [heat_pump])[0][0][1] == f"{APPLIANCE_ID}_1"
    assert resolver.resolve(APPLIANCE_ID, [boiler])[0][0][1] == f"{APPLIANCE_ID}_2"
    assert resolver.resolve(APPLIANCE_ID, [heat_pump, boiler])[0][0][1] == f"{APPLIANCE_ID}_1"
    assert resolver.get_id(APPLIANCE_ID, boiler) == f"{APPLIANCE_ID}_2"


def test_colliding_ids_do_not_depend_on_api_producer_order() -> None:
    """An Elga hybrid with duplicated instance indexes is deterministic."""
    heat_pump = {
        "producerType": "HeatPumpAirSource",
        "energyType": "Electric",
        "instanceWithinDevice": 1,
    }
    boiler = {
        "producerType": "GasBoiler",
        "energyType": "NaturalGas",
        "instanceWithinDevice": 1,
    }
    for producers in ([heat_pump, boiler], [boiler, heat_pump]):
        resolver = ProducerIdResolver()
        resolver.resolve(APPLIANCE_ID, producers)
        assert resolver.get_id(APPLIANCE_ID, heat_pump) == f"{APPLIANCE_ID}_1"
        assert resolver.get_id(APPLIANCE_ID, boiler) == f"{APPLIANCE_ID}_1_GasBoiler"


def test_persistent_producer_ids_survive_missing_producers_and_restart() -> None:
    """Once assigned, producer IDs do not depend on later incomplete API data."""
    heat_pump = {
        "producerType": "HeatPumpAirSource",
        "energyType": "Electric",
        "instanceWithinDevice": 1,
    }
    boiler = {
        "producerType": "GasBoiler",
        "energyType": "NaturalGas",
        "instanceWithinDevice": 1,
    }
    resolver = ProducerIdResolver()
    first, changed = resolver.resolve(APPLIANCE_ID, [heat_pump, boiler])
    assert changed
    assert {item["producerType"]: uid for item, uid in first} == {
        "HeatPumpAirSource": f"{APPLIANCE_ID}_1",
        "GasBoiler": f"{APPLIANCE_ID}_1_GasBoiler",
    }
    previous = resolver.get_id(APPLIANCE_ID, boiler)
    only_boiler, changed = resolver.resolve(APPLIANCE_ID, [boiler])
    assert not changed
    assert only_boiler[0][1] == previous

    restored = ProducerIdResolver(resolver.assignments)
    assert restored.resolve(APPLIANCE_ID, [boiler])[0][0][1] == previous
    restored.resolve(APPLIANCE_ID, [boiler, heat_pump])
    assert restored.get_id(APPLIANCE_ID, boiler) == f"{APPLIANCE_ID}_1_GasBoiler"
    assert restored.get_id(APPLIANCE_ID, heat_pump) == f"{APPLIANCE_ID}_1"


def test_standalone_and_incomplete_initial_discovery_preserve_ids() -> None:
    """A standalone device retains its legacy ID; later producers get new IDs."""
    heat_pump = {
        "producerType": "HeatPumpAirSource",
        "energyType": "Electric",
        "instanceWithinDevice": 1,
    }
    boiler = {
        "producerType": "GasBoiler",
        "energyType": "NaturalGas",
        "instanceWithinDevice": 1,
    }
    resolver = ProducerIdResolver()
    assert resolver.resolve(APPLIANCE_ID, [boiler])[0][0][1] == f"{APPLIANCE_ID}_1"
    resolver.resolve(APPLIANCE_ID, [heat_pump, boiler])
    assert resolver.get_id(APPLIANCE_ID, boiler) == f"{APPLIANCE_ID}_1"
    assert resolver.get_id(APPLIANCE_ID, heat_pump) == f"{APPLIANCE_ID}_1_HeatPumpAirSource"


def test_ambiguous_same_type_duplicate_producers_are_not_merged() -> None:
    """Skip indistinguishable API producers rather than mix their meter values."""
    heat_pump = {
        "producerType": "HeatPumpAirSource",
        "energyType": "Electric",
        "instanceWithinDevice": 1,
    }
    resolver = ProducerIdResolver()
    allocated, changed = resolver.resolve(APPLIANCE_ID, [heat_pump, dict(heat_pump)])
    assert allocated == []
    assert not changed


def test_hybrid_first_reports_only_boiler() -> None:
    """On a heat-pump appliance, the boiler must not take the heat pump ID."""
    boiler = {
        "producerType": "GasBoiler",
        "energyType": "NaturalGas",
        "instanceWithinDevice": 1,
    }
    pump = {
        "producerType": "HeatPumpAirSource",
        "energyType": "Electric",
        "instanceWithinDevice": 1,
    }
    resolver = ProducerIdResolver()
    assert resolver.resolve(
        APPLIANCE_ID, [boiler], appliance_type="HeatPump"
    )[0][0][1] == f"{APPLIANCE_ID}_1_GasBoiler"
    restored = ProducerIdResolver(resolver.assignments)
    restored.resolve(APPLIANCE_ID, [pump, boiler], appliance_type="HeatPump")
    assert restored.get_id(APPLIANCE_ID, boiler) == f"{APPLIANCE_ID}_1_GasBoiler"
    assert restored.get_id(APPLIANCE_ID, pump) == f"{APPLIANCE_ID}_1"
