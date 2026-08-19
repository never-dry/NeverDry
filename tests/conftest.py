"""Shared test fixtures for NeverDry tests.

Since we test the core logic without a full Home Assistant runtime,
we mock the HA dependencies and expose the sensor classes directly.
"""

import asyncio
import sys
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock

import pytest


# ── Stub out homeassistant imports before loading our code ────────
def _create_ha_stubs():
    """Create minimal stubs for homeassistant modules."""

    # homeassistant.components.sensor
    def _async_on_remove(self, func):
        """Record a cleanup callback, as ``Entity.async_on_remove`` does.

        Modelled rather than no-op'd on purpose: a listener registered without
        it is never released when the entity is removed, so an integration
        reload leaves the dead entity still subscribed and still writing state.
        The stub has to carry that contract or the tests cannot see the leak.
        """
        if getattr(self, "_on_remove", None) is None:
            self._on_remove = []
        self._on_remove.append(func)

    def _run_on_remove_callbacks(self):
        """Run the recorded callbacks — what HA does inside ``Entity.async_remove``."""
        for func in reversed(getattr(self, "_on_remove", None) or []):
            func()
        self._on_remove = []

    sensor_mod = ModuleType("homeassistant.components.sensor")
    sensor_mod.SensorEntity = type(
        "SensorEntity",
        (),
        {
            "async_write_ha_state": lambda self: None,
            "async_on_remove": _async_on_remove,
            "run_on_remove_callbacks": _run_on_remove_callbacks,
        },
    )

    class SensorStateClass:
        MEASUREMENT = "measurement"
        TOTAL_INCREASING = "total_increasing"

    class SensorDeviceClass:
        AREA = "area"
        DURATION = "duration"
        # Enum sensors carry an identifier as their state and let the frontend
        # translate it. Present in Home Assistant since 2022.10; the stub has to
        # mirror it or the entity that reports the active model cannot load.
        ENUM = "enum"
        PRECIPITATION = "precipitation"
        PRECIPITATION_INTENSITY = "precipitation_intensity"
        TIMESTAMP = "timestamp"
        VOLUME_FLOW_RATE = "volume_flow_rate"
        VOLUME_STORAGE = "volume_storage"
        WATER = "water"

    sensor_mod.SensorStateClass = SensorStateClass
    sensor_mod.SensorDeviceClass = SensorDeviceClass

    # homeassistant.core
    core_mod = ModuleType("homeassistant.core")
    core_mod.HomeAssistant = MagicMock
    core_mod.ServiceCall = MagicMock
    core_mod.callback = lambda fn: fn

    # homeassistant.helpers.event
    event_mod = ModuleType("homeassistant.helpers.event")
    event_mod.async_track_state_change_event = MagicMock()
    event_mod.async_track_time_change = MagicMock()
    event_mod.async_track_time_interval = MagicMock()

    # homeassistant.helpers.restore_state
    restore_mod = ModuleType("homeassistant.helpers.restore_state")
    restore_mod.RestoreEntity = type(
        "RestoreEntity",
        (),
        {
            "async_get_last_state": lambda self: None,
        },
    )

    # homeassistant.helpers.typing
    typing_mod = ModuleType("homeassistant.helpers.typing")
    typing_mod.ConfigType = dict

    # homeassistant.config_entries
    config_entries_mod = ModuleType("homeassistant.config_entries")
    config_entries_mod.ConfigEntry = MagicMock

    class _ConfigFlow:
        """Subclassable stub accepting the ``domain=`` class kwarg."""

        def __init_subclass__(cls, **kwargs):
            super().__init_subclass__()

    class _OptionsFlow:
        pass

    config_entries_mod.ConfigFlow = _ConfigFlow
    config_entries_mod.OptionsFlow = _OptionsFlow
    config_entries_mod.ConfigFlowResult = dict

    # homeassistant.helpers.entity_platform
    entity_platform_mod = ModuleType("homeassistant.helpers.entity_platform")
    entity_platform_mod.AddEntitiesCallback = MagicMock

    # homeassistant.helpers.config_validation
    cv_mod = ModuleType("homeassistant.helpers.config_validation")
    cv_mod.config_entry_only_config_schema = lambda domain: {}

    # homeassistant.helpers.selector (only attribute access at call time)
    selector_mod = ModuleType("homeassistant.helpers.selector")

    # homeassistant.components.button
    button_mod = ModuleType("homeassistant.components.button")
    button_mod.ButtonEntity = type(
        "ButtonEntity",
        (),
        {
            "async_press": lambda self: None,
        },
    )

    # homeassistant.components.repairs — the guided fix flows. Stubbed as a bare
    # base class: what the integration puts in it is ours and testable, what
    # Home Assistant does with it is not.
    repairs_mod = ModuleType("homeassistant.components.repairs")
    repairs_mod.RepairsFlow = type("RepairsFlow", (), {})

    # homeassistant.components.recorder
    recorder_mod = ModuleType("homeassistant.components.recorder")
    recorder_mod.get_instance = MagicMock(return_value=MagicMock())

    # homeassistant.components.recorder.history
    recorder_history_mod = ModuleType("homeassistant.components.recorder.history")
    recorder_history_mod.get_significant_states = MagicMock(return_value={})

    # homeassistant.helpers.device_registry
    device_registry_mod = ModuleType("homeassistant.helpers.device_registry")

    class DeviceInfo(dict):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.__dict__.update(kwargs)

    device_registry_mod.DeviceInfo = DeviceInfo

    # homeassistant.helpers.entity_registry
    entity_registry_mod = ModuleType("homeassistant.helpers.entity_registry")
    entity_registry_mod.async_get = MagicMock(return_value=MagicMock())
    entity_registry_mod.async_entries_for_device = MagicMock(return_value=[])
    entity_registry_mod.async_entries_for_config_entry = MagicMock(return_value=[])
    entity_registry_mod.async_migrate_entries = AsyncMock()
    entity_registry_mod.RegistryEntry = MagicMock

    # homeassistant.helpers.storage
    storage_mod = ModuleType("homeassistant.helpers.storage")

    class _StubStore:
        def __init__(self, hass, version, key):
            pass

        async def async_load(self):
            return None

        async def async_save(self, data):
            pass

    storage_mod.Store = _StubStore

    # homeassistant.const
    const_mod = ModuleType("homeassistant.const")

    class EntityCategory:
        DIAGNOSTIC = "diagnostic"
        CONFIG = "config"

    class UnitOfArea:
        SQUARE_METERS = "m²"

    class UnitOfLength:
        MILLIMETERS = "mm"
        INCHES = "in"

    class UnitOfTime:
        SECONDS = "s"

    class UnitOfVolume:
        LITERS = "L"
        GALLONS = "gal"

    class UnitOfVolumeFlowRate:
        LITERS_PER_MINUTE = "L/min"
        LITERS_PER_HOUR = "L/h"
        GALLONS_PER_HOUR = "gal/h"

    class UnitOfVolumetricFlux:
        MILLIMETERS_PER_HOUR = "mm/h"
        INCHES_PER_HOUR = "in/h"

    const_mod.EntityCategory = EntityCategory
    const_mod.UnitOfArea = UnitOfArea
    const_mod.UnitOfLength = UnitOfLength
    const_mod.UnitOfTime = UnitOfTime
    const_mod.UnitOfVolume = UnitOfVolume
    const_mod.UnitOfVolumeFlowRate = UnitOfVolumeFlowRate
    const_mod.UnitOfVolumetricFlux = UnitOfVolumetricFlux

    # Register all stubs
    helpers_mod = ModuleType("homeassistant.helpers")
    helpers_mod.config_validation = cv_mod
    helpers_mod.device_registry = device_registry_mod
    helpers_mod.entity_registry = entity_registry_mod
    helpers_mod.selector = selector_mod
    # homeassistant.data_entry_flow — only `section` is used, to group the
    # zone form into collapsible blocks. The stub returns the inner schema
    # unchanged: grouping is presentation, and the flow flattens the nesting
    # back out before anything reads a value.
    data_entry_flow_mod = ModuleType("homeassistant.data_entry_flow")
    data_entry_flow_mod.section = lambda schema, options=None: schema

    mods = {
        "voluptuous": ModuleType("voluptuous"),
        "homeassistant": ModuleType("homeassistant"),
        "homeassistant.data_entry_flow": data_entry_flow_mod,
        "homeassistant.components": ModuleType("homeassistant.components"),
        "homeassistant.components.button": button_mod,
        "homeassistant.components.repairs": repairs_mod,
        "homeassistant.components.sensor": sensor_mod,
        "homeassistant.config_entries": config_entries_mod,
        "homeassistant.const": const_mod,
        "homeassistant.core": core_mod,
        "homeassistant.helpers": helpers_mod,
        "homeassistant.helpers.config_validation": cv_mod,
        "homeassistant.helpers.selector": selector_mod,
        "homeassistant.helpers.entity_platform": entity_platform_mod,
        "homeassistant.helpers.entity_registry": entity_registry_mod,
        "homeassistant.helpers.event": event_mod,
        "homeassistant.helpers.restore_state": restore_mod,
        "homeassistant.helpers.device_registry": device_registry_mod,
        "homeassistant.helpers.storage": storage_mod,
        "homeassistant.helpers.typing": typing_mod,
        "homeassistant.components.recorder": recorder_mod,
        "homeassistant.components.recorder.history": recorder_history_mod,
    }
    sys.modules.update(mods)


