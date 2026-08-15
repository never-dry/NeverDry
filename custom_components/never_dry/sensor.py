"""Sensor platform for the NeverDry integration.

Provides:
- ETSensor: instantaneous evapotranspiration estimate [mm/h]
- DrynessIndexSensor: reference soil water deficit [mm] (Kc=1.0)
- IrrigationZoneSensor: per-zone deficit, volume, and duration (N instances)
  Each zone tracks its own deficit scaled by a crop coefficient Kc
  that varies seasonally based on the plant family.
"""

from __future__ import annotations

import contextlib
import logging
import math
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from time import monotonic

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    UnitOfArea,
    UnitOfLength,
    UnitOfTime,
    UnitOfVolume,
    UnitOfVolumeFlowRate,
    UnitOfVolumetricFlux,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.typing import ConfigType

from . import flow_utils
from .const import (
    CONF_ALPHA,
    CONF_BACKFILL_DAYS,
    CONF_D_MAX,
    CONF_FIELD_CAPACITY,
    CONF_INTER_ZONE_DELAY,
    CONF_RAIN_SENSOR,
    CONF_RAIN_SENSOR_TYPE,
    CONF_ROOT_DEPTH,
    CONF_T_BASE,
    CONF_TEMP_SENSOR,
    CONF_VWC_SENSOR,
    CONF_ZONE_AREA,
    CONF_ZONE_BATTERY_SENSOR,
    CONF_ZONE_DELIVERY_MODE,
    CONF_ZONE_DELIVERY_TIMEOUT,
    CONF_ZONE_EFFICIENCY,
    CONF_ZONE_EXPOSURE,
    CONF_ZONE_FLOW_METER_SENSOR,
    CONF_ZONE_FLOW_RATE,
    CONF_ZONE_HW_MAX_DURATION_PAYLOAD,
    CONF_ZONE_HW_MAX_DURATION_TOPIC,
    CONF_ZONE_IRRIGATION_MODE,
    CONF_ZONE_IRRIGATION_TIME,
    CONF_ZONE_KC,
    CONF_ZONE_MICROCLIMATE_FACTOR,
    CONF_ZONE_NAME,
    CONF_ZONE_PLANT_FAMILY,
    CONF_ZONE_SYSTEM_TYPE,
    CONF_ZONE_THRESHOLD,
    CONF_ZONE_VALVE,
    CONF_ZONE_VOLUME_ENTITY,
    CONF_ZONES,
    DEFAULT_ALPHA,
    DEFAULT_BACKFILL_DAYS,
    DEFAULT_D_MAX,
    DEFAULT_DELIVERY_MODE,
    DEFAULT_DELIVERY_TIMEOUT_S,
    DEFAULT_EFFICIENCY,
    DEFAULT_FIELD_CAPACITY,
    DEFAULT_INTER_ZONE_DELAY,
    DEFAULT_KC,
    DEFAULT_MICROCLIMATE_FACTOR,
    DEFAULT_RAIN_SENSOR_TYPE,
    DEFAULT_ROOT_DEPTH,
    DEFAULT_T_BASE,
    DEFAULT_THRESHOLD,
    DELIVERY_DURATION_MARGIN,
    DELIVERY_MODE_ESTIMATED_FLOW,
    DELIVERY_MODE_FLOW_METER,
    DOMAIN,
    ET_BUFFER_MIN_READINGS,
    ET_BUFFER_SIZE,
    ET_TEMP_VALID_RANGE,
    EXPOSURES,
    KC_ANCHOR_DAYS,
    MICROCLIMATE_FACTOR_MAX,
    MICROCLIMATE_FACTOR_MIN,
    PLANT_FAMILIES,
    RAIN_TYPE_EVENT,
    SAFETY_LAYER_SPREAD,
    SYSTEM_TYPES,
    UNUSUAL_FLOW_MAX_LPM,
    VALVE_STARTUP_GRACE_S,
)
from .controller import IrrigationController
from .services import async_setup_services
from .unit_convert import LPM_TO_GPH, LPM_TO_LPH
from .valve_fsm import FailureKind, ValveState
from .water_balance_model import vwc_to_fraction
from .zone import Zone as DomainZone

_LOGGER = logging.getLogger(__name__)

#: Failures that mean "the valve did not answer", as opposed to "it answered
#: and no water moved". Only the first is a reachability problem; keeping the
#: set here rather than inline is what stops the two from being conflated the
#: next time a failure kind is added.
_COMMS_FAILURES = frozenset({FailureKind.OPEN_FAILED, FailureKind.CLOSE_VERIFICATION_FAILED})


@dataclass(frozen=True)
class _LitersDelivered:
    """A bare delivered figure, shaped to satisfy ``zone.Delivery``.

    The entity layer still receives plain floats from the controller. Rather
    than widen the Zone's contract to accept them, this wraps one at the seam —
    so the day the controller returns a real ``DeliveryResult`` the wrapper
    simply disappears.
    """

    liters_delivered: float
    elapsed_s: float = 0.0


# ══════════════════════════════════════════════════════════
#  SensorBuffer — rolling median for ET input robustness
# ══════════════════════════════════════════════════════════


class SensorBuffer:
    """Rolling FIFO buffer of valid numeric sensor readings.

    Rejects ``None``, ``'unavailable'``, ``'unknown'``, NaN, ±inf, and
    values outside ``valid_range``. Returns the median of buffered readings
    as a robust estimate; returns ``None`` when fewer than
    ``min_readings`` valid samples are available.
    """

    def __init__(
        self,
        size: int,
        valid_range: tuple[float, float] = (-math.inf, math.inf),
    ) -> None:
        self._size = size
        self._lo, self._hi = valid_range
        self._buf: deque[float] = deque(maxlen=size)

    def push(self, raw) -> bool:
        """Parse and push ``raw`` if it is a valid in-range finite number.

        Returns ``True`` when the value was accepted.
        """
        if raw in (None, "unavailable", "unknown"):
            return False
        try:
            v = float(raw)
        except (ValueError, TypeError):
            return False
        if not math.isfinite(v) or v < self._lo or v > self._hi:
            return False
        self._buf.append(v)
        return True

    def median(self, min_readings: int = 1) -> float | None:
        """Return the median, or ``None`` if fewer than ``min_readings`` samples."""
        if len(self._buf) < min_readings:
            return None
        s = sorted(self._buf)
        n = len(s)
        mid = n // 2
        return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0

    def __len__(self) -> int:
        return len(self._buf)


def _to_celsius(state) -> float | None:
    """Return temperature in °C from a HA State object.

    Converts from °F if unit_of_measurement is '°F'. Returns None when the
    state is unavailable or not numeric.
    """
    if state is None or state.state in ("unavailable", "unknown"):
        return None
    try:
        value = float(state.state)
    except (ValueError, TypeError):
        return None
    if state.attributes.get("unit_of_measurement") == "°F":
        return (value - 32) * 5 / 9
    return value


# ══════════════════════════════════════════════════════════
#  Kc computation
# ══════════════════════════════════════════════════════════


def _as_finite_float(value) -> float | None:
    """Parse ``value`` to a finite float, or ``None`` if it is not one."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def resolve_microclimate_factor(
    exposure: str | None,
    custom_factor: float | None = None,
) -> float:
    """Resolve a zone's microclimate factor (kmc) from its exposure setting.

    **The dropdown decides**, per the preset/override contract in ``const``:
    a preset carries its own factor, and only the custom entry (``factor:
    None``) reads ``custom_factor``, clamped to the MIN/MAX bounds. A factor
    left behind a preset is ignored here; the config flow warns about it
    rather than silently dropping what the user typed.

    Total by design: anything unset, unknown or non-numeric gives a neutral
    1.0 — never 0, which would freeze the deficit, and never an exception,
    which would abort setup for every zone in the entry. Callers log the
    fallback in their own context.
    """
    if not isinstance(exposure, str) or exposure not in EXPOSURES:
        return DEFAULT_MICROCLIMATE_FACTOR
    preset = EXPOSURES[exposure]["factor"]
    if preset is not None:
        return preset

    value = _as_finite_float(custom_factor)
    if value is None:
        return DEFAULT_MICROCLIMATE_FACTOR
    return min(max(value, MICROCLIMATE_FACTOR_MIN), MICROCLIMATE_FACTOR_MAX)


def compute_kc(
    day_of_year: int,
    plant_family: str | None,
    manual_kc: float | None,
    latitude: float = 45.0,
    microclimate_factor: float = DEFAULT_MICROCLIMATE_FACTOR,
) -> float:
    """Compute the effective crop coefficient for a given day of year.

    ``Kc = base * microclimate_factor``.

    **The plant family decides the base**, per the preset/override contract
    in ``const``: the custom family (``kc_seasonal: None``) reads manual_kc,
    every other family follows its seasonal profile and ignores it. A manual
    Kc left behind a real family is not applied — the config flow warns about
    it instead of quietly overriding the curve the user can see selected.

    The factor applies to either base on purpose: it describes the site, not
    the planting, so a shaded zone keeps its seasonal shape (#146).
    """
    if _family_is_custom(plant_family):
        base = manual_kc if manual_kc is not None else DEFAULT_KC
    else:
        base = _seasonal_kc(day_of_year, plant_family, latitude)
    return round(base * microclimate_factor, 4)


def _family_is_custom(plant_family: str | None) -> bool:
    """True when the family carries no curve and defers to the manual Kc."""
    return (
        isinstance(plant_family, str)
        and plant_family in PLANT_FAMILIES
        and PLANT_FAMILIES[plant_family]["kc_seasonal"] is None
    )


def _seasonal_kc(
    day_of_year: int,
    plant_family: str | None,
    latitude: float = 45.0,
) -> float:
    """Interpolate a plant family's seasonal Kc profile for a day of year.

    The profile uses 4 anchor points (winter, spring, summer, autumn) with
    linear interpolation.  For southern hemisphere (latitude < 0) the day
    is shifted by 182 days. Unknown or missing family: DEFAULT_KC (1.0).

    The custom family carries no curve (``kc_seasonal: None``); it means the
    zone follows its manual Kc, which ``compute_kc`` applies before ever
    reaching here. Falling back to DEFAULT_KC covers the one case that
    escapes it — custom selected with no value, which the config flow
    rejects but a hand-edited entry could still contain.
    """
    if plant_family is None or plant_family not in PLANT_FAMILIES:
        return DEFAULT_KC

    kc_values = PLANT_FAMILIES[plant_family]["kc_seasonal"]
    if kc_values is None:
        return DEFAULT_KC

    # Southern hemisphere: shift by half a year
    doy = day_of_year
    if latitude < 0:
        doy = ((doy + 182 - 1) % 365) + 1  # keep in 1-365 range

    anchors = list(KC_ANCHOR_DAYS)  # (15, 105, 196, 288)
    values = list(kc_values)

    # Find surrounding anchors and interpolate
    for i in range(4):
        a1 = anchors[i]
        a2 = anchors[(i + 1) % 4]
        v1 = values[i]
        v2 = values[(i + 1) % 4]

        if a2 > a1:
            # Normal segment (e.g., winter→spring, spring→summer, summer→autumn)
            if a1 <= doy < a2:
                frac = (doy - a1) / (a2 - a1)
                return round(v1 + frac * (v2 - v1), 4)
        else:
            # Wrap-around segment (autumn→winter, crossing year boundary)
            if doy >= a1 or doy < a2:
                span = (365 - a1) + a2
                dist = (doy - a1) % 365
                frac = dist / span
                return round(v1 + frac * (v2 - v1), 4)

    return DEFAULT_KC  # fallback


# ══════════════════════════════════════════════════════════
#  Entity creation helpers
# ══════════════════════════════════════════════════════════


def _hub_device_info(entry_id: str) -> DeviceInfo:
    """Device info for the main NeverDry hub (ET + deficit sensors)."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry_id)},
        name="NeverDry",
        manufacturer="NeverDry",
        model="Smart Watering Controller",
    )