_create_ha_stubs()

# Now we can safely import our code
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "custom_components"))
from never_dry.const import (  # noqa: E402
    CONF_ALPHA,
    CONF_RAIN_SENSOR,
    CONF_T_BASE,
    CONF_TEMP_SENSOR,
    CONF_ZONE_AREA,
    CONF_ZONE_EFFICIENCY,
    CONF_ZONE_FLOW_RATE,
    CONF_ZONE_NAME,
    CONF_ZONE_SYSTEM_TYPE,
    CONF_ZONE_THRESHOLD,
    CONF_ZONE_VALVE,
    SYSTEM_TYPE_CUSTOM,
)
from never_dry.controller import IrrigationController  # noqa: E402
from never_dry.sensor import (  # noqa: E402
    DrynessIndexSensor,
    ETSensor,
    IrrigationZoneSensor,
)


def _make_state(value, unit=None):
    """Create a mock HA state object."""
    state = MagicMock()
    state.state = str(value)
    state.attributes = {"unit_of_measurement": unit} if unit else {}
    return state


@pytest.fixture
def base_config():
    """Minimal valid configuration (no zones)."""
    return {
        CONF_TEMP_SENSOR: "sensor.temperature",
        CONF_RAIN_SENSOR: "sensor.rain",
        CONF_ALPHA: 0.22,
        CONF_T_BASE: 9.0,
    }