def _zone_device_info(entry_id: str, zone_name: str) -> DeviceInfo:
    """Device info for a zone (sensor + buttons grouped together)."""
    slug = zone_name.lower().replace(" ", "_")
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry_id}_{slug}")},
        name=f"NeverDry {zone_name}",
        manufacturer="NeverDry",
        model="Irrigation Zone",
        via_device=(DOMAIN, entry_id),
    )


def _create_entities(
    hass: HomeAssistant, config: dict, entry_id: str = "yaml"
) -> tuple[list[SensorEntity], DrynessIndexSensor, list[IrrigationZoneSensor]]:
    """Create sensor entities from a config dict (shared by YAML and UI)."""
    hub_device = _hub_device_info(entry_id)
    di_sensor = DrynessIndexSensor(hass, config, hub_device)
    entities: list[SensorEntity] = []
    # In VWC mode the ET model is bypassed entirely — an "ET Hourly
    # Estimate" entity that keeps updating from temperature reads as if
    # the model were still active (tester report, 2026-07-18).
    if not config.get(CONF_VWC_SENSOR):
        entities.append(ETSensor(hass, config, hub_device))
    entities.append(di_sensor)

    zone_sensors: list[IrrigationZoneSensor] = []
    for zone_conf in config.get(CONF_ZONES, []):
        zone_device = _zone_device_info(entry_id, zone_conf[CONF_ZONE_NAME])
        zone_sensor = IrrigationZoneSensor(hass, zone_conf, di_sensor, zone_device)
        zone_sensors.append(zone_sensor)
        entities.append(zone_sensor)
        entities.append(ZoneDeficitSensor(zone_sensor, zone_device))
        entities.append(ZoneRainSensor(zone_sensor, zone_device))
        entities.append(ZoneSessionWaterSensor(zone_sensor, zone_device))
        entities.append(ZoneYearlyWaterSensor(zone_sensor, zone_device))
        # Operational info
        entities.append(ZoneLastIrrigatedSensor(zone_sensor, zone_device))
        entities.append(ZoneLastSourceSensor(zone_sensor, zone_device))
        entities.append(ZoneLastVolumeSensor(zone_sensor, zone_device))
        entities.append(ZoneFlowRateSensor(zone_sensor, zone_device))
        entities.append(ZoneDurationSensor(zone_sensor, zone_device))
        entities.append(ZoneLastDurationSensor(zone_sensor, zone_device))
        entities.append(ZoneKcSensor(zone_sensor, zone_device))
        # Diagnostic (config)
        entities.append(ZoneIrrigationModeSensor(zone_sensor, zone_device))
        entities.append(ZoneIrrigationTimeSensor(zone_sensor, zone_device))
        entities.append(ZoneThresholdSensor(zone_sensor, zone_device))
        entities.append(ZoneAreaSensor(zone_sensor, zone_device))
        entities.append(ZoneEfficiencySensor(zone_sensor, zone_device))
        # Linked mirrors of external entities configured for this zone
        slug = zone_conf[CONF_ZONE_NAME].lower().replace(" ", "_")
        if zone_conf.get(CONF_ZONE_VALVE):
            entities.append(
                ZoneLinkedSensor(
                    hass,
                    zone_conf[CONF_ZONE_VALVE],
                    "Valve",
                    "mdi:valve",
                    f"linked_valve_{slug}",
                    zone_device,
                )
            )
        if zone_conf.get(CONF_ZONE_BATTERY_SENSOR):
            entities.append(
                ZoneLinkedSensor(
                    hass,
                    zone_conf[CONF_ZONE_BATTERY_SENSOR],
                    "Battery",
                    "mdi:battery",
                    f"linked_battery_{slug}",
                    zone_device,
                )
            )
        if zone_conf.get(CONF_ZONE_FLOW_METER_SENSOR):
            entities.append(
                ZoneLinkedSensor(
                    hass,
                    zone_conf[CONF_ZONE_FLOW_METER_SENSOR],
                    "Flow meter",
                    "mdi:gauge",
                    f"linked_flow_{slug}",
                    zone_device,
                )
            )

    # Scope every unique_id to the config entry: static and slug-only ids
    # collide across entries and HA silently drops the duplicates — the
    # second entry was born without entities (GH #116). The registry
    # migration in __init__._async_migrate_unique_ids renames existing
    # installations to this format.
    for entity in entities:
        entity._attr_unique_id = f"{entry_id}_{entity._attr_unique_id}"

    return entities, di_sensor, zone_sensors


_HW_DURATION_KEYWORDS = frozenset({"max", "duration", "time", "irrigation", "timer", "delay"})
_HW_MINUTE_KEYWORDS = frozenset({"min", "minute", "minutes"})


def _discover_hw_max_duration(
    hass: HomeAssistant,
    switch_entity_id: str,
) -> tuple[str | None, float]:
    """Look for a hardware max-duration ``number`` entity on the same HA device.

    Searches the entity registry for ``number.*`` entities sharing the same
    device as ``switch_entity_id`` whose name contains irrigation/duration
    keywords. Returns ``(entity_id, multiplier)`` where multiplier converts
    seconds to the entity's native unit (1.0 for seconds, 1/60 for minutes),
    or ``(None, 1.0)`` when nothing suitable is found.
    """
    from homeassistant.helpers import entity_registry as er
    from homeassistant.helpers.entity_registry import async_entries_for_device

    ent_reg = er.async_get(hass)
    switch_entry = ent_reg.async_get(switch_entity_id)
    if switch_entry is None or switch_entry.device_id is None:
        return None, 1.0

    device_id = switch_entry.device_id
    candidates = [
        entry
        for entry in async_entries_for_device(ent_reg, device_id, include_disabled_entities=False)
        if entry.domain == "number"
        and any(kw in (entry.entity_id + " " + (entry.original_name or "")).lower() for kw in _HW_DURATION_KEYWORDS)
    ]
    if not candidates:
        return None, 1.0

    best = candidates[0]
    state = hass.states.get(best.entity_id)
    unit = (state.attributes.get("unit_of_measurement", "") if state else "").lower()
    multiplier = 1.0 / 60.0 if any(kw in unit for kw in _HW_MINUTE_KEYWORDS) else 1.0
    _LOGGER.debug(
        "Valve '%s' hw_max_duration entity discovered: %s (multiplier=%.4f)",
        switch_entity_id,
        best.entity_id,
        multiplier,
    )
    return best.entity_id, multiplier