@pytest.fixture
def hass_mock():
    """Mock HomeAssistant instance with async services.

    Provides a real event loop for the fixture lifetime so that both sync
    and async test paths can create tasks without hitting the Python 3.12
    deprecation of implicit event-loop creation.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    hass = MagicMock()
    hass.states = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.services.async_register = MagicMock()
    hass.bus = MagicMock()
    hass.bus.async_fire = MagicMock()

    def _create_task(coro):
        try:
            running = asyncio.get_running_loop()
            return running.create_task(coro)
        except RuntimeError:
            return loop.create_task(coro)

    hass.async_create_task = _create_task
    # Mock HA config with latitude (northern hemisphere)
    hass.config = MagicMock()
    hass.config.latitude = 45.0

    yield hass

    loop.close()
    asyncio.set_event_loop(None)


@pytest.fixture
def et_sensor(hass_mock, base_config):
    """Create an ETSensor instance."""
    return ETSensor(hass_mock, base_config)


@pytest.fixture
def di_sensor(hass_mock, base_config):
    """Create a DrynessIndexSensor instance."""
    return DrynessIndexSensor(hass_mock, base_config)


@pytest.fixture
def zone_orto(hass_mock, di_sensor):
    """Create an IrrigationZoneSensor for 'Orto'."""
    zone_config = {
        CONF_ZONE_NAME: "Orto",
        CONF_ZONE_VALVE: "switch.valve_orto",
        CONF_ZONE_AREA: 20.0,
        CONF_ZONE_SYSTEM_TYPE: SYSTEM_TYPE_CUSTOM,
        CONF_ZONE_EFFICIENCY: 0.90,
        CONF_ZONE_FLOW_RATE: 8.0,
        CONF_ZONE_THRESHOLD: 15.0,
    }
    return IrrigationZoneSensor(hass_mock, zone_config, di_sensor)


@pytest.fixture
def zone_prato(hass_mock, di_sensor):
    """Create an IrrigationZoneSensor for 'Prato'."""
    zone_config = {
        CONF_ZONE_NAME: "Prato",
        CONF_ZONE_VALVE: "switch.valve_prato",
        CONF_ZONE_AREA: 50.0,
        CONF_ZONE_SYSTEM_TYPE: SYSTEM_TYPE_CUSTOM,
        CONF_ZONE_EFFICIENCY: 0.70,
        CONF_ZONE_FLOW_RATE: 15.0,
    }
    return IrrigationZoneSensor(hass_mock, zone_config, di_sensor)


@pytest.fixture(autouse=True)
def _fast_auto_open_grace(monkeypatch):
    """Shrink the volume_preset auto-open grace window for tests."""
    import never_dry.controller as _ctrl_mod

    monkeypatch.setattr(_ctrl_mod, "AUTO_OPEN_GRACE_S", 0.05)


@pytest.fixture
def controller(hass_mock, di_sensor, zone_orto, zone_prato):
    """Create an IrrigationController with two zones (fast grace for tests)."""
    return IrrigationController(hass_mock, di_sensor, [zone_orto, zone_prato], inter_zone_delay=0)


@pytest.fixture
def make_state():
    """Factory for mock state objects."""
    return _make_state


@pytest.fixture
def make_event():
    """Factory for mock state change events."""

    def _make(new_value, unit=None):
        event = MagicMock()
        event.data = {"new_state": _make_state(new_value, unit=unit)}
        return event

    return _make