def _setup_controller(
    hass: HomeAssistant,
    config: dict,
    di_sensor: DrynessIndexSensor,
    zone_sensors: list[IrrigationZoneSensor],
) -> IrrigationController:
    """Create the irrigation controller and register all services.

    Also builds one :class:`ValveOperator` per zone with a valve and a
    shared :class:`ValveNotifier`. Smart valves controlled in
    ``volume_preset`` mode bypass the operator: their entry is omitted
    from the dict.
    """
    from .valve_notifier import ValveNotifier  # local import: optional path
    from .valve_operator import ValveOperator

    inter_zone_delay = config.get(CONF_INTER_ZONE_DELAY, DEFAULT_INTER_ZONE_DELAY)

    notifier = ValveNotifier(hass)
    valve_operators: dict = {}
    for zs in zone_sensors:
        if not zs.valve:
            continue
        # volume_preset relies on smart-valve self-control; bypass operator.
        if getattr(zs, "delivery_mode", None) == "volume_preset":
            continue
        hw_entity, hw_mult = _discover_hw_max_duration(hass, zs.valve)
        op = ValveOperator(
            hass=hass,
            switch_entity_id=zs.valve,
            flow_sensor_entity_id=zs.flow_meter_sensor,
            zone_name=zs.zone_name,
            notifier=notifier,
            # Callable, not snapshot: re-evaluated at every valve open so the
            # watchdog and hardware timer scale with the current deficit.
            max_open_duration_s=lambda zs=zs: zs.watchdog_timeout,
            hw_max_duration_s=lambda zs=zs: zs.hw_max_duration_s,
            hw_max_duration_entity=hw_entity,
            hw_max_duration_multiplier=hw_mult,
            hw_max_duration_topic=zs.hw_max_duration_topic,
            hw_max_duration_payload_template=zs.hw_max_duration_payload,
        )
        valve_operators[zs.valve] = op
        zs.set_operator(op)

    controller = IrrigationController(
        hass,
        di_sensor,
        zone_sensors,
        inter_zone_delay,
        valve_operators=valve_operators,
        notifier=notifier,
    )
    controller.register_services()
    return controller  # caller may store valve_operators via controller.valve_operators


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities,
    discovery_info=None,
) -> None:
    """Set up the NeverDry sensors from YAML configuration."""
    entities, di_sensor, zone_sensors = _create_entities(hass, config)
    async_add_entities(entities, True)
    controller = _setup_controller(hass, config, di_sensor, zone_sensors)
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN]["_controller_yaml"] = controller
    async_setup_services(hass)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the NeverDry sensors from a config entry (UI)."""
    from .const import DOMAIN

    config = dict(entry.data)
    entities, di_sensor, zone_sensors = _create_entities(hass, config, entry.entry_id)
    async_add_entities(entities, True)
    controller = _setup_controller(hass, config, di_sensor, zone_sensors)
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][f"_controller_{entry.entry_id}"] = controller
    hass.data[DOMAIN][f"_operators_{entry.entry_id}"] = controller.valve_operators
    # Domain services are registered once and dispatch across ALL entries'
    # controllers (GH #105) — never per controller, or the last entry to
    # load would capture every never_dry.* call.
    async_setup_services(hass)


# ══════════════════════════════════════════════════════════
#  ETSensor
# ══════════════════════════════════════════════════════════


class ETSensor(SensorEntity):
    """Instantaneous evapotranspiration estimate [mm/h].

    Uses a simplified linear model: ET_h = max(0, alpha * (T - T_base) / 24)
    """

    _attr_has_entity_name = True
    _attr_name = "ET Hourly Estimate"
    _attr_unique_id = "et_hourly_estimate"
    _attr_device_class = SensorDeviceClass.PRECIPITATION_INTENSITY
    _attr_native_unit_of_measurement = UnitOfVolumetricFlux.MILLIMETERS_PER_HOUR
    # Native precision in mm/h; HA scales up the decimals automatically when the
    # user's unit system converts to in/h (issue #139).
    _attr_suggested_display_precision = 2
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:sun-thermometer"

    def __init__(self, hass: HomeAssistant, config: ConfigType, device_info: DeviceInfo | None = None) -> None:
        self._hass = hass
        self._temp_sensor = config[CONF_TEMP_SENSOR]
        self._alpha = config.get(CONF_ALPHA, DEFAULT_ALPHA)
        self._t_base = config.get(CONF_T_BASE, DEFAULT_T_BASE)
        self._value = 0.0
        if device_info:
            self._attr_device_info = device_info

    async def async_added_to_hass(self) -> None:
        """Register state change listener on temperature sensor."""
        # Through async_on_remove: a listener registered on its own outlives the
        # entity, and every options-flow save reloads the entry — so the dead
        # sensor would keep waking on temperature and writing state forever.
        self.async_on_remove(async_track_state_change_event(self._hass, [self._temp_sensor], self._on_temp_change))

    @callback
    def _on_temp_change(self, event) -> None:
        """Update ET estimate when temperature changes."""
        new_state = event.data.get("new_state")
        if new_state is None:
            return
        t = _to_celsius(new_state)
        if t is not None:
            self._value = max(0.0, self._alpha * (t - self._t_base) / 24)
        self.async_write_ha_state()

    @property
    def native_value(self) -> float:
        return round(self._value, 4)


# ══════════════════════════════════════════════════════════
#  DrynessIndexSensor (reference, Kc=1.0)
# ══════════════════════════════════════════════════════════


class DrynessIndexSensor(SensorEntity, RestoreEntity):
    """Reference soil water deficit [mm] at Kc=1.0.

    Integrates ET - precipitation in real-time using forward Euler
    with variable time steps (event-driven).  Zone sensors register
    as listeners to receive (dt_h, et_h, rain) broadcasts and track
    their own per-zone deficit scaled by Kc.
    """

    _attr_has_entity_name = True
    _attr_name = "Dryness Index"
    _attr_unique_id = "never_dry"
    _attr_device_class = SensorDeviceClass.PRECIPITATION
    _attr_native_unit_of_measurement = UnitOfLength.MILLIMETERS
    # Native precision in mm; HA scales up the decimals automatically when the
    # user's unit system converts to inches (issue #139).
    _attr_suggested_display_precision = 1
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:water-percent-alert"

    def __init__(self, hass: HomeAssistant, config: ConfigType, device_info: DeviceInfo | None = None) -> None:
        self._hass = hass
        self._temp_sensor = config[CONF_TEMP_SENSOR]
        self._rain_sensor = config[CONF_RAIN_SENSOR]
        self._alpha = config.get(CONF_ALPHA, DEFAULT_ALPHA)
        self._t_base = config.get(CONF_T_BASE, DEFAULT_T_BASE)
        self._d_max = config.get(CONF_D_MAX, DEFAULT_D_MAX)
        self._vwc_sensor = config.get(CONF_VWC_SENSOR)
        self._field_cap = config.get(CONF_FIELD_CAPACITY, DEFAULT_FIELD_CAPACITY)
        self._root_depth = config.get(CONF_ROOT_DEPTH, DEFAULT_ROOT_DEPTH)
        self._rain_type = config.get(CONF_RAIN_SENSOR_TYPE, DEFAULT_RAIN_SENSOR_TYPE)
        self._backfill_days = config.get(CONF_BACKFILL_DAYS, DEFAULT_BACKFILL_DAYS)
        self._deficit = 0.0
        # None = baseline unknown (fresh boot): the first reading fixes the
        # baseline WITHOUT crediting. Starting at 0.0 re-credited the whole
        # cumulative/24h rain reading at every restart — 14.2 mm of rain
        # wiped every zone deficit on reboot (field bug, 2026-07-17).
        self._last_rain: float | None = None
        self._last_rain_event_ts = None
        self._last_update = datetime.now()
        self._zone_listeners: list[Callable] = []
        # Rain that fell this calendar year [mm] — a SYSTEM quantity (one sky
        # over the whole garden), so every zone mirrors the same "Rain Yearly"
        # value instead of keeping its own drifting per-zone counter. Resets on
        # 1 Jan. See docs/design_water_balance_reference_model.md (D3).
        self._yearly_rain: float = 0.0
        self._yearly_rain_year: int = datetime.now().year
        self._temp_buffer = SensorBuffer(ET_BUFFER_SIZE, valid_range=ET_TEMP_VALID_RANGE)
        # One warning per condition, not one per poll: a probe reporting
        # percentages does so at every reading, and an unreadable one likewise.
        self._vwc_percent_warned = False
        self._vwc_invalid_warned = False
        if device_info:
            self._attr_device_info = device_info

    def register_zone_listener(self, listener: Callable) -> None:
        """Register a zone sensor callback for ET/rain broadcasts."""
        self._zone_listeners.append(listener)

    @property
    def deficit(self) -> float:
        """Current reference deficit in mm (Kc=1.0)."""
        return self._deficit

    @property
    def yearly_rain(self) -> float:
        """Rain that fell this calendar year [mm] — shared by all zones."""
        return self._yearly_rain

    def _accrue_yearly_rain(self, rain_mm: float) -> None:
        """Add credited rain to the yearly total, resetting on a new year."""
        year = datetime.now().year
        if year != self._yearly_rain_year:
            self._yearly_rain = 0.0
            self._yearly_rain_year = year
        self._yearly_rain += rain_mm

    @property
    def extra_state_attributes(self) -> dict:
        """Expose the rain baseline so it survives restarts via restore_state."""
        attrs: dict = {
            "yearly_rain_mm": round(self._yearly_rain, 2),
            "yearly_rain_year": self._yearly_rain_year,
        }
        if self._last_rain is not None:
            attrs["rain_baseline_mm"] = round(self._last_rain, 2)
            # The baseline is only meaningful against the sensor that
            # produced it: on restore it is discarded if the configured
            # rain sensor has changed in the meantime.
            attrs["rain_baseline_entity"] = self._rain_sensor
        return attrs

    async def async_added_to_hass(self) -> None:
        """Restore previous state and register listeners."""
        last = await self.async_get_last_state()
        restored = False
        if last and last.state not in ("unknown", "unavailable"):
            with contextlib.suppress(ValueError, TypeError):
                self._deficit = float(last.state)
                restored = True
            # Restore the rain baseline (daily_total only) so rain fallen
            # while HA was down is still credited on the first tick. Without
            # it the None sentinel makes the first reading a no-credit
            # baseline fix. Event sensors are gated on the process-local
            # event timestamp instead.
            if self._rain_type != RAIN_TYPE_EVENT:
                baseline = last.attributes.get("rain_baseline_mm")
                baseline_entity = last.attributes.get("rain_baseline_entity")
                if baseline is not None and baseline_entity == self._rain_sensor:
                    with contextlib.suppress(ValueError, TypeError):
                        self._last_rain = float(baseline)
                elif baseline is not None:
                    # Baseline from a different (or unknown, pre-upgrade)
                    # rain sensor: comparing it with the new sensor's scale
                    # would credit phantom rain. Drop it — the next reading
                    # fixes a fresh baseline without crediting.
                    _LOGGER.info(
                        "Rain baseline %s mm belongs to '%s', configured sensor is '%s'"
                        " — discarding, rebaselining on next reading",
                        baseline,
                        baseline_entity,
                        self._rain_sensor,
                    )
            # Restore the yearly rain total; reset it if the calendar year
            # rolled over while HA was down.
            with contextlib.suppress(ValueError, TypeError):
                self._yearly_rain_year = int(last.attributes.get("yearly_rain_year", self._yearly_rain_year))
                self._yearly_rain = float(last.attributes.get("yearly_rain_mm", 0.0))
                if datetime.now().year != self._yearly_rain_year:
                    self._yearly_rain = 0.0
                    self._yearly_rain_year = datetime.now().year

        if not restored:
            if self._vwc_sensor:
                # The backfill replays the ET/rain water balance, which is
                # the wrong model here: the first VWC reading sets the
                # observed deficit directly.
                _LOGGER.info("VWC mode: skipping ET-model backfill")
            else:
                await self._backfill_from_recorder()

        tracked = [self._temp_sensor, self._rain_sensor]
        if self._vwc_sensor:
            tracked.append(self._vwc_sensor)

        # Through async_on_remove, as in ETSensor. This one matters most: a
        # surviving hub keeps broadcasting to the zone listeners of the entry it
        # belonged to, so after a reload two water balances advance in parallel.
        self.async_on_remove(async_track_state_change_event(self._hass, tracked, self._on_sensor_change))

    @callback
    def _on_sensor_change(self, event) -> None:
        """Recalculate deficit on any tracked sensor change."""
        now = datetime.now()
        dt_h = (now - self._last_update).total_seconds() / 3600.0
        self._last_update = now

        # Yearly rain is a SYSTEM quantity (one sky over the whole garden),
        # independent of the deficit model — so it must be credited in BOTH
        # modes. It used to live only in the ET branch below, so VWC mode never
        # accrued it and every "Rain Yearly" sensor read 0 (issue #144). The
        # rain baseline lives on this hub, so _compute_rain_delta() runs once
        # per tick here and its result is reused by the ET branch.
        rain_delta = self._compute_rain_delta()
        if rain_delta > 0:
            self._accrue_yearly_rain(rain_delta)

        if self._vwc_sensor:
            self._update_from_vwc()
            # In VWC mode, broadcast zeros — zones use VWC deficit * Kc. Rain is
            # already credited above; the VWC probe reflects soil moisture
            # (rain included) directly, so it is not subtracted from deficit.
            self._broadcast_to_zones(0.0, 0.0, 0.0)
        else:
            # Push temp into the buffer (converted to °C if sensor reports °F);
            # invalid/unavailable readings are rejected, median stays stable.
            raw_state = self._hass.states.get(self._temp_sensor)
            self._temp_buffer.push(_to_celsius(raw_state))

            t_median = self._temp_buffer.median(ET_BUFFER_MIN_READINGS)
            if t_median is None:
                # Buffer not ready yet (startup); keep deficit frozen.
                self.async_write_ha_state()
                return

            et_h = max(0.0, self._alpha * (t_median - self._t_base) / 24)
            et_dt = et_h * dt_h
            self._deficit = max(0.0, min(self._deficit + et_dt - rain_delta, self._d_max))
            self._broadcast_to_zones(dt_h, et_h, rain_delta)

        self.async_write_ha_state()

    def _broadcast_to_zones(self, dt_h: float, et_h: float, rain: float) -> None:
        """Notify all registered zone sensors with ET/rain data."""
        for listener in self._zone_listeners:
            listener(dt_h, et_h, rain)

    def _update_from_vwc(self) -> None:
        """Update deficit from direct VWC measurement.

        The probe's reading is normalised to a ``[0, 1]`` fraction before it
        meets ``field_capacity`` (see :func:`vwc_to_fraction`): a percentage
        left unconverted drives the bracket negative for every reading and the
        clamp below silently pins the deficit at zero forever (GH #170).
        """
        vwc_state = self._hass.states.get(self._vwc_sensor)
        if vwc_state is None:
            return
        try:
            raw = float(vwc_state.state)
        except (ValueError, TypeError):
            # VWC sensor not yet numeric (boot / unavailable):
            # keep the previous self._deficit unchanged.
            return

        vwc = vwc_to_fraction(raw)
        if vwc is None:
            # Not a water content on any scale (raw ADC count, negative, NaN).
            # Refusing it keeps the last good deficit instead of asserting a
            # saturated soil we have no evidence for.
            if not self._vwc_invalid_warned:
                self._vwc_invalid_warned = True
                _LOGGER.warning(
                    "VWC sensor '%s' reported %s, which is not a volumetric water content "
                    "on either scale (expected 0-1 or 0-100). Reading ignored, deficit held "
                    "at its last value. If the sensor exposes a raw count, it needs "
                    "calibration before NeverDry can read it",
                    self._vwc_sensor,
                    raw,
                )
            return

        if raw > 1.0 and not self._vwc_percent_warned:
            self._vwc_percent_warned = True
            _LOGGER.warning(
                "VWC sensor '%s' reports percentages (%s); reading it as %.3f. "
                "No action needed — logged once so the conversion is visible",
                self._vwc_sensor,
                raw,
                vwc,
            )

        self._deficit = max(0.0, (self._field_cap - vwc) * self._root_depth * 1000)

    def _compute_rain_delta(self) -> float:
        """Compute rain increment since last reading.

        For 'event' type: the sensor value IS the delta (mm per event), gated
        on the sensor's last_updated timestamp so identical consecutive events
        each count once and non-rain recomputes credit nothing.
        For 'daily_total' (any accumulator — midnight-reset total, rolling 24h
        window, or lifetime cumulative): credit ONLY the positive increment
        between readings. A decrease is never precipitation — it is a reset,
        a rolling-window age-out, or a sensor glitch — so it credits zero.
        This single rule is correct for every accumulator without heuristics
        and cannot resurrect old rain as phantom precipitation (field bugs
        2026-07-18 phantom rain at 05:00 and #123 daily-total miscount).
        Converts inches to mm when the sensor reports in imperial units.
        """
        try:
            state = self._hass.states.get(self._rain_sensor)
            rain_now = float(state.state)
            unit = (state.attributes.get("unit_of_measurement") or "").lower().strip()
            if unit in ("in", "inch", "inches"):
                rain_now *= 25.4
        except (TypeError, ValueError, AttributeError):
            return 0.0

        if self._rain_type == RAIN_TYPE_EVENT:
            # The value IS the delta (mm per event). A new event is detected by
            # the sensor's last_updated timestamp, not by value: this counts
            # consecutive identical events (e.g. 2 mm then 2 mm via force_update)
            # while ignoring recomputes triggered by other sensors (temperature),
            # for which the rain state object is unchanged.
            event_ts = getattr(state, "last_updated", None)
            if self._last_rain_event_ts is None:
                # First observation after boot (the timestamp is process-local,
                # never restored): the sensor state is the restore of an event
                # that was already counted before the restart — fix the
                # baseline without crediting it again.
                self._last_rain_event_ts = event_ts
                self._last_rain = rain_now
                return 0.0
            if event_ts is not None and event_ts == self._last_rain_event_ts:
                return 0.0  # same event already counted
            self._last_rain_event_ts = event_ts
            self._last_rain = rain_now
            return max(0.0, rain_now)

        # Accumulator: credit only the positive increment.
        if self._last_rain is None:
            # Baseline unknown (fresh boot, nothing restored): fix it from
            # the current reading without crediting — the accumulation
            # predates this boot.
            self._last_rain = rain_now
            return 0.0
        rain_delta = rain_now - self._last_rain
        if rain_delta < 0:
            # A drop is never precipitation: midnight reset, rolling-window
            # age-out, or sensor glitch. Rebase and credit nothing — fresh
            # rain is credited as the counter climbs again.
            _LOGGER.debug(
                "Rain sensor '%s' dropped %.2f -> %.2f mm — reset/age-out, no rain credited",
                self._rain_sensor,
                self._last_rain,
                rain_now,
            )
            self._last_rain = rain_now
            return 0.0
        self._last_rain = rain_now
        return rain_delta

    def _update_from_model(self, dt_h: float) -> None:
        """Update deficit from ET model and precipitation (standalone).

        Used by unit tests.  The event-driven path in _on_sensor_change
        computes the same logic inline to capture rain_delta for broadcast.
        """
        raw_state = self._hass.states.get(self._temp_sensor)
        self._temp_buffer.push(raw_state.state if raw_state is not None else None)
        t_median = self._temp_buffer.median(ET_BUFFER_MIN_READINGS)
        if t_median is None:
            return

        rain_delta = self._compute_rain_delta()
        et_dt = max(0.0, self._alpha * (t_median - self._t_base) / 24) * dt_h
        self._deficit = max(0.0, min(self._deficit + et_dt - rain_delta, self._d_max))

    async def _backfill_from_recorder(self) -> None:
        """Replay historical T/rain from HA recorder to bootstrap deficit.

        Called only on first-time setup (no RestoreEntity state).
        Fails gracefully if recorder is not available.
        """
        try:
            from homeassistant.components.recorder import get_instance
            from homeassistant.components.recorder.history import (
                get_significant_states,
            )
        except ImportError:
            _LOGGER.warning("Recorder component not available; starting deficit at 0.0")
            return

        instance = get_instance(self._hass)
        if instance is None:
            _LOGGER.warning("Recorder instance not available; starting deficit at 0.0")
            return

        now = datetime.utcnow()
        start_time = now - timedelta(days=self._backfill_days)
        entity_ids = [self._temp_sensor, self._rain_sensor]

        try:
            history = await instance.async_add_executor_job(
                get_significant_states,
                self._hass,
                start_time,
                now,
                entity_ids,
            )
        except Exception:
            _LOGGER.warning(
                "Failed to query recorder for backfill; starting deficit at 0.0",
                exc_info=True,
            )
            return

        if not history:
            _LOGGER.info("No recorder history found for backfill")
            return

        temp_states = history.get(self._temp_sensor, [])
        rain_states = history.get(self._rain_sensor, [])

        if not temp_states:
            _LOGGER.info("No temperature history for backfill")
            return

        deficit = self._replay_water_balance(temp_states, rain_states)
        self._deficit = deficit
        self.async_write_ha_state()

        _LOGGER.info(
            "Backfilled deficit from recorder history: %.2f mm (%d temp states, %d rain states)",
            deficit,
            len(temp_states),
            len(rain_states),
        )

    def _replay_water_balance(
        self,
        temp_states: list,
        rain_states: list,
    ) -> float:
        """Replay the ET water-balance loop over historical states.

        Returns the final deficit value.
        """
        events: list[tuple[datetime, str, float]] = []

        for s in temp_states:
            t = _to_celsius(s)
            if t is not None:
                events.append((s.last_changed, "temp", t))

        for s in rain_states:
            if s.state in ("unknown", "unavailable"):
                continue
            try:
                events.append((s.last_changed, "rain", float(s.state)))
            except (ValueError, TypeError):
                continue

        events.sort(key=lambda e: e[0])

        if not events:
            return 0.0

        deficit = 0.0
        last_temp: float | None = None
        last_rain = 0.0
        last_time = events[0][0]

        for ts, kind, value in events:
            if kind == "temp":
                if last_temp is not None:
                    dt_h = (ts - last_time).total_seconds() / 3600.0
                    et_h = max(0.0, self._alpha * (last_temp - self._t_base) / 24)
                    deficit = max(0.0, min(deficit + et_h * dt_h, self._d_max))
                last_temp = value
                last_time = ts

            elif kind == "rain":
                if last_temp is not None:
                    dt_h = (ts - last_time).total_seconds() / 3600.0
                    et_h = max(0.0, self._alpha * (last_temp - self._t_base) / 24)
                    deficit = max(0.0, min(deficit + et_h * dt_h, self._d_max))
                    last_time = ts

                rain_delta = self._compute_backfill_rain_delta(value, last_rain)
                deficit = max(0.0, deficit - rain_delta)
                last_rain = value

        return deficit

    def _compute_backfill_rain_delta(self, rain_now: float, last_rain: float) -> float:
        """Compute rain delta for backfill replay (same rule as the live path).

        Accumulators credit only the positive increment; a decrease is a
        reset/age-out/glitch and credits nothing. Keeping this identical to
        _compute_rain_delta means a restart mid-day replays the same balance
        the live path produced.
        """
        if self._rain_type == RAIN_TYPE_EVENT:
            if rain_now == last_rain:
                return 0.0
            return max(0.0, rain_now)

        # Accumulator: only positive increments are precipitation.
        rain_delta = rain_now - last_rain
        return rain_delta if rain_delta > 0 else 0.0

    def reset(self) -> None:
        """Reset deficit to zero (called after irrigation)."""
        self._deficit = 0.0
        self._last_update = datetime.now()

    def reset_yearly_rain(self) -> None:
        """Clear the year-to-date rain total [mm] — system-wide.

        Zeroes the source counter the "Rain Yearly [L]" zone sensors derive
        from and re-anchors the calendar year, so a fresh restore attribute is
        written on the next ``async_write_ha_state``. Needed because the total
        persists as a restore attribute that survives a plain reinstall
        (GH forum: yearly rain stuck after switching rain sensor type).
        Historical recorder statistics are left untouched.
        """
        self._yearly_rain = 0.0
        self._yearly_rain_year = datetime.now().year

    def set_deficit_mm(self, value: float) -> None:
        """Set deficit to an arbitrary value [mm] — intended for testing/debugging."""
        self._deficit = max(0.0, min(float(value), self._d_max))
        self._last_update = datetime.now()

    @property
    def native_value(self) -> float:
        return round(self._deficit, 2)


# ══════════════════════════════════════════════════════════
#  IrrigationZoneSensor (per-zone deficit with Kc)
# ══════════════════════════════════════════════════════════


class IrrigationZoneSensor(SensorEntity, RestoreEntity):
    """Per-zone irrigation volume and duration.

    Each zone tracks its own deficit:
        D_zone(t) = clamp(D_zone(t-1) + ET_h * Kc(doy) * Δt - rain, 0, D_max)

    The crop coefficient Kc varies seasonally based on the plant family
    and is auto-adjusted for hemisphere via hass.config.latitude.
    """

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.VOLUME_STORAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfVolume.LITERS
    _attr_icon = "mdi:sprinkler-variant"

    def __init__(
        self,
        hass: HomeAssistant,
        zone_config: dict,
        dryness_sensor: DrynessIndexSensor,
        device_info: DeviceInfo | None = None,
    ) -> None:
        self._hass = hass
        self._dryness = dryness_sensor
        self._zone_name = zone_config[CONF_ZONE_NAME]
        self._valve = zone_config.get(CONF_ZONE_VALVE)
        self._system_type = zone_config.get(CONF_ZONE_SYSTEM_TYPE)
        self._flow_rate = zone_config.get(CONF_ZONE_FLOW_RATE, 0.0)
        if self._flow_rate > UNUSUAL_FLOW_MAX_LPM:
            _LOGGER.warning(
                "Zone '%s': flow_rate_lpm=%.1f L/min (= %.0f L/h) looks unrealistically high "
                "for garden irrigation. If you configured it in L/h, divide by 60 (e.g. %.0f L/h → %.2f L/min).",
                zone_config.get(CONF_ZONE_NAME, "?"),
                self._flow_rate,
                self._flow_rate * 60,
                self._flow_rate,
                self._flow_rate / 60,
            )
        self._threshold = zone_config.get(CONF_ZONE_THRESHOLD, DEFAULT_THRESHOLD)
        self._delivery_mode = zone_config.get(CONF_ZONE_DELIVERY_MODE, DEFAULT_DELIVERY_MODE)
        self._volume_entity = zone_config.get(CONF_ZONE_VOLUME_ENTITY)
        self._flow_meter_sensor = zone_config.get(CONF_ZONE_FLOW_METER_SENSOR)
        self._delivery_timeout = zone_config.get(CONF_ZONE_DELIVERY_TIMEOUT, DEFAULT_DELIVERY_TIMEOUT_S)
        self._battery_sensor = zone_config.get(CONF_ZONE_BATTERY_SENSOR)
        self._irrigation_mode = zone_config.get(CONF_ZONE_IRRIGATION_MODE, "manual")
        self._irrigation_time = zone_config.get(CONF_ZONE_IRRIGATION_TIME)
        self._hw_max_duration_topic: str | None = zone_config.get(CONF_ZONE_HW_MAX_DURATION_TOPIC)
        self._hw_max_duration_payload: str = zone_config.get(CONF_ZONE_HW_MAX_DURATION_PAYLOAD, "{value}")
        self._irrigating = False
        self._no_guard_flow_warned = False
        # Reachability grace: when this zone was built, and whether its valve
        # has ever been seen alive. Monotonic, so a clock change cannot
        # silently extend or end the window.
        self._created_at = monotonic()
        self._valve_seen = False
        self._timeout_caps_job_warned = False
        self._session_listeners: list[Callable] = []
        self._operator = None  # set by _setup_controller after operator creation

        # Kc: manual override > plant family seasonal profile > 1.0,
        # scaled by the site-exposure microclimate factor (kmc).
        self._plant_family = zone_config.get(CONF_ZONE_PLANT_FAMILY)
        self._manual_kc = zone_config.get(CONF_ZONE_KC)
        self._exposure = zone_config.get(CONF_ZONE_EXPOSURE)
        configured_factor = zone_config.get(CONF_ZONE_MICROCLIMATE_FACTOR)
        self._microclimate_factor = resolve_microclimate_factor(self._exposure, configured_factor)
        zone_label = zone_config.get(CONF_ZONE_NAME, "?")
        if self._exposure is not None and self._exposure not in EXPOSURES:
            _LOGGER.warning(
                "Zone '%s': unknown exposure %r — using a neutral microclimate factor of %.2f",
                zone_label,
                self._exposure,
                self._microclimate_factor,
            )
        elif self._exposure is not None and EXPOSURES[self._exposure]["factor"] is None:
            # Against the parsed value: a stored "0.65" resolves to 0.65 and
            # must not warn about itself.
            if self._microclimate_factor != _as_finite_float(configured_factor):
                _LOGGER.warning(
                    "Zone '%s': custom microclimate factor %r is missing or outside [%.2f, %.2f], using %.2f instead",
                    zone_label,
                    configured_factor,
                    MICROCLIMATE_FACTOR_MIN,
                    MICROCLIMATE_FACTOR_MAX,
                    self._microclimate_factor,
                )
        elif configured_factor is not None:
            # The number field is shown for every exposure, so leave a trace
            # when a preset silently wins over it.
            _LOGGER.debug(
                "Zone '%s': exposure %r is a preset (%.2f), ignoring microclimate factor %r",
                zone_label,
                self._exposure,
                self._microclimate_factor,
                configured_factor,
            )

        self._d_max = dryness_sensor._d_max

        # Efficiency: the system type decides, per the preset/override
        # contract in const. Only the custom type (default_efficiency: None)
        # reads the box; behind a real type the box is ignored and the config
        # flow warns. Zones configured before this rule were migrated to the
        # custom type by async_migrate_entry, so their value still applies.
        preset_efficiency = (
            SYSTEM_TYPES[self._system_type]["default_efficiency"]
            if self._system_type and self._system_type in SYSTEM_TYPES
            else DEFAULT_EFFICIENCY
        )
        if preset_efficiency is not None:
            efficiency = preset_efficiency
        else:
            efficiency = zone_config.get(CONF_ZONE_EFFICIENCY, DEFAULT_EFFICIENCY)

        # The domain object behind this entity (A1). It owns the deficit, the
        # water counters and the crediting arithmetic; the entity keeps only
        # what Home Assistant needs — unique_id, device info, published state.
        # The private attributes below survive as properties onto this object so
        # that every existing reader, including the test suite, is unaffected.
        self._zone = DomainZone(
            name=self._zone_name,
            area_m2=zone_config.get(CONF_ZONE_AREA, 0.0),
            efficiency=efficiency,
            plant_family=self._plant_family,
            manual_kc=self._manual_kc,
            exposure=self._exposure,
            microclimate_factor=self._microclimate_factor,
            d_max=self._d_max,
            threshold_mm=self._threshold,
        )
        # The counters default to "no year recorded"; today's entity starts on
        # the current one, and the published attribute must not become null.
        self._zone.counters.yearly_water_year = datetime.now().year

        slug = self._zone_name.lower().replace(" ", "_")
        self._attr_name = "Volume"
        self._attr_unique_id = f"irrigation_zone_{slug}"
        if device_info:
            self._attr_device_info = device_info

        # Register as listener on the dryness sensor
        dryness_sensor.register_zone_listener(self._on_et_update)

    # ── Delegation to the domain Zone (anomaly A1) ──────────────────────────
    #
    # These were plain attributes; they are now views onto ``self._zone``, which
    # is the single storage. Read *and* write are preserved verbatim — in
    # particular the deficit setter does **not** clamp, because the attribute it
    # replaces did not: callers clamp explicitly and the tests rely on assigning
    # arbitrary values. Behaviour is identical by construction; what changes is
    # that there is now one owner of this state instead of thirteen loose fields.

    @property
    def _area(self) -> float:
        return self._zone.area_m2

    @_area.setter
    def _area(self, value: float) -> None:
        self._zone.area_m2 = value

    @property
    def _efficiency(self) -> float:
        return self._zone.efficiency

    @_efficiency.setter
    def _efficiency(self, value: float) -> None:
        self._zone.efficiency = value

    @property
    def _zone_deficit(self) -> float:
        return self._zone.deficit.value_mm

    @_zone_deficit.setter
    def _zone_deficit(self, value: float) -> None:
        self._zone.deficit = self._zone.deficit.with_value(value)

    @property
    def _deficit_at_irrigation_start(self) -> float | None:
        return self._zone.cycle_baseline_mm

    @_deficit_at_irrigation_start.setter
    def _deficit_at_irrigation_start(self, value: float | None) -> None:
        self._zone.cycle_baseline_mm = value

    @property
    def _last_irrigated(self) -> datetime | None:
        return self._zone.last_irrigated

    @_last_irrigated.setter
    def _last_irrigated(self, value: datetime | None) -> None:
        self._zone.last_irrigated = value

    @property
    def _last_irrigation_source(self) -> str | None:
        return self._zone.last_source

    @_last_irrigation_source.setter
    def _last_irrigation_source(self, value: str | None) -> None:
        self._zone.last_source = value

    @property
    def _last_session_duration_s(self) -> int:
        return self._zone.last_duration_s

    @_last_session_duration_s.setter
    def _last_session_duration_s(self, value: int) -> None:
        self._zone.last_duration_s = value

    @property
    def _last_volume_delivered(self) -> float:
        return self._zone.counters.last_volume_l

    @_last_volume_delivered.setter
    def _last_volume_delivered(self, value: float) -> None:
        self._zone.counters.last_volume_l = value

    @property
    def _session_water_delivered(self) -> float:
        return self._zone.counters.session_water_l

    @_session_water_delivered.setter
    def _session_water_delivered(self, value: float) -> None:
        self._zone.counters.session_water_l = value

    @property
    def _total_water_delivered(self) -> float:
        return self._zone.counters.total_water_l

    @_total_water_delivered.setter
    def _total_water_delivered(self, value: float) -> None:
        self._zone.counters.total_water_l = value

    @property
    def _yearly_water_delivered(self) -> float:
        return self._zone.counters.yearly_water_l

    @_yearly_water_delivered.setter
    def _yearly_water_delivered(self, value: float) -> None:
        self._zone.counters.yearly_water_l = value

    @property
    def _yearly_water_year(self) -> int:
        return self._zone.counters.yearly_water_year

    @_yearly_water_year.setter
    def _yearly_water_year(self, value: int) -> None:
        self._zone.counters.yearly_water_year = value

    # ── The irrigation cycle, delegated ─────────────────────────────────────

    def begin_cycle(self) -> None:
        """Open an irrigation cycle, snapshotting the deficit it starts from."""
        self._zone.begin_cycle()

    def credit_delivery(self, liters: float) -> float:
        """Credit delivered liters against the deficit; return the new deficit.

        The single home of ``max(0, baseline - delivered*efficiency/area)``,
        which used to be written out at four call sites.
        """
        return self._zone.credit_delivery(_LitersDelivered(liters)).value_mm

    def settle_cycle(self, liters: float, *, source: str, at: datetime, duration_s: int = 0) -> float:
        """Close a cycle: credit the final figure, stamp it, drop the snapshot."""
        deficit = self._zone.settle(_LitersDelivered(liters, duration_s), source=source, at=at)
        return deficit.value_mm

    async def async_added_to_hass(self) -> None:
        """Restore zone deficit from previous state.

        If no previous state exists (new zone), initialize from the
        global Dryness Index scaled by this zone's Kc — so a new zone
        starts with a realistic deficit instead of zero.
        """
        last = await self.async_get_last_state()
        if last and last.attributes:
            with contextlib.suppress(ValueError, TypeError):
                self._zone_deficit = float(last.attributes.get("deficit_mm", 0.0))
            with contextlib.suppress(ValueError, TypeError):
                ts = last.attributes.get("last_irrigated")
                if ts:
                    self._last_irrigated = datetime.fromisoformat(ts)
                    self._last_volume_delivered = float(last.attributes.get("last_volume_delivered", 0.0))
                    self._last_irrigation_source = last.attributes.get("last_irrigation_source")
                    self._last_session_duration_s = int(last.attributes.get("last_session_duration_s", 0))
            with contextlib.suppress(ValueError, TypeError):
                self._total_water_delivered = float(last.attributes.get("total_water_delivered_l", 0.0))
            with contextlib.suppress(ValueError, TypeError):
                self._yearly_water_delivered = float(last.attributes.get("yearly_water_delivered_l", 0.0))
            with contextlib.suppress(ValueError, TypeError):
                self._yearly_water_year = int(last.attributes.get("yearly_water_year", datetime.now().year))
                # Reset yearly counter if year changed since last save
                if datetime.now().year != self._yearly_water_year:
                    self._yearly_water_delivered = 0.0
                    self._yearly_water_year = datetime.now().year
        else:
            # New zone (no restored state): start at zero rather than
            # inheriting the global reference deficit. The global is an ET
            # accumulator that only resets when ALL zones are irrigated
            # together (controller), so under per-zone irrigation it drifts
            # high and would hand a brand-new zone a spurious "irrigation
            # due" (#123, Rasen 2.1 read 11 mm while its identical sibling
            # was at 0). Starting at 0 accepts a small first-cycle bias — a
            # fresh zone may under-read the real deficit until ET accumulates
            # — which self-corrects on the first irrigation. Frame-agnostic:
            # no dependency on the reference deficit or sibling state.
            # See docs/design_water_balance_reference_model.md (D4).
            self._zone_deficit = 0.0

    def _get_latitude(self) -> float:
        """Get latitude from HA config, default to 45.0 (northern)."""
        try:
            return self._hass.config.latitude
        except AttributeError:
            return 45.0

    def _get_current_kc(self) -> float:
        """Effective Kc for this zone (base * exposure).

        Derived from ``_get_base_kc()`` so the two values published side by
        side in the attributes can never come from different days.
        """
        return round(self._get_base_kc() * self._microclimate_factor, 4)

    def _get_base_kc(self) -> float:
        """Kc before the site-exposure factor (plant family curve or override)."""
        doy = datetime.now().timetuple().tm_yday
        return compute_kc(doy, self._plant_family, self._manual_kc, self._get_latitude())

    def _on_et_update(self, dt_h: float, et_h: float, rain: float) -> None:
        """Update zone-specific deficit when base sensor broadcasts."""
        # In VWC mode (dt_h==0, et_h==0, rain==0), use base deficit * Kc
        if dt_h == 0.0 and et_h == 0.0 and rain == 0.0:
            kc = self._get_current_kc()
            self._zone_deficit = self._dryness.deficit * kc
        else:
            kc = self._get_current_kc()
            self._zone_deficit = max(
                0.0,
                min(self._zone_deficit + et_h * kc * dt_h - rain, self._d_max),
            )
        if getattr(self, "hass", None):
            self.async_write_ha_state()

    @property
    def zone_name(self) -> str:
        """Zone display name."""
        return self._zone_name

    @property
    def valve(self) -> str | None:
        """Entity ID of the valve switch."""
        return self._valve

    @property
    def irrigation_mode(self) -> str:
        """Configured irrigation mode: manual, reactive, or scheduled."""
        return self._irrigation_mode

    @property
    def irrigation_time(self) -> str | None:
        """Configured daily irrigation time (HH:MM) or None."""
        return self._irrigation_time

    @property
    def delivery_mode(self) -> str:
        """Configured delivery mode for this zone."""
        return self._delivery_mode

    @property
    def volume_entity(self) -> str | None:
        """Entity ID of the number entity for volume_preset mode."""
        return self._volume_entity

    @property
    def flow_meter_sensor(self) -> str | None:
        """Entity ID of the flow meter sensor for flow_meter mode."""
        return self._flow_meter_sensor

    @property
    def battery_sensor(self) -> str | None:
        """Entity ID of the battery sensor for low-battery alerts."""
        return self._battery_sensor

    @property
    def delivery_timeout(self) -> int:
        """Safety timeout in seconds for flow_meter and volume_preset modes.

        Two different questions used to share this number. *How long should
        the job take* is a prediction about the work — volume over flow rate.
        *How long do we tolerate before something is wrong* is a bound on
        failure. Combining them with ``max()`` made the configured value a
        floor, so the estimate could only ever loosen the bound: a zone with
        five minutes of work to do was guarded with the one-hour default, and
        a meter that stopped counting kept the valve open for the whole hour
        (GH #173). The user manual has always described this field as an upper
        bound; this restores that.

        So: when the expected duration is known, the timeout is that duration
        plus :data:`DELIVERY_DURATION_MARGIN`, and the configured value caps it
        from above — the user can always tighten, never loosen. With no guard
        flow configured there is no prediction to bound anything with, and the
        configured value is all we have.

        Deliberately based on the guard flow only — never the live meter rate.
        It would be absurd to calibrate the protection *against* a meter using
        that same meter, and a momentary high reading must not be able to
        tighten the watchdog. For the same reason the caller reads this once,
        before opening: the deficit shrinks as water arrives, so a bound
        re-read mid-session would follow the session it is meant to bound.
        """
        expected_s = self._guard_duration_s
        if expected_s <= 0:
            return self._delivery_timeout
        bound_s = round(expected_s * DELIVERY_DURATION_MARGIN)
        if bound_s > self._delivery_timeout and not self._timeout_caps_job_warned:
            # The cap bites: the zone needs more time than the user allows, so
            # it will stop short. Silence here would under-water the zone every
            # cycle with nothing to show for it — the failure this whole bound
            # exists to avoid, only in the other direction.
            self._timeout_caps_job_warned = True
            _LOGGER.warning(
                "Zone '%s' needs about %ds to deliver %.1fL at %.2f L/min, but its safety"
                " timeout is %ds — irrigation will stop short. Raise the safety timeout, or"
                " check that the configured flow rate matches the real one",
                self._zone_name,
                expected_s,
                self.volume_liters,
                self._flow_rate,
                self._delivery_timeout,
            )
        return min(self._delivery_timeout, bound_s)

    @property
    def watchdog_timeout(self) -> int:
        """Second safety layer: fires when the delivery loop itself is stuck or gone.

        A spread above :attr:`delivery_timeout` so it catches that layer's
        failure instead of racing it — the watchdog is armed at valve open while
        the loop only starts counting once the open is confirmed, so equal
        values would make the watchdog trip *first* and report a fault where
        the loop was about to close in good order.

        Capped by the configured timeout like every other layer: the user's
        value means "never run longer than this", so no layer may sit above it.
        Without a guard flow rate there is no room under the cap and all three
        collapse onto it — the same flat ladder as before, which is what the
        configuration deserves when it gives us nothing to derive one from.
        """
        return min(self._delivery_timeout, round(self.delivery_timeout * SAFETY_LAYER_SPREAD))

    @property
    def hw_max_duration_s(self) -> int:
        """Outermost layer, written to the device so it survives Home Assistant.

        A spread above :attr:`watchdog_timeout`, under the same cap.
        """
        return min(self._delivery_timeout, round(self.watchdog_timeout * SAFETY_LAYER_SPREAD))

    @property
    def hw_max_duration_topic(self) -> str | None:
        """Optional raw MQTT topic for writing the on-device hardware max-duration."""
        return self._hw_max_duration_topic

    @property
    def hw_max_duration_payload(self) -> str:
        """Payload template for the hw_max_duration MQTT publish (``{value}`` placeholder)."""
        return self._hw_max_duration_payload

    def set_operator(self, operator) -> None:
        """Attach the ValveOperator so FSM state can be exposed in attributes."""
        self._operator = operator

    @property
    def _within_startup_grace(self) -> bool:
        """True while this valve has never been seen and setup is recent.

        Closes on either condition: the window expires, or the valve is seen
        alive even once. A valve that comes up in twenty seconds and drops out
        at two minutes is reported straight away — the grace covers the absence
        of evidence at startup, not the first five minutes indiscriminately.
        """
        if self._valve_seen:
            return False
        return (monotonic() - self._created_at) < VALVE_STARTUP_GRACE_S

    @property
    def valve_reachable(self) -> bool | None:
        """Whether the valve is *answering* — not whether it works.

        The two are different faults and the user can act on only one of them.
        A valve that never confirms a command is a radio problem: move the
        device, add a router, check the batteries. A valve that confirms and
        moves no water is hydraulic: the supply is off, the filter is clogged.
        The FSM already separates them, so the card can too.

        Evidence, in order of *strength* rather than directness, because that
        ordering is what the startup grace turns on:

        1. **Active** — the last command failed for want of a confirmation
           (``OPEN_FAILED`` / ``CLOSE_VERIFICATION_FAILED``). We asked and got
           nothing back: that is proof, and proof is never suspended. This is
           also the case that actually bites, since a flaky Zigbee valve keeps
           reporting a level and so never looks unavailable (field, 'Giardino
           Pino').
        2. **Passive** — the entity is missing/``unavailable``/``unknown``, or
           the FSM sits in ``UNREACHABLE``. Nobody asked; the valve simply has
           not spoken. Right after a restart that is the normal state of every
           Zigbee entity for a minute or two, so during the startup grace this
           reads as ``None`` rather than as a fault.

        ``None`` means *no evidence either way* and must never be drawn as a
        fault: a zone with no valve, or one we have not heard from yet. The FSM
        clears ``last_failure`` on any clean cycle, so a recovered valve stops
        warning by itself.
        """
        if not self._valve:
            return None

        state = self._hass.states.get(self._valve)
        entity_alive = state is not None and state.state not in ("unavailable", "unknown")
        if entity_alive:
            # Latch, deliberately set from the read path: this entity has no
            # listener of its own on the valve, and the flag only ever goes
            # from false to true.
            self._valve_seen = True

        # Active evidence first — a command that went unanswered outranks the
        # grace window, whatever the clock says.
        if self._operator is not None and self._operator.last_failure in _COMMS_FAILURES:
            return False

        if not entity_alive:
            return None if self._within_startup_grace else False
        if self._operator is not None and self._operator.state == ValveState.UNREACHABLE:
            return None if self._within_startup_grace else False
        return True

    @property
    def is_irrigating(self) -> bool:
        """True if this zone is currently being irrigated."""
        return self._irrigating

    def set_irrigating(self, state: bool) -> None:
        """Set the irrigating state (called by controller)."""
        if state and not self._irrigating:
            # Starting a new irrigation session
            self._session_water_delivered = 0.0
        self._irrigating = state
        self.notify_session_listeners()

    def register_session_listener(self, listener: Callable) -> None:
        """Register a callback fired on live irrigation-session progress.

        Mirrors the dryness ``register_zone_listener`` pattern: dependent
        entities (e.g. the expected-duration sensor) subscribe so they can
        refresh while a session depletes the deficit in real time.
        """
        self._session_listeners.append(listener)

    def notify_session_listeners(self) -> None:
        """Fire the session listeners (called on session start/stop/progress)."""
        for listener in self._session_listeners:
            listener()

    def set_deficit_mm(self, value: float) -> None:
        """Set zone deficit to an arbitrary value [mm] — intended for testing/debugging."""
        self._zone_deficit = max(0.0, min(float(value), self._d_max))

    def reset_yearly_water(self) -> None:
        """Clear this zone's year-to-date irrigated-water total [L].

        Zeroes the counter behind the "Yearly Water" sensor and re-anchors the
        calendar year, so a fresh restore attribute is written on the next
        ``async_write_ha_state``. The lifetime ``total_water_delivered_l`` is
        left untouched — only the yearly total, mirroring the yearly-rain reset.
        """
        self._yearly_water_delivered = 0.0
        self._yearly_water_year = datetime.now().year

    def reset_deficit(self, source: str = "unknown", delivered_liters: float | None = None) -> None:
        """Reset this zone's deficit to zero (called after irrigation).

        When ``delivered_liters`` is provided it is credited to the water
        counters as the actual volume delivered. This is required for
        flow-metered deliveries where ``_zone_deficit`` is depleted in real time
        during the cycle, so ``volume_liters`` would read ~0 by the time the
        cycle settles. When omitted (manual reset / mark-irrigated), the volume
        is derived from the current deficit via ``volume_liters``.
        """
        self._last_irrigation_source = source
        credited = round(self.volume_liters if delivered_liters is None else delivered_liters, 1)
        self._last_volume_delivered = credited
        self._session_water_delivered = credited
        self._total_water_delivered += credited
        # Reset yearly counter on year change
        now = datetime.now()
        if now.year != self._yearly_water_year:
            self._yearly_water_delivered = 0.0
            self._yearly_water_year = now.year
        self._yearly_water_delivered += credited
        self._last_irrigated = now
        self._zone_deficit = 0.0

    @property
    def volume_liters(self) -> float:
        """Volume to irrigate this zone [L]."""
        if self._efficiency <= 0:
            return 0.0
        return self._zone_deficit * self._area / self._efficiency

    @property
    def _guard_duration_s(self) -> int:
        """Expected duration derived from the configured guard flow rate [s].

        This is the stable estimate used for safety scaling: it never
        follows the live meter rate, so a momentary high reading cannot
        tighten the watchdog. Returns 0 when no guard flow is configured.
        """
        if self._flow_rate <= 0:
            return 0
        return round(self.volume_liters / self._flow_rate * 60)

    @property
    def duration_s(self) -> int:
        """Expected irrigation duration for this zone [s].

        Source chain:
        1. Live flow-meter rate (flow_meter mode, rate sensor reading > 0)
           — since ``volume_liters`` shrinks in real time with the deficit,
           during a session this reads as the estimated remaining time.
        2. Configured guard flow rate — flow_meter at rest / volume-only
           meters, volume_preset, and estimated_flow (original behaviour).
        3. 0 — no rate source available; only the ``delivery_timeout``
           floor guards the valve.
        """
        if self._delivery_mode == DELIVERY_MODE_FLOW_METER and self._flow_meter_sensor:
            rate_lpm = flow_utils.read_flow_rate_lpm(self._hass, self._flow_meter_sensor)
            if rate_lpm is not None:
                return round(self.volume_liters / rate_lpm * 60)
        guard = self._guard_duration_s
        # Warn only when the guard FLOW is missing — guard==0 also happens
        # legitimately whenever the deficit (hence volume) is zero, e.g. at
        # the end of every session (field false positive, 2026-07-15 12:06).
        if (
            self._flow_rate <= 0
            and self._delivery_mode != DELIVERY_MODE_ESTIMATED_FLOW
            and not self._no_guard_flow_warned
        ):
            self._no_guard_flow_warned = True
            _LOGGER.warning(
                "Zone '%s' (%s mode) has no guard flow rate configured — expected duration"
                " is unknown and the safety timeout stays at its %ds floor. Set the guard"
                " flow rate in the zone options (it will become required in a future release).",
                self._zone_name,
                self._delivery_mode,
                self._delivery_timeout,
            )
        return guard

    @property
    def native_value(self) -> float:
        return round(self.volume_liters, 1)

    @property
    def extra_state_attributes(self) -> dict:
        kc = self._get_current_kc()
        attrs = {
            "zone_name": self._zone_name,
            "valve": self._valve,
            "delivery_mode": self._delivery_mode,
            "irrigation_mode": self._irrigation_mode,
            "irrigation_time": self._irrigation_time,
            "system_type": self._system_type,
            "plant_family": self._plant_family,
            "kc": round(kc, 3),
            "kc_base": round(self._get_base_kc(), 3),
            "kc_override": self._manual_kc,
            "exposure": self._exposure,
            "microclimate_factor": self._microclimate_factor,
            "area_m2": self._area,
            "efficiency": self._efficiency,
            "flow_rate_lpm": self._flow_rate,
            "flow_rate_lph": round(self._flow_rate * 60.0, 1),
            "threshold_mm": self._threshold,
            "volume_liters": round(self.volume_liters, 1),
            "duration_s": self.duration_s,
            "deficit_mm": round(self._zone_deficit, 2),
            "irrigating": self._irrigating,
        }
        attrs["total_water_delivered_l"] = round(self._total_water_delivered, 1)
        attrs["yearly_water_delivered_l"] = round(self._yearly_water_delivered, 1)
        attrs["yearly_water_year"] = self._yearly_water_year
        attrs["session_water_delivered_l"] = round(self._session_water_delivered, 1)
        if self._last_irrigated:
            attrs["last_irrigated"] = self._last_irrigated.isoformat()
            attrs["last_volume_delivered"] = self._last_volume_delivered
            attrs["last_irrigation_source"] = self._last_irrigation_source
            attrs["last_session_duration_s"] = self._last_session_duration_s
        if self._volume_entity:
            attrs["volume_entity"] = self._volume_entity
        if self._flow_meter_sensor:
            attrs["flow_meter_sensor"] = self._flow_meter_sensor
        if self._delivery_mode != DELIVERY_MODE_ESTIMATED_FLOW:
            attrs["delivery_timeout_s"] = self.delivery_timeout
        if self._operator is not None:
            attrs["valve_fsm_state"] = self._operator.state.value
            attrs["valve_in_maintenance"] = self._operator.is_in_maintenance
            last_failure = self._operator.last_failure
            attrs["valve_last_failure"] = last_failure.value if last_failure else None
        reachable = self.valve_reachable
        if reachable is not None:
            attrs["valve_reachable"] = reachable
        return attrs


# ══════════════════════════════════════════════════════════
#  ZoneDeficitSensor (per-zone deficit in mm)
# ══════════════════════════════════════════════════════════


class ZoneDeficitSensor(SensorEntity):
    """Per-zone soil water deficit [mm].

    Mirrors the zone deficit from the parent IrrigationZoneSensor
    as a dedicated sensor entity, making it visible in the device page.
    """

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.PRECIPITATION
    _attr_name = "Deficit"
    _attr_native_unit_of_measurement = UnitOfLength.MILLIMETERS
    # Native precision in mm; HA scales up the decimals automatically when the
    # user's unit system converts to inches (issue #139).
    _attr_suggested_display_precision = 1
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:water-percent-alert"

    def __init__(
        self,
        zone_sensor: IrrigationZoneSensor,
        device_info: DeviceInfo | None = None,
    ) -> None:
        self._zone_sensor = zone_sensor
        slug = zone_sensor.zone_name.lower().replace(" ", "_")
        self._attr_unique_id = f"deficit_zone_{slug}"
        if device_info:
            self._attr_device_info = device_info
        zone_sensor._dryness.register_zone_listener(self._on_update)

    def _on_update(self, dt_h: float, et_h: float, rain: float) -> None:
        """Update when the dryness sensor broadcasts."""
        if getattr(self, "hass", None):
            self.async_write_ha_state()

    @property
    def native_value(self) -> float:
        return round(self._zone_sensor._zone_deficit, 2)

    @property
    def extra_state_attributes(self) -> dict:
        attrs = {
            "flow_rate_lpm": self._zone_sensor._flow_rate,
            "irrigating": self._zone_sensor._irrigating,
        }
        if self._zone_sensor._last_irrigated:
            attrs["last_session_duration_s"] = self._zone_sensor._last_session_duration_s
        op = self._zone_sensor._operator
        if op is not None:
            attrs["valve_fsm_state"] = op.state.value
            attrs["valve_in_maintenance"] = op.is_in_maintenance
            last_failure = op.last_failure
            attrs["valve_last_failure"] = last_failure.value if last_failure else None
        # The card reads its status chips from this sensor first, so the
        # reachability flag has to be here and not only on the Volume one.
        reachable = self._zone_sensor.valve_reachable
        if reachable is not None:
            attrs["valve_reachable"] = reachable
        return attrs


# ══════════════════════════════════════════════════════════
#  ZoneRainSensor (cumulative rain per zone in mm)
# ══════════════════════════════════════════════════════════


class ZoneRainSensor(SensorEntity):
    """Rain this zone received this calendar year [L].

    Rain is a system quantity — the same mm of sky over the whole garden — but
    shown per zone in LITERS via the zone area (mm x m2 = L), so the number is
    informative for THIS zone instead of an identical mm repeated on every
    card. Resets on 1 Jan. Source: the hub's yearly rain [mm].
    """

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.WATER
    _attr_name = "Rain Yearly"
    _attr_native_unit_of_measurement = UnitOfVolume.LITERS
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_icon = "mdi:weather-rainy"

    def __init__(
        self,
        zone_sensor: IrrigationZoneSensor,
        device_info: DeviceInfo | None = None,
    ) -> None:
        self._zone_sensor = zone_sensor
        slug = zone_sensor.zone_name.lower().replace(" ", "_")
        # unique_id deliberately changed from the old "rain_zone_" when the
        # sensor switched from lifetime mm (device_class precipitation) to
        # per-zone yearly liters (device_class water): a fresh id makes HA
        # create a new entity with no prior statistics, sidestepping the
        # "unit of measurement changed" repair. The old rain_zone_ entity
        # orphans (its broken lifetime mm history is discarded).
        self._attr_unique_id = f"rain_yearly_zone_{slug}"
        if device_info:
            self._attr_device_info = device_info
        zone_sensor._dryness.register_zone_listener(self._on_update)

    def _on_update(self, dt_h: float, et_h: float, rain: float) -> None:
        """Update when the dryness sensor broadcasts."""
        if getattr(self, "hass", None):
            self.async_write_ha_state()

    @property
    def native_value(self) -> float:
        # mm x m2 = liters this zone caught from the shared yearly rain.
        return round(self._zone_sensor._dryness.yearly_rain * self._zone_sensor._area, 1)


# ══════════════════════════════════════════════════════════
#  ZoneSessionWaterSensor (current/last irrigation session in L)
# ══════════════════════════════════════════════════════════


class ZoneSessionWaterSensor(SensorEntity):
    """Water delivered in the current or last irrigation session [L].

    Resets to zero when a new irrigation starts.
    """

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.VOLUME_STORAGE
    _attr_name = "Session water"
    _attr_native_unit_of_measurement = UnitOfVolume.LITERS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:water-pump"

    def __init__(
        self,
        zone_sensor: IrrigationZoneSensor,
        device_info: DeviceInfo | None = None,
    ) -> None:
        self._zone_sensor = zone_sensor
        slug = zone_sensor.zone_name.lower().replace(" ", "_")
        self._attr_unique_id = f"session_water_zone_{slug}"
        if device_info:
            self._attr_device_info = device_info
        zone_sensor._dryness.register_zone_listener(self._on_update)

    def _on_update(self, dt_h: float, et_h: float, rain: float) -> None:
        """Update when the dryness sensor broadcasts."""
        if getattr(self, "hass", None):
            self.async_write_ha_state()

    @property
    def native_value(self) -> float:
        return round(self._zone_sensor._session_water_delivered, 1)


# ══════════════════════════════════════════════════════════
#  ZoneYearlyWaterSensor (yearly cumulative irrigation in L)
# ══════════════════════════════════════════════════════════


class ZoneYearlyWaterSensor(SensorEntity):
    """Water this zone received from irrigation this year [L].

    Irrigation only — the water you delivered, which is the meaningful
    *consumption* figure (device_class WATER feeds the HA Energy dashboard;
    mixing in rain, which you did not consume, would inflate it). Rain is
    reported separately by Rain Yearly. Resets automatically on January 1st.
    """

    _attr_has_entity_name = True
    # WATER (not VOLUME_STORAGE): HA rejects total_increasing on
    # volume_storage — that class means "amount currently stored", while
    # this is a cumulative consumption total (GH #105). WATER also makes
    # the sensor usable in the HA Energy dashboard water tracking.
    _attr_device_class = SensorDeviceClass.WATER
    _attr_name = "Irrigated Yearly"
    _attr_native_unit_of_measurement = UnitOfVolume.LITERS
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_icon = "mdi:calendar-clock"

    def __init__(
        self,
        zone_sensor: IrrigationZoneSensor,
        device_info: DeviceInfo | None = None,
    ) -> None:
        self._zone_sensor = zone_sensor
        slug = zone_sensor.zone_name.lower().replace(" ", "_")
        self._attr_unique_id = f"yearly_water_zone_{slug}"
        if device_info:
            self._attr_device_info = device_info
        zone_sensor._dryness.register_zone_listener(self._on_update)

    def _on_update(self, dt_h: float, et_h: float, rain: float) -> None:
        """Update when the dryness sensor broadcasts."""
        if getattr(self, "hass", None):
            self.async_write_ha_state()

    @property
    def native_value(self) -> float:
        # Irrigation water delivered by this zone only — rain is deliberately
        # NOT included (that is Rain Yearly). Keeping this a pure consumption
        # figure is what makes the WATER device_class / Energy dashboard correct.
        return round(self._zone_sensor._yearly_water_delivered, 1)


# ══════════════════════════════════════════════════════════
#  ZoneDurationSensor / ZoneLastDurationSensor
# ══════════════════════════════════════════════════════════


class ZoneDurationSensor(SensorEntity):
    """Planned irrigation duration for the next session [s]."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_name = "Duration"
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:timer"
    _attr_should_poll = False

    def __init__(
        self,
        zone_sensor: IrrigationZoneSensor,
        device_info: DeviceInfo | None = None,
    ) -> None:
        self._zone_sensor = zone_sensor
        slug = zone_sensor.zone_name.lower().replace(" ", "_")
        self._attr_unique_id = f"duration_zone_{slug}"
        if device_info:
            self._attr_device_info = device_info
        zone_sensor._dryness.register_zone_listener(self._on_update)
        zone_sensor.register_session_listener(self._on_session_update)

    def _on_update(self, dt_h: float, et_h: float, rain: float) -> None:
        if getattr(self, "hass", None):
            self.async_write_ha_state()

    def _on_session_update(self) -> None:
        if getattr(self, "hass", None):
            self.async_write_ha_state()

    @property
    def native_value(self) -> int:
        return self._zone_sensor.duration_s


class ZoneLastDurationSensor(SensorEntity):
    """Actual duration of the last completed irrigation session [s]."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_name = "Last duration"
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:timer"
    _attr_should_poll = False

    def __init__(
        self,
        zone_sensor: IrrigationZoneSensor,
        device_info: DeviceInfo | None = None,
    ) -> None:
        self._zone_sensor = zone_sensor
        slug = zone_sensor.zone_name.lower().replace(" ", "_")
        self._attr_unique_id = f"last_duration_zone_{slug}"
        if device_info:
            self._attr_device_info = device_info
        zone_sensor._dryness.register_zone_listener(self._on_update)

    def _on_update(self, dt_h: float, et_h: float, rain: float) -> None:
        if getattr(self, "hass", None):
            self.async_write_ha_state()

    @property
    def native_value(self) -> int | None:
        if not self._zone_sensor._last_irrigated:
            return None
        return self._zone_sensor._last_session_duration_s


# ══════════════════════════════════════════════════════════
#  Zone diagnostic / info sensors
# ══════════════════════════════════════════════════════════


class _ZoneTextSensor(SensorEntity):
    """Base for zone text sensors shown in device page."""

    _attr_has_entity_name = True

    def __init__(
        self,
        zone_sensor: IrrigationZoneSensor,
        name: str,
        icon: str,
        unique_suffix: str,
        device_info: DeviceInfo | None = None,
        diagnostic: bool = False,
    ) -> None:
        self._zone_sensor = zone_sensor
        self._attr_name = name
        self._attr_icon = icon
        slug = zone_sensor.zone_name.lower().replace(" ", "_")
        self._attr_unique_id = f"{unique_suffix}_{slug}"
        if device_info:
            self._attr_device_info = device_info
        if diagnostic:
            from homeassistant.const import EntityCategory

            self._attr_entity_category = EntityCategory.DIAGNOSTIC


class ZoneLastIrrigatedSensor(_ZoneTextSensor):
    """When the zone was last irrigated."""

    # A TIMESTAMP sensor is rendered by Home Assistant in the viewer's locale
    # (relative "2 hours ago" / localized date-time) instead of the raw ISO
    # string a plain text sensor produced (AI-104).
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, zone_sensor, device_info=None):
        super().__init__(
            zone_sensor,
            "Last irrigated",
            "mdi:clock-outline",
            "last_irrigated_zone",
            device_info,
        )

    @property
    def native_value(self) -> datetime | None:
        ts = self._zone_sensor._last_irrigated
        # A TIMESTAMP sensor must return a timezone-aware datetime; the stored
        # value is naive local time, so attach the local zone.
        return ts.astimezone() if ts else None


class ZoneLastSourceSensor(_ZoneTextSensor):
    """How the zone was last irrigated."""

    def __init__(self, zone_sensor, device_info=None):
        super().__init__(
            zone_sensor,
            "Last source",
            "mdi:information-outline",
            "last_source_zone",
            device_info,
        )

    @property
    def native_value(self) -> str | None:
        return self._zone_sensor._last_irrigation_source


class ZoneFlowRateSensor(_ZoneTextSensor):
    """Configured flow rate for this zone.

    Stored internally in L/min (``_flow_rate``). Displayed in L/h (metric) or
    gal/h (US-customary). Home Assistant does NOT auto-convert the
    ``volume_flow_rate`` device class between unit systems, so we pick the unit
    and value ourselves based on ``hass.config.units``.
    """

    _attr_device_class = SensorDeviceClass.VOLUME_FLOW_RATE

    def __init__(self, zone_sensor, device_info=None):
        super().__init__(
            zone_sensor,
            "Flow rate",
            "mdi:gauge",
            "flow_rate_zone",
            device_info,
        )

    @property
    def _is_imperial(self) -> bool:
        """True when HA runs in US-customary mode (volume unit is gallons)."""
        units = getattr(getattr(self, "hass", None), "config", None)
        units = getattr(units, "units", None)
        return getattr(units, "volume_unit", None) == UnitOfVolume.GALLONS

    @property
    def native_unit_of_measurement(self) -> str:
        if self._is_imperial:
            return UnitOfVolumeFlowRate.GALLONS_PER_HOUR
        return UnitOfVolumeFlowRate.LITERS_PER_HOUR

    @property
    def native_value(self) -> float:
        lpm = self._zone_sensor._flow_rate
        if self._is_imperial:
            return round(lpm * LPM_TO_GPH, 1)
        return round(lpm * LPM_TO_LPH, 1)


class ZoneLastVolumeSensor(_ZoneTextSensor):
    """Volume delivered in the last irrigation."""

    _attr_device_class = SensorDeviceClass.VOLUME_STORAGE
    _attr_native_unit_of_measurement = UnitOfVolume.LITERS

    def __init__(self, zone_sensor, device_info=None):
        super().__init__(
            zone_sensor,
            "Last volume",
            "mdi:water",
            "last_volume_zone",
            device_info,
        )

    @property
    def native_value(self) -> float:
        return round(self._zone_sensor._last_volume_delivered, 1)


class ZoneIrrigationModeSensor(_ZoneTextSensor):
    """Configured irrigation mode."""

    def __init__(self, zone_sensor, device_info=None):
        super().__init__(
            zone_sensor,
            "Irrigation mode",
            "mdi:cog",
            "irrigation_mode_zone",
            device_info,
            diagnostic=True,
        )

    @property
    def native_value(self) -> str:
        return self._zone_sensor._irrigation_mode


class ZoneIrrigationTimeSensor(_ZoneTextSensor):
    """Configured daily irrigation time."""

    def __init__(self, zone_sensor, device_info=None):
        super().__init__(
            zone_sensor,
            "Irrigation time",
            "mdi:clock-time-six",
            "irrigation_time_zone",
            device_info,
            diagnostic=True,
        )

    @property
    def native_value(self) -> str | None:
        return self._zone_sensor._irrigation_time


class ZoneThresholdSensor(_ZoneTextSensor):
    """Configured irrigation threshold."""

    _attr_device_class = SensorDeviceClass.PRECIPITATION
    _attr_native_unit_of_measurement = UnitOfLength.MILLIMETERS
    # Native precision in mm; HA scales up the decimals automatically when the
    # user's unit system converts to inches (issue #139).
    _attr_suggested_display_precision = 1

    def __init__(self, zone_sensor, device_info=None):
        super().__init__(
            zone_sensor,
            "Threshold",
            "mdi:target",
            "threshold_zone",
            device_info,
            diagnostic=True,
        )

    @property
    def native_value(self) -> float:
        return self._zone_sensor._threshold


class ZoneAreaSensor(_ZoneTextSensor):
    """Configured zone area."""

    _attr_device_class = SensorDeviceClass.AREA
    _attr_native_unit_of_measurement = UnitOfArea.SQUARE_METERS

    def __init__(self, zone_sensor, device_info=None):
        super().__init__(
            zone_sensor,
            "Area",
            "mdi:texture-box",
            "area_zone",
            device_info,
            diagnostic=True,
        )

    @property
    def native_value(self) -> float:
        return self._zone_sensor._area


class ZoneEfficiencySensor(_ZoneTextSensor):
    """Configured zone efficiency."""

    def __init__(self, zone_sensor, device_info=None):
        super().__init__(
            zone_sensor,
            "Efficiency",
            "mdi:percent",
            "efficiency_zone",
            device_info,
            diagnostic=True,
        )

    @property
    def native_value(self) -> float:
        return round(self._zone_sensor._efficiency, 2)


class ZoneKcSensor(_ZoneTextSensor):
    """Current crop coefficient Kc (effective: base curve * site exposure)."""

    def __init__(self, zone_sensor, device_info=None):
        super().__init__(
            zone_sensor,
            "Kc",
            "mdi:leaf",
            "kc_zone",
            device_info,
        )
        zone_sensor._dryness.register_zone_listener(self._on_update)

    def _on_update(self, dt_h, et_h, rain):
        if getattr(self, "hass", None):
            self.async_write_ha_state()

    @property
    def native_value(self) -> float:
        return round(self._zone_sensor._get_current_kc(), 3)

    @property
    def extra_state_attributes(self) -> dict:
        """Break the effective Kc into its two factors.

        A shaded zone reads 0.53 in October; this shows it as the 0.70 lawn
        curve times a 0.75 exposure factor.
        """
        zone = self._zone_sensor
        return {
            "kc_base": round(zone._get_base_kc(), 3),
            "exposure": zone._exposure,
            "microclimate_factor": zone._microclimate_factor,
        }


# ══════════════════════════════════════════════════════════
#  ZoneLinkedSensor — mirrors an external HA entity inside
#  the NeverDry zone device (valve, battery, flow meter)
# ══════════════════════════════════════════════════════════


class ZoneLinkedSensor(SensorEntity):
    """Mirrors the state of an external HA entity within the NeverDry zone device.

    Used to surface valve switch state, battery level, and flow meter readings
    directly on the zone device card without leaving the NeverDry UI context.
    Updates in real-time via state-change subscription.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        hass: HomeAssistant,
        source_entity_id: str,
        name: str,
        icon: str,
        unique_id: str,
        device_info: DeviceInfo | None = None,
    ) -> None:
        self._hass = hass
        self._source_entity_id = source_entity_id
        self._attr_name = name
        self._attr_icon = icon
        self._attr_unique_id = unique_id
        if device_info:
            self._attr_device_info = device_info

    async def async_added_to_hass(self) -> None:
        # Through async_on_remove, as in ETSensor and DrynessIndexSensor.
        self.async_on_remove(
            async_track_state_change_event(self.hass, [self._source_entity_id], self._on_source_change)
        )

    @callback
    def _on_source_change(self, event) -> None:
        self.async_write_ha_state()

    @property
    def native_value(self):
        state = self.hass.states.get(self._source_entity_id)
        if state is None or state.state in ("unavailable", "unknown"):
            return None
        raw = state.state
        if raw == "on":
            return "open"
        if raw == "off":
            return "closed"
        try:
            return float(raw)
        except ValueError:
            return raw

    @property
    def native_unit_of_measurement(self) -> str | None:
        state = self.hass.states.get(self._source_entity_id)
        if state:
            return state.attributes.get("unit_of_measurement")
        return None

    @property
    def available(self) -> bool:
        state = self.hass.states.get(self._source_entity_id)
        return state is not None and state.state not in ("unavailable", "unknown")
