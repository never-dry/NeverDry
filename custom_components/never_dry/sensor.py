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
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import ClassVar

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
    CONF_ANEMOMETER_HEIGHT,
    CONF_BACKFILL_DAYS,
    CONF_D_MAX,
    CONF_ET_METHOD,
    CONF_FIELD_CAPACITY,
    CONF_HUMIDITY_SENSOR,
    CONF_INTER_ZONE_DELAY,
    CONF_NET_RADIATION_SENSOR,
    CONF_RAIN_SENSOR,
    CONF_RAIN_SENSOR_TYPE,
    CONF_ROOT_DEPTH,
    CONF_T_BASE,
    CONF_TEMP_MAX_SENSOR,
    CONF_TEMP_MIN_SENSOR,
    CONF_TEMP_SENSOR,
    CONF_VWC_SENSOR,
    CONF_WIND_SPEED_SENSOR,
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
    CONF_ZONE_VWC_SENSOR,
    CONF_ZONES,
    DEFAULT_ALPHA,
    DEFAULT_ANEMOMETER_HEIGHT_M,
    DEFAULT_BACKFILL_DAYS,
    DEFAULT_D_MAX,
    DEFAULT_DELIVERY_MODE,
    DEFAULT_DELIVERY_TIMEOUT_S,
    DEFAULT_EFFICIENCY,
    DEFAULT_ET_METHOD,
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
    ET_METHOD_AUTO,
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
from .environment import DEFAULT_LATITUDE, Environment, RainSensorType
from .services import async_setup_services
from .unit_convert import LITERS_TO_GALLONS, LPM_TO_GPH, LPM_TO_LPH
from .valve_fsm import FailureKind, ValveState
from .water_balance_model import (
    MODEL_CATALOGUE,
    RUNNABLE_INPUTS,
    W_M2_TO_MJ_DAY,
    DailySolarEnergy,
    DiurnalRange,
    ETModel,
    ETStep,
    HargreavesModel,
    HargreavesStep,
    ModelInput,
    PenmanMonteithModel,
    PenmanStep,
    ReferenceFrame,
    VWCPerZoneModel,
    VWCReading,
    WaterBalanceModel,
    build_model,
    net_radiation_mj,
    solar_radiation_from_range,
    vwc_to_fraction,
    wind_at_2m,
)
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


def _read_float(hass, entity_id: str | None) -> float | None:
    """A numeric reading from ``entity_id``, or ``None`` when there is not one.

    Unavailable, unknown, missing and non-numeric all answer the same way on
    purpose: the caller has to decide what to do without a reading, and merging
    the cases stops that decision being made four times.
    """
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None or state.state in ("unavailable", "unknown"):
        return None
    try:
        value = float(state.state)
    except (ValueError, TypeError):
        return None
    return value if math.isfinite(value) else None


def _wind_to_m_s(hass, entity_id: str | None, value: float) -> float:
    """Wind speed in m/s, converting from whatever unit the entity declares.

    Read from the entity rather than assumed: consumer stations report km/h,
    Home Assistant may convert to mph in an imperial system, and the equation
    wants m/s. A factor of 3.6 applied in the wrong direction is not a rounding
    error, it is a different climate.
    """
    state = hass.states.get(entity_id) if entity_id else None
    unit = (state.attributes.get("unit_of_measurement") if state else None) or "m/s"
    unit = str(unit).lower()
    if unit in ("km/h", "kph"):
        return value / 3.6
    if unit in ("mph", "mi/h"):
        return value * 0.44704
    if unit in ("kn", "kt", "knot", "knots"):
        return value * 0.514444
    return value


def _read_solar_mj(hass, entity_id: str | None) -> float | None:
    """Solar radiation as MJ/m2/day, or ``None`` when unreadable.

    A pyranometer reports an instantaneous flux in W/m2 while the equations work
    in daily energy, so the reading is converted rather than compared. An entity
    already publishing MJ/m2/day is passed through.
    """
    value = _read_float(hass, entity_id)
    if value is None:
        return None
    state = hass.states.get(entity_id)
    unit = str((state.attributes.get("unit_of_measurement") if state else None) or "W/m²").lower()
    if "mj" in unit:
        return value
    return value * W_M2_TO_MJ_DAY


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


#: Which derived quantities are worth an entity, per running method. Only the
#: ones that method actually computes: an entity stuck at unknown teaches the
#: user to ignore the whole diagnostic group.
_MODEL_INPUT_ENTITIES: dict[str, tuple[tuple[str, str, str, str], ...]] = {
    "hargreaves": (
        ("derived_diurnal_range_c", "Diurnal temperature range", "°C", "mdi:thermometer-lines"),
        ("derived_temp_max_c", "Daily maximum temperature", "°C", "mdi:thermometer-high"),
        ("derived_temp_min_c", "Daily minimum temperature", "°C", "mdi:thermometer-low"),
    ),
    "penman_monteith": (
        ("derived_diurnal_range_c", "Diurnal temperature range", "°C", "mdi:thermometer-lines"),
        ("derived_temp_max_c", "Daily maximum temperature", "°C", "mdi:thermometer-high"),
        ("derived_temp_min_c", "Daily minimum temperature", "°C", "mdi:thermometer-low"),
        ("derived_solar_mj", "Daily solar radiation", "MJ/m²", "mdi:white-balance-sunny"),
        ("derived_net_radiation_mj", "Net radiation", "MJ/m²", "mdi:sun-angle"),
        ("derived_wind_2m_m_s", "Wind speed at 2 m", "m/s", "mdi:weather-windy"),
    ),
}


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
    if di_sensor.reference_frame is ReferenceFrame.ET:
        entities.append(ETSensor(hass, config, hub_device, hub=di_sensor))
    entities.append(di_sensor)
    entities.append(WaterBalanceMethodSensor(di_sensor, hub_device))
    # The derived quantities, as entities so they get history: the way to judge
    # a computed radiation is to watch it follow the weather for a week.
    for key, name, unit, icon in _MODEL_INPUT_ENTITIES.get(type(di_sensor._model).method_id, ()):
        entities.append(ModelInputSensor(di_sensor, key, name, unit, icon, device_info=hub_device))

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
        entities.append(ZoneMeasuredFlowSensor(zone_sensor, zone_device))
        entities.append(ZoneMeterResolutionSensor(zone_sensor, zone_device))
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
                    "Water meter",
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

    _drop_stale_model_inputs(hass, entry_id, entities)

    return entities, di_sensor, zone_sensors


def _drop_stale_model_inputs(hass: HomeAssistant, entry_id: str, entities: list) -> None:
    """Forget the derived entities the current method does not produce.

    Which derived quantities exist depends on the method, so changing method
    changes the set. Home Assistant keeps an entity that stops being created: it
    survives in the registry and shows as unavailable for ever. Six dead rows in
    the diagnostic group is exactly the noise that teaches people to stop
    reading it — the problem this set was narrowed to avoid, moved one step
    along.

    Only ``model_input_*`` ids are touched, and only for this entry. Everything
    else in the registry is somebody else's business, and a zone sensor removed
    by accident would take its history with it.
    """
    try:
        from homeassistant.helpers import entity_registry as er
    except ImportError:  # pragma: no cover - Home Assistant is always present in production
        return

    registry = er.async_get(hass)
    wanted = {entity._attr_unique_id for entity in entities}
    marker = f"{entry_id}_model_input_"
    for entry in list(registry.entities.values()):
        if entry.config_entry_id != entry_id or not str(entry.unique_id).startswith(marker):
            continue
        if entry.unique_id not in wanted:
            _LOGGER.info(
                "Removing %s: the running water-balance method no longer computes it",
                entry.entity_id,
            )
            registry.async_remove(entry.entity_id)


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

    Also builds one :class:`~.driver.ZoneDriver` per zone with a valve and a
    shared :class:`ValveNotifier`. Smart valves controlled in
    ``volume_preset`` mode bypass the driver: their entry is omitted
    from the dict.
    """
    from .driver import ZoneDriver
    from .valve_notifier import ValveNotifier  # local import: optional path

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
        # The delivery loop still lives in the controller, so no delivery
        # arguments are passed here: this seam is the command layer only —
        # open, close, and the state the two produce.
        op = ZoneDriver(
            hass,
            zs.valve,
            flow_meter_sensor=zs.flow_meter_sensor,
            # The design rate, even though the delivery loop still lives in the
            # controller: the driver needs it to size the flow-verification
            # window (resolution / flow rate — GH #173). Without it that window
            # falls back to a blanket constant, which is the defect being fixed.
            flow_rate_lpm=zs._flow_rate,
            name=zs.zone_name,
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
    """The reference evapotranspiration rate [mm/h] the running model produces.

    It used to compute the simple temperature formula itself, which was true
    while that was the only model. With four of them it became a second opinion:
    the entity published 0.24 mm/h while the model integrating the deficit was
    producing 0.22 — a number nobody was using, sitting on the device next to the
    ones that mattered.

    So it mirrors the hub rather than calculating. This is the same rate every
    zone scales by its Kc, which is what makes it the honest thing to publish
    under this name.
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

    def __init__(
        self,
        hass: HomeAssistant,
        config: ConfigType,
        device_info: DeviceInfo | None = None,
        hub: DrynessIndexSensor | None = None,
    ) -> None:
        """Mirror ``hub``'s rate; without one, fall back to computing the simple formula.

        The fallback exists for the tests that build this entity alone. In the
        integration a hub is always passed.
        """
        self._hass = hass
        self._hub = hub
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
            self._value = ETModel.et_hourly(t, alpha=self._alpha, t_base=self._t_base)
        self.async_write_ha_state()

    @property
    def native_value(self) -> float:
        """The hub's last computed rate, or the simple formula when standing alone."""
        if self._hub is not None:
            return round(self._hub.current_et_rate, 4)
        return round(self._value, 4)

    @property
    def extra_state_attributes(self) -> dict:
        """Which method produced this rate — the same question the deficit raises."""
        return {"et_method": self._hub.active_method} if self._hub is not None else {}


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
        # What this installation *has*, and the shared sky over it. The bindings
        # and the yearly rain used to be eleven loose attributes here; they are
        # one object now, and the private names below survive as views onto it so
        # every existing reader is unaffected.
        self._env = Environment(
            temperature_sensor=config[CONF_TEMP_SENSOR],
            rain_sensor=config[CONF_RAIN_SENSOR],
            soil_moisture_sensor=config.get(CONF_VWC_SENSOR),
            humidity_sensor=config.get(CONF_HUMIDITY_SENSOR),
            wind_speed_sensor=config.get(CONF_WIND_SPEED_SENSOR),
            net_radiation_sensor=config.get(CONF_NET_RADIATION_SENSOR),
            temp_max_sensor=config.get(CONF_TEMP_MAX_SENSOR),
            temp_min_sensor=config.get(CONF_TEMP_MIN_SENSOR),
            rain_sensor_type=RainSensorType(config.get(CONF_RAIN_SENSOR_TYPE, DEFAULT_RAIN_SENSOR_TYPE)),
            backfill_days=config.get(CONF_BACKFILL_DAYS, DEFAULT_BACKFILL_DAYS),
            latitude=getattr(getattr(hass, "config", None), "latitude", DEFAULT_LATITUDE),
            # The published attribute must never be null, so the site starts on
            # the current year rather than on "no year recorded".
            yearly_rain_year=datetime.now().year,
        )
        self._alpha = config.get(CONF_ALPHA, DEFAULT_ALPHA)
        self._t_base = config.get(CONF_T_BASE, DEFAULT_T_BASE)
        self._d_max = config.get(CONF_D_MAX, DEFAULT_D_MAX)
        self._field_cap = config.get(CONF_FIELD_CAPACITY, DEFAULT_FIELD_CAPACITY)
        self._root_depth = config.get(CONF_ROOT_DEPTH, DEFAULT_ROOT_DEPTH)
        # The water balance itself lives in the model, not here. This entity
        # supplies readings and publishes the answer; which physics turns one
        # into the other is the model's business, and it is chosen once — by
        # what the installation declared, which is the capability match.
        self._configured_method = config.get(CONF_ET_METHOD, DEFAULT_ET_METHOD)
        self._method_reason = ""
        self._last_et_rate = 0.0
        # What the model was last fed, split into what came from a sensor and
        # what the integration worked out. Kept so the answer can be checked
        # rather than trusted: a derived maximum that looks wrong, or a radiation
        # balance that does not match the pyranometer, is visible here and
        # nowhere else.
        # Never empty: between a restart and the first computation the entity
        # would otherwise publish half a list with no explanation, which reads
        # as "the feature is not working" rather than "it has not run yet".
        self._last_inputs: dict = {"status": "no reading computed since startup"}
        # Entities that show what the model was fed. They subscribe directly
        # rather than watching this entity's state: the inputs change without
        # the deficit changing — at startup, most obviously — and a listener on
        # the state would sleep through exactly the update that matters.
        self._input_listeners: list[Callable[[], None]] = []
        self._model: WaterBalanceModel = ETModel()
        self._select_model()
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
        self._temp_buffer = SensorBuffer(ET_BUFFER_SIZE, valid_range=ET_TEMP_VALID_RANGE)
        # Observed from the thermometer we already read, so the daily extremes
        # never have to be asked for as two more entities.
        self._diurnal = DiurnalRange()
        # The pyranometer reports power; the equations need the day's energy.
        # Accumulated here rather than converted per reading — see DailySolarEnergy.
        self._solar_day = DailySolarEnergy()
        self._anemometer_h = config.get(CONF_ANEMOMETER_HEIGHT, DEFAULT_ANEMOMETER_HEIGHT_M)
        # One warning per condition, not one per poll: a probe reporting
        # percentages does so at every reading, and an unreadable one likewise.
        self._vwc_percent_warned = False
        self._vwc_invalid_warned = False
        if device_info:
            self._attr_device_info = device_info

    # ── Delegation to the model (the water balance) ─────────────────────────

    @property
    def _deficit(self) -> float:
        """The current deficit [mm] — a view onto the model, which owns it.

        Kept under the old private name so every existing reader and writer is
        unaffected, exactly as the site attributes were when they moved into
        :class:`Environment`. The point is that there is now one storage: an
        assignment here cannot drift from what the model integrates, because it
        *is* what the model integrates.
        """
        return self._model.deficit.value_mm

    @_deficit.setter
    def _deficit(self, value: float) -> None:
        """Adopt a deficit computed outside a reading — a restore or a backfill."""
        self._model.restore(value)

    @property
    def reference_frame(self) -> ReferenceFrame:
        """The frame this hub's deficit is defined against — the model decides it."""
        return self._model.reference_frame

    def _select_model(self, observed_range_c: float | None = None) -> None:
        """Choose the model once, and record in one sentence why.

        Called at construction and again when the recorder has filled the
        diurnal window — that is, at startup, with the evidence in hand. Never
        again: a method that changes while the integration runs would move the
        physics under a garden with nobody watching, which is the failure this
        whole design is built to avoid.

        The reason is kept because the answer alone is not actionable. "Simple"
        tells a user nothing; "simple, because the thermometer shows no real
        daily swing" tells them where to look, and that they may overrule it.
        """
        # A model is state, not only physics: it holds the deficit. Re-selecting
        # after the restore has run would hand back a brand-new object starting
        # at zero, wiping the value the entity just recovered — which is exactly
        # what happened the first time this ran on a live garden.
        carried = self._model.deficit.value_mm if getattr(self, "_model", None) is not None else None

        explicit = self._configured_method != ET_METHOD_AUTO
        self._model = build_model(
            self._env,
            method_id=None if not explicit else self._configured_method,
            diurnal_range_c=observed_range_c,
            alpha=self._alpha,
            t_base=self._t_base,
            d_max=self._d_max,
            field_capacity=self._field_cap,
            root_depth=self._root_depth,
        )
        if carried:
            self._model.restore(carried)
        running = type(self._model).method_id

        if explicit and running == self._configured_method:
            self._method_reason = "chosen explicitly"
        elif explicit:
            self._method_reason = (
                f"'{self._configured_method}' was chosen but this installation cannot run it; "
                f"using the best it supports"
            )
        elif observed_range_c is not None and observed_range_c < DiurnalRange.IMPLAUSIBLE_RANGE_C:
            self._method_reason = (
                f"automatic: the temperature sensor shows a daily swing of only "
                f"{observed_range_c:.1f} °C, which is too flat to be real weather — so the methods "
                f"that read the daily range were left out. If your sensor is genuinely outdoors and "
                f"you want Hargreaves-Samani, select it explicitly."
            )
        else:
            self._method_reason = "automatic: the best method the declared sensors support"

    @property
    def current_et_rate(self) -> float:
        """The reference rate [mm/h] most recently broadcast to the zones."""
        return self._last_et_rate

    @property
    def active_method(self) -> str:
        """The identifier of the method actually running.

        Not the one configured: ``auto`` has to resolve to something, and a
        stored choice whose sensors are gone degrades rather than fails. Those
        are the two cases where asking the configuration gives the wrong answer,
        and they are exactly the cases a user needs to see.
        """
        return type(self._model).method_id

    # ── Delegation to the site (Environment) ────────────────────────────────
    #
    # Views onto ``self._env``, which is the single storage. Read-only where the
    # code only reads: a binding that changed after setup would leave the
    # listeners subscribed to the old entity, so it is not a thing to allow by
    # accident — the entry reloads instead.

    @property
    def environment(self) -> Environment:
        """The site this hub reads from. The zones need its latitude and frame."""
        return self._env

    @property
    def _temp_sensor(self) -> str:
        return self._env.temperature_sensor

    @property
    def _rain_sensor(self) -> str:
        return self._env.rain_sensor

    @property
    def _vwc_sensor(self) -> str | None:
        return self._env.soil_moisture_sensor

    @property
    def _rain_type(self) -> RainSensorType:
        return self._env.rain_sensor_type

    @property
    def _backfill_days(self) -> int:
        return self._env.backfill_days

    @property
    def _yearly_rain(self) -> float:
        return self._env.yearly_rain_mm

    @_yearly_rain.setter
    def _yearly_rain(self, value: float) -> None:
        self._env.yearly_rain_mm = value

    @property
    def _yearly_rain_year(self) -> int:
        return self._env.yearly_rain_year

    @_yearly_rain_year.setter
    def _yearly_rain_year(self, value: int) -> None:
        self._env.yearly_rain_year = value

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
        """Add credited rain to the yearly total — the site keeps the books.

        The roll-over on a new year used to be written out here as well as in
        ``Environment``; one copy is enough, and it is the one that can be tested
        without a Home Assistant runtime.
        """
        self._env = self._env.accrue_yearly_rain(rain_mm, year=datetime.now().year)

    @property
    def extra_state_attributes(self) -> dict:
        """Expose the rain baseline so it survives restarts via restore_state."""
        attrs: dict = {
            "yearly_rain_mm": round(self._yearly_rain, 2),
            "yearly_rain_year": self._yearly_rain_year,
            # Which physics is actually running. Published because the choice can
            # differ from what was asked for — `auto` resolves it, and a stored
            # choice the site no longer supports degrades — and a user who cannot
            # see the answer is reading a number without knowing how it was made.
            "et_method": self.active_method,
            "et_method_configured": self._configured_method,
            "et_method_reason": self._method_reason,
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
                    # A restart across midnight on 1 January. Clearing and
                    # re-anchoring is the site's own rule, not a third copy of it.
                    self._env = self._env.reset_yearly_rain(year=datetime.now().year)

        if not restored:
            if self._vwc_sensor:
                # The backfill replays the ET/rain water balance, which is
                # the wrong model here: the first VWC reading sets the
                # observed deficit directly.
                _LOGGER.info("VWC mode: skipping ET-model backfill")
            else:
                await self._backfill_from_recorder()

        # The automatic choice is made once, here, with the evidence rather than
        # without it. Filling the diurnal window from the recorder first means a
        # site is judged on the swing its thermometer actually shows, and that a
        # richer tier starts working immediately instead of after a day of
        # warm-up.
        await self._bootstrap_diurnal_range()
        if self._configured_method == ET_METHOD_AUTO:
            observed = self._diurnal.extremes()
            self._select_model(None if observed is None else observed[1] - observed[0])
        _LOGGER.info("Water balance method: %s — %s", self.active_method, self._method_reason)

        self._publish_initial_inputs()

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

        # The branch follows the **model**, not the sensor. They used to be the
        # same question, because a declared probe was the only way to get the VWC
        # frame. Since the method can be chosen, they can disagree — a site with
        # a probe that picks the simple tier — and branching on the sensor then
        # feeds a VWC reading to an ET model, which raises on every update.
        if self._model.reference_frame is not ReferenceFrame.ET:
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

            # Every temperature reading also feeds the diurnal range, whether or
            # not the running tier uses it: switching method must not mean
            # waiting a day for the window to fill.
            hours = now.timestamp() / 3600.0
            self._diurnal.observe(hours, t_median)
            watts = _read_float(self._hass, self._env.net_radiation_sensor)
            if watts is not None:
                self._solar_day.observe(hours, watts)

            # The reading goes to the model and the model does the physics. The
            # rate is asked for separately because the zones need it: each one
            # integrates the same rate against its own Kc, so the tier the site
            # runs reaches every zone through this broadcast, with no zone
            # needing to know which tier that is.
            reading = self._build_reading(dt_h, t_median, rain_delta, now)
            if reading is None:
                # The running model needs something the site cannot supply yet —
                # a diurnal range still filling. Freeze rather than guess: a
                # partial range understates the weather, and understated weather
                # is a garden watered less than it needs.
                self.async_write_ha_state()
                return
            et_h = self._model.et_rate(reading)
            self._model.step(reading)
            self._broadcast_to_zones(dt_h, et_h, rain_delta)

        self.async_write_ha_state()

    def _build_reading(self, dt_h: float, temp_c: float, rain_mm: float, now: datetime) -> ModelInput | None:
        """The reading the *running* model consumes, or ``None`` if not available yet.

        The hub knows how to observe; each model knows what it needs. This is the
        one place the two meet, and it is deliberately a single function: a model
        that is offered without an entry here is selectable and not runnable,
        which is the failure this shape exists to make impossible.
        """
        if isinstance(self._model, PenmanMonteithModel):
            return self._build_penman_reading(dt_h, temp_c, rain_mm, now)
        if isinstance(self._model, HargreavesModel):
            extremes = self._diurnal.extremes()
            if extremes is None:
                # Warming up: the tier falls back to the temperature-only rate
                # rather than freezing, because a fresh install would otherwise
                # look like a garden that never dries out.
                self._last_inputs = self._warming_up_inputs(temp_c)
                return ETStep(dt_h=dt_h, temp_c=temp_c, rain_mm=rain_mm)
            tmin, tmax = extremes
            self._last_inputs = {
                "status": "computing",
                "measured_temperature_c": round(temp_c, 2),
                "derived_temp_max_c": round(tmax, 2),
                "derived_temp_min_c": round(tmin, 2),
                "derived_diurnal_range_c": round(tmax - tmin, 2),
                "diurnal_window_hours": self._diurnal.coverage_h,
            }
            return HargreavesStep(
                dt_h=dt_h,
                tmax_c=tmax,
                tmin_c=tmin,
                day_of_year=now.timetuple().tm_yday,
                rain_mm=rain_mm,
            )
        self._last_inputs = {
            "status": "computing",
            "measured_temperature_c": round(temp_c, 2),
            "diurnal_window_hours": self._diurnal.coverage_h,
        }
        return ETStep(dt_h=dt_h, temp_c=temp_c, rain_mm=rain_mm)

    def register_input_listener(self, callback_fn: Callable[[], None]) -> Callable[[], None]:
        """Subscribe to changes of the published model inputs; returns the unsubscribe."""
        self._input_listeners.append(callback_fn)

        def _unsubscribe() -> None:
            with contextlib.suppress(ValueError):
                self._input_listeners.remove(callback_fn)

        return _unsubscribe

    def _notify_input_listeners(self) -> None:
        """Tell the derived entities to republish."""
        for listener in list(self._input_listeners):
            listener()

    def _publish_initial_inputs(self) -> None:
        """Fill the derived values once at startup, without touching the balance.

        Everything derived reads *unknown* until the first temperature change,
        and an entity that says nothing after a restart is indistinguishable
        from one that is broken — which is how three separate looks at this
        device concluded the feature was not working.

        Deliberately not a full tick: running one would fix the rain baseline
        earlier than the design intends, and moving rain accounting to make a
        display look better is the wrong trade. This builds the reading for its
        inputs and throws it away.
        """
        if self.reference_frame is not ReferenceFrame.ET:
            return
        raw_state = self._hass.states.get(self._temp_sensor)
        temp = _to_celsius(raw_state)
        if temp is None:
            return
        with contextlib.suppress(Exception):
            self._build_reading(0.0, temp, 0.0, datetime.now())
        self._notify_input_listeners()

    def _warming_up_inputs(self, temp_c: float) -> dict:
        """What to publish while a tier cannot yet compute its own rate.

        Saying "warming up, N hours of 24" is the difference between a user
        seeing the method work and a user seeing nothing and assuming it does
        not.
        """
        return {
            "status": "warming up",
            "measured_temperature_c": round(temp_c, 2),
            "diurnal_window_hours": self._diurnal.coverage_h,
            "warming_up_because": f"the daily range needs {DiurnalRange.MIN_COVERAGE_H} hours of readings",
        }

    def _build_penman_reading(self, dt_h: float, temp_c: float, rain_mm: float, now: datetime) -> ModelInput | None:
        """The full-weather reading, with the net radiation computed rather than read.

        A pyranometer reports the shortwave arriving; FAO-56 needs the *balance*,
        which also depends on what the ground loses to the sky — and that term
        needs the day's extremes and the humidity. So this waits for the diurnal
        window like Hargreaves does, even though the tier reads a radiation
        sensor: without the extremes the longwave half cannot be computed.

        With no pyranometer the radiation is estimated from the same extremes
        (FAO-56 eq. 50), which is what makes the tier reachable for a site that
        has humidity and wind but no radiation instrument.
        """
        extremes = self._diurnal.extremes()
        if extremes is None:
            self._last_inputs = self._warming_up_inputs(temp_c)
            return ETStep(dt_h=dt_h, temp_c=temp_c, rain_mm=rain_mm)
        tmin, tmax = extremes

        rh = _read_float(self._hass, self._env.humidity_sensor)
        wind_raw = _read_float(self._hass, self._env.wind_speed_sensor)
        if rh is None or wind_raw is None:
            # A sensor unavailable this tick is not a reason to stop watering:
            # fall back for this step and pick the richer reading up on the next.
            self._last_inputs = {
                **self._warming_up_inputs(temp_c),
                "warming_up_because": "a required sensor was unreadable this tick",
            }
            return ETStep(dt_h=dt_h, temp_c=temp_c, rain_mm=rain_mm)

        ra = HargreavesModel.extraterrestrial_radiation(now.timetuple().tm_yday, self._env.latitude)
        measured_solar = _read_float(self._hass, self._env.net_radiation_sensor)
        # The day's energy, not the current flux scaled to a day: an evening
        # reading treated as a daily average understates the radiation by a
        # factor of several, and every number downstream inherits it.
        solar = self._solar_day.energy_mj()
        if solar is None:
            solar = solar_radiation_from_range(ra, tmax_c=tmax, tmin_c=tmin)

        wind_2m = wind_at_2m(_wind_to_m_s(self._hass, self._env.wind_speed_sensor, wind_raw), self._anemometer_h)
        net_rad = net_radiation_mj(solar_mj=solar, ra_mj=ra, tmax_c=tmax, tmin_c=tmin, rh_pct=rh)

        self._last_inputs = {
            "status": "computing",
            "measured_temperature_c": round(temp_c, 2),
            "measured_humidity_pct": round(rh, 1),
            "measured_wind_raw": round(wind_raw, 2),
            "measured_solar_w_m2": round(measured_solar, 1) if measured_solar is not None else None,
            "derived_temp_max_c": round(tmax, 2),
            "derived_temp_min_c": round(tmin, 2),
            "derived_diurnal_range_c": round(tmax - tmin, 2),
            "diurnal_window_hours": self._diurnal.coverage_h,
            "derived_wind_2m_m_s": round(wind_2m, 2),
            "derived_solar_mj": round(solar, 2),
            "solar_is_measured": self._solar_day.energy_mj() is not None,
            "solar_window_hours": self._solar_day.coverage_h,
            "derived_extraterrestrial_mj": round(ra, 2),
            "derived_net_radiation_mj": round(net_rad, 2),
        }
        return PenmanStep(
            dt_h=dt_h,
            temp_c=temp_c,
            rh_pct=rh,
            wind_m_s=wind_2m,
            net_radiation_mj=net_rad,
            rain_mm=rain_mm,
        )

    def _broadcast_to_zones(self, dt_h: float, et_h: float, rain: float) -> None:
        """Notify all registered zone sensors with ET/rain data."""
        self._last_et_rate = et_h
        self._notify_input_listeners()
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

        deficit = self._model.step(VWCReading(vwc=vwc))
        self._last_inputs = {
            "status": "computing",
            "measured_soil_moisture_raw": round(raw, 3),
            "derived_soil_moisture_fraction": round(vwc, 3),
            "derived_deficit_mm": round(deficit.value_mm, 2),
        }

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
        self._model.step(ETStep(dt_h=dt_h, temp_c=t_median, rain_mm=rain_delta))

    async def _bootstrap_diurnal_range(self) -> None:
        """Fill the daily windows from recorder history, so the choice is informed.

        Without it the window starts empty at every restart and the site is
        judged, or left warming up, on no data at all. The thermometer almost
        always predates the integration, so the evidence is usually already
        there — it just has to be read.

        Failures are not fatal and not reported loudly: an installation without
        a recorder simply fills the window live, which is the previous behaviour.
        """
        try:
            from homeassistant.components.recorder import get_instance
            from homeassistant.components.recorder.history import get_significant_states
        except ImportError:
            return

        instance = get_instance(self._hass)
        if instance is None:
            return

        now = datetime.now(UTC)
        start = now - timedelta(hours=26)
        wanted = [self._temp_sensor]
        if self._env.net_radiation_sensor:
            wanted.append(self._env.net_radiation_sensor)
        try:
            history = await instance.async_add_executor_job(get_significant_states, self._hass, start, now, wanted)
        except Exception as exc:
            _LOGGER.debug("Diurnal range bootstrap skipped: %s", exc)
            return

        seen = 0
        for state in (history or {}).get(self._temp_sensor, []):
            value = _to_celsius(state)
            if value is None:
                continue
            self._diurnal.observe(state.last_changed.timestamp() / 3600.0, value)
            seen += 1
        for state in (history or {}).get(self._env.net_radiation_sensor or "", []):
            try:
                watts = float(state.state)
            except (ValueError, TypeError):
                continue
            self._solar_day.observe(state.last_changed.timestamp() / 3600.0, watts)

        if seen:
            _LOGGER.debug(
                "Diurnal range bootstrapped from %d readings — %d hours covered; solar window %d hours",
                seen,
                self._diurnal.coverage_h,
                self._solar_day.coverage_h,
            )

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
                    et_h = ETModel.et_hourly(last_temp, alpha=self._alpha, t_base=self._t_base)
                    deficit = max(0.0, min(deficit + et_h * dt_h, self._d_max))
                last_temp = value
                last_time = ts

            elif kind == "rain":
                if last_temp is not None:
                    dt_h = (ts - last_time).total_seconds() / 3600.0
                    et_h = ETModel.et_hourly(last_temp, alpha=self._alpha, t_base=self._t_base)
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
        self._env = self._env.reset_yearly_rain(year=datetime.now().year)

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
        self._last_valve_test: dict | None = None
        self._battery_sensor = zone_config.get(CONF_ZONE_BATTERY_SENSOR)
        self._irrigation_mode = zone_config.get(CONF_ZONE_IRRIGATION_MODE, "manual")
        self._irrigation_time = zone_config.get(CONF_ZONE_IRRIGATION_TIME)
        self._hw_max_duration_topic: str | None = zone_config.get(CONF_ZONE_HW_MAX_DURATION_TOPIC)
        self._hw_max_duration_payload: str = zone_config.get(CONF_ZONE_HW_MAX_DURATION_PAYLOAD, "{value}")
        self._irrigating = False
        self._awaiting_valve = False
        self._silence_verdict: bool | None = None
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

        # A probe of this zone's own, if declared. When present it replaces the
        # site's model entirely for this zone: the reading is a measurement of
        # the soil being watered, and no estimate can improve on that.
        self._own_probe = zone_config.get(CONF_ZONE_VWC_SENSOR)
        self._own_probe_warned = False
        self._probe_vwc: float | None = None
        self._probe_implied_mm: float | None = None
        self._probe_model = (
            VWCPerZoneModel(
                source=self._zone_name,
                field_capacity=dryness_sensor._field_cap,
                root_depth=dryness_sensor._root_depth,
                d_max=self._d_max,
            )
            if self._own_probe
            else None
        )

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
            # The frame follows the hub's model, not a default. With a soil
            # probe configured the deficit is a VWC measurement scaled by Kc,
            # not an ET integration, and a Deficit that says otherwise is the
            # value object asserting the one thing it exists to prevent. Nothing
            # reads the frame yet; tagging it correctly now is what stops the
            # first reader from inheriting a lie.
            frame=ReferenceFrame.VWC_SYSTEM if dryness_sensor._vwc_sensor else ReferenceFrame.ET,
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
        """Close a cycle: credit the final figure, stamp it, drop the snapshot.

        The session listeners are fired **here** rather than at ``set_irrigating``
        because this is where the session's figures are written. Closing the
        valve happens first and would notify too early: the last-session sensors
        would refresh while still holding the previous run's numbers, and then
        wait for the next ET tick to catch up. That left a window of minutes in
        which the card described the previous irrigation as if it were the one
        that had just ended, with nothing to say the value was stale — and the
        minutes right after a run are exactly when somebody looks (field,
        2026-08-19: a 6039 s session reading "2" for six minutes).
        """
        deficit = self._zone.settle(_LitersDelivered(liters, duration_s), source=source, at=at)
        self.notify_session_listeners()
        return deficit.value_mm

    async def async_added_to_hass(self) -> None:
        """Restore zone deficit from previous state.

        If no previous state exists (new zone), initialize from the
        global Dryness Index scaled by this zone's Kc — so a new zone
        starts with a realistic deficit instead of zero.
        """
        if self._own_probe:
            self.async_on_remove(async_track_state_change_event(self.hass, [self._own_probe], self._on_own_probe))

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
            # The supervised test result, rebuilt from the attributes it published.
            # Without this, *saving* the measured flow rate destroyed the evidence
            # for it: writing the config reloads the entry, the entity is recreated,
            # and the number you had just read vanished from the screen — on every
            # zone, not only the one saved. It also meant the statistics series lost
            # a point each time somebody acted on it.
            restored_test = {
                key[len("valve_test_") :]: value
                for key, value in last.attributes.items()
                if key.startswith("valve_test_")
            }
            if restored_test:
                self._last_valve_test = {"zone_name": self._zone_name, **restored_test}

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
        """The site's latitude, which decides the hemisphere of the Kc curve.

        Read from the site rather than from ``hass.config`` directly: it is a
        property of the place, and the place is what ``Environment`` is. Fixed at
        setup, so moving house takes a reload — which moving house does anyway.
        """
        return self._dryness.environment.latitude

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

    def _on_own_probe(self, event) -> None:
        """Record this zone's probe reading. It does **not** own the deficit.

        A point measurement does not generalise to the zone, and the reason is
        empirical rather than theoretical: two zones on the same soil sit at
        systematically different moisture because the irrigation is unbalanced
        and one has far more shade on the ground. Both are circumstances of a
        spot. Letting the deficit follow that reading would take an imbalance in
        the plumbing and a difference in canopy and feed them back as if they
        were information about how much water the soil needs.

        What a probe legitimately generalises is what the *soil* does — the field
        capacity it settles at after drainage, and how fast it drains and dries.
        Those are properties of the texture, not of the plant above it, and both
        need a curve over days rather than a single reading. So the reading is
        published and the deficit stays with the model that covers the whole zone.

        The probe-implied deficit is published beside it on purpose: the gap
        between predicted and measured after an irrigation is the one signal that
        reveals a hydraulic fault — thirty litres delivered and the moisture did
        not move means a clogged emitter or a closed tap, which nothing else in
        the system can see.
        """
        state = event.data.get("new_state") if event else self._hass.states.get(self._own_probe)
        if state is None or state.state in ("unavailable", "unknown"):
            return
        try:
            raw = float(state.state)
        except (ValueError, TypeError):
            return
        vwc = vwc_to_fraction(raw)
        if vwc is None:
            if not self._own_probe_warned:
                self._own_probe_warned = True
                _LOGGER.warning(
                    "Zone '%s': probe '%s' reported %s, which is not a water content on either "
                    "scale (expected 0-1 or 0-100). Reading ignored, deficit held at its last value",
                    self._zone_name,
                    self._own_probe,
                    raw,
                )
            return
        self._probe_vwc = vwc
        self._probe_implied_mm = self._probe_model.step(VWCReading(vwc=vwc)).value_mm
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
        # Same comparison as `timeout_caps_duration`, and for the same reason:
        # the log used to fire on `bound_s`, so it announced that irrigation
        # "will stop short" whenever the allowance was tighter than twice the
        # job — including when the job fitted with room to spare.
        if expected_s > self._delivery_timeout and not self._timeout_caps_job_warned:
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
    def timeout_caps_duration(self) -> bool:
        """True when the safety timeout is shorter than the job needs.

        The zone will stop short: it opens, it runs out of allowance, and it
        closes having delivered less than the deficit asked for. Nothing is
        broken and nothing errors — the garden is simply under-watered every
        cycle, which is the quietest failure this project has.

        Published so the card can flag it without re-deriving the rule. The
        comparison lives here, next to the clamp it describes, because a second
        copy in JavaScript would be a second source of truth for a safety
        bound — and the two would drift the first time the margin changed.
        """
        expected_s = self._guard_duration_s
        if expected_s <= 0:
            return False
        # The JOB against the allowance, not the job plus its headroom. The
        # earlier version compared `expected * DELIVERY_DURATION_MARGIN`, which
        # is the bound used to *tighten* a short job's timeout — a different
        # quantity. It fired on zones that finish comfortably: with a 4969 s job
        # and a 5400 s allowance it claimed the zone would stop short, and the
        # zone stops short of nothing. Found in the field the same afternoon the
        # warning became visible, which is the argument for making it visible.
        return expected_s > self._delivery_timeout

    @property
    def last_valve_test(self) -> dict | None:
        """The last supervised test result, or ``None`` if never tested."""
        return self._last_valve_test

    def record_valve_test(self, result: dict) -> None:
        """Keep the last supervised test result and publish it.

        Kept on the zone rather than in a store of its own because it is a fact
        *about this zone's plumbing*, and because the place people look for it is
        the zone they just tested. One result, not a history: the history is worth
        having (a delivery that decays over weeks is how clogged emitters
        announce themselves) and is deliberately out of this first delivery.
        """
        self._last_valve_test = result
        # `getattr`, not `self.hass`: the attribute does not exist until Home
        # Assistant adds the entity, and a test can complete during startup.
        # The same guard is used elsewhere in this file for the same reason.
        if getattr(self, "hass", None) is not None:
            self.async_write_ha_state()

    @property
    def active_warnings(self) -> list[str]:
        """Conditions that change how much water this zone gets, as codes.

        Codes and not sentences: the card owns the wording and the translation,
        the entity owns the truth. A rendered string here would need a second
        home for every language and would put user-facing copy in the layer
        that decides irrigation.

        Only conditions derivable from the current configuration and state
        belong here. The one-shot ``_warned`` flags elsewhere in this file mark
        things that happened *once* to a reading; those need their own carrier
        and are deliberately not folded in.
        """
        out: list[str] = []
        # Reachability first, and outside the mode gate: a valve that stopped
        # answering is not a configuration nuance, and it applies to every
        # delivery mode including monitoring. `None` means "not judged yet" and
        # must not read as trouble.
        if self.valve_reachable is False:
            out.append("valve_unreachable")
        if self._delivery_mode == DELIVERY_MODE_ESTIMATED_FLOW:
            return out
        if self.timeout_caps_duration:
            out.append("timeout_caps_duration")
        if self._flow_rate <= 0:
            # Same condition as the once-per-boot log below: no guard flow means
            # the expected duration is unknown and the timeout sits at its floor.
            out.append("no_guard_flow")
        return out

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
        """Attach the driver so FSM state can be exposed in attributes."""
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
        # 3. **Comparative** — the valve has been far quieter than its siblings.
        #    Weaker than a failed command and weaker than an unavailable entity,
        #    so it is consulted last; but it is the only one that speaks *before*
        #    an irrigation is due, which is the whole point of watching. A device
        #    off the mesh keeps publishing a stale, perfectly ordinary `off`, so
        #    the first two never fire for it (field: two valves off the mesh,
        #    2026-08-18).
        silent_vs_siblings = self._silence_verdict is True and not self._within_startup_grace
        return not silent_vs_siblings

    @property
    def is_irrigating(self) -> bool:
        """True if this zone is currently being irrigated."""
        return self._irrigating

    def set_silence_verdict(self, silent: bool | None) -> None:
        """Record what the fleet-silence watch concluded about this valve.

        ``None`` means the watch could not tell — one valve, or a fleet with no
        observed cadence yet — and must not be drawn as either state.
        """
        self._silence_verdict = silent

    def set_awaiting_valve(self, state: bool) -> None:
        """Mark that a valve command has been issued and has not yet resolved.

        Between pressing *Irrigate* and the valve confirming, nothing on the
        card changed: a zone whose valve took 48 s to give up looked exactly
        like a button that did nothing (field, 'Giardino Pino', 2026-08-18).
        The wait is the part worth showing — it is also the part that ends in
        a failure often enough to matter.
        """
        self._awaiting_valve = state

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

    def reset_deficit(
        self,
        source: str = "unknown",
        delivered_liters: float | None = None,
        duration_s: int | None = None,
    ) -> None:
        """Reset this zone's deficit to zero (called after irrigation).

        When ``delivered_liters`` is provided it is credited to the water
        counters as the actual volume delivered. This is required for
        flow-metered deliveries where ``_zone_deficit`` is depleted in real time
        during the cycle, so ``volume_liters`` would read ~0 by the time the
        cycle settles. When omitted (manual reset / mark-irrigated), the volume
        is derived from the current deficit via ``volume_liters``.
        """
        self._zone.mark_irrigated(
            source=source, at=datetime.now(), credited_liters=delivered_liters, duration_s=duration_s
        )

    @property
    def volume_liters(self) -> float:
        """Volume to irrigate this zone [L] — the zone's own demand."""
        return self._zone.water_demand_l

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
            "awaiting_valve": self._awaiting_valve,
        }
        if self._delivery_mode != DELIVERY_MODE_ESTIMATED_FLOW:
            # Only where the timeout is actually the closing criterion: in
            # estimated mode the valve opens for a computed duration and the
            # bound never bites, so flagging it would warn about a limit that
            # does not apply. `delivery_timeout_s` is published further down by
            # the block that already owned it — one home per attribute.
            attrs["timeout_caps_duration"] = self.timeout_caps_duration
            warnings = self.active_warnings
            if warnings:
                attrs["warnings"] = warnings

        if self._last_valve_test:
            # Prefixed and flat: the card and the report block read these, and a
            # nested dict in an attribute is awkward for both.
            for key, value in self._last_valve_test.items():
                if key == "zone_name":
                    continue
                attrs[f"valve_test_{key}"] = value

        if self._probe_vwc is not None:
            # Published, not used: the raw material for observing field capacity
            # and the soil's dynamics, and for spotting a delivery that moved no
            # water. The deficit above is the model's, and stays the model's.
            attrs["probe_water_content"] = round(self._probe_vwc, 3)
            attrs["probe_implied_deficit_mm"] = round(self._probe_implied_mm, 2)
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
            "awaiting_valve": self._zone_sensor._awaiting_valve,
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
        # Also on session close, so the figure is right the moment it changes
        # rather than at the next ET tick.
        zone_sensor.register_session_listener(self._on_session_update)

    def _on_update(self, dt_h: float, et_h: float, rain: float) -> None:
        if getattr(self, "hass", None):
            self.async_write_ha_state()

    def _on_session_update(self) -> None:
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
    """How the zone was last irrigated.

    An ``enum`` device class with a translation key, rather than a plain string:
    the raw value is an internal token (``mark_irrigated``, ``scheduled``) and it
    was reaching the user verbatim. Translating it here means Home Assistant
    renders it everywhere — card, history, logbook, voice — instead of only in
    the places we remembered to handle.

    Every value the controller can write must be listed in ``options`` or Home
    Assistant rejects the state, so this list is the authoritative enumeration:
    the four ``Trigger`` members, plus ``button`` for the card's actions,
    ``automatic`` as the controller's fallback, and the two ``reset_deficit``
    reasons that also surface here.
    """

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_translation_key = "last_source"
    _attr_options: ClassVar[list[str]] = [
        "scheduled",
        "reactive",
        "manual",
        "service",
        "button",
        "automatic",
        "mark_irrigated",
        "service_reset",
    ]

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
        """The raw token, or ``None`` when it is one we never declared.

        Returning an undeclared value would make Home Assistant log the state as
        invalid on every write; a gap is quieter than a broken entity, and the
        log line below says which token is missing from ``options``.
        """
        value = self._zone_sensor._last_irrigation_source
        if value is None or value in self._attr_options:
            return value
        _LOGGER.warning(
            "Zone '%s': irrigation source '%s' is not a declared option — add it to ZoneLastSourceSensor",
            self._zone_sensor.zone_name,
            value,
        )
        return None


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
            "Design flow rate",
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


class ZoneMeasuredFlowSensor(_ZoneTextSensor):
    """What the last supervised test actually measured, next to what was declared.

    Two reasons this exists rather than living only in the log. The first is
    immediate: without it, pressing *Save measured flow rate* is a blind act —
    you would be writing a number you never saw. The second is the one that
    lasts: with a `measurement` state class Home Assistant keeps the series, and
    a series is worth more than any single reading here. Flow depends on mains
    pressure, so it depends on the hour and on who else is drawing water; one
    test tells you the flow *at that moment*, while a run of them tells you the
    zone's flow and, at steady pressure, a slow decline is emitters clogging.

    Sits beside the *Design flow rate* on purpose. Seeing 205 next to 360 is
    the whole argument for the feature, and no wording explains it as well.
    """

    _attr_device_class = SensorDeviceClass.VOLUME_FLOW_RATE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, zone_sensor, device_info=None):
        super().__init__(
            zone_sensor,
            "Measured flow rate",
            "mdi:gauge-full",
            "measured_flow_zone",
            device_info,
            diagnostic=True,
        )

    @property
    def _is_imperial(self) -> bool:
        units = getattr(getattr(self, "hass", None), "config", None)
        units = getattr(units, "units", None)
        return getattr(units, "volume_unit", None) == UnitOfVolume.GALLONS

    @property
    def native_unit_of_measurement(self) -> str:
        if self._is_imperial:
            return UnitOfVolumeFlowRate.GALLONS_PER_HOUR
        return UnitOfVolumeFlowRate.LITERS_PER_HOUR

    @property
    def native_value(self) -> float | None:
        """The median of what this zone has really delivered — `None` until it has.

        Reads the rolling history rather than the last test on purpose. Flow
        follows mains pressure, so a single run — supervised or not — reports
        the flow *at that moment*; the median over sessions reports the zone.
        A supervised test is not excluded, it is simply the first sample in the
        same series.
        """
        operator = getattr(self._zone_sensor, "_operator", None)
        lpm = getattr(operator, "measured_flow_lpm", None) if operator else None
        if not lpm:
            return None
        return round(lpm * (LPM_TO_GPH if self._is_imperial else LPM_TO_LPH), 1)

    @property
    def extra_state_attributes(self) -> dict:
        """The gap against the design rate, plus what the last test could see.

        `vs_design_pct` is the diagnosis the pair exists for: the design rate is
        the sum of the emitters' rated output, so a zone delivering well under it
        is losing water to pressure or to clogged emitters. One number alone says
        nothing — 205 L/h is only alarming next to a design figure of 360.

        `smallest_step` with `updates` is the limit of detection: a meter that
        changed once in a minute cannot describe a run that short, however
        precise its unit looks.
        """
        design_lpm = self._zone_sensor._flow_rate
        attrs: dict = {
            "design_flow_lph": round(design_lpm * LPM_TO_LPH, 1) if design_lpm else None,
        }
        operator = getattr(self._zone_sensor, "_operator", None)
        if operator is not None:
            history = operator.session_flow_diagnostics
            attrs["sample_count"] = history.get("sample_count")
            attrs["min_samples_required"] = history.get("min_samples_required")
            attrs["min_lph"] = round(v * LPM_TO_LPH, 1) if (v := history.get("min_lpm")) else None
            attrs["max_lph"] = round(v * LPM_TO_LPH, 1) if (v := history.get("max_lpm")) else None
            measured = operator.measured_flow_lpm
            if measured and design_lpm:
                attrs["vs_design_pct"] = round(measured / design_lpm * 100.0, 1)

        test = self._zone_sensor.last_valve_test
        if test:
            attrs.update(
                {
                    "last_test_volume_l": test.get("volume_l"),
                    "last_test_duration_s": test.get("duration_s"),
                    "smallest_step": test.get("smallest_step"),
                    "updates": test.get("updates"),
                    "meter_entity": test.get("meter_entity"),
                    "notes": test.get("notes"),
                }
            )
        return attrs


class ZoneMeterResolutionSensor(_ZoneTextSensor):
    """The smallest increment this zone's water meter has ever reported.

    Its limit of detection, learned by watching deliveries — no test required.
    Exposed rather than kept internal because it is the number that decides how
    long the integration must wait before a still counter means anything, and a
    control whose basis is invisible cannot be argued with when it misfires.

    With a series behind it, it also reports on the meter itself: a resolution
    that suddenly coarsens is a counter that has started skipping.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, zone_sensor, device_info=None):
        super().__init__(
            zone_sensor,
            "Water meter resolution",
            "mdi:ruler",
            "meter_resolution_zone",
            device_info,
            diagnostic=True,
        )

    @property
    def native_unit_of_measurement(self) -> str:
        return UnitOfVolume.GALLONS if self._is_imperial else UnitOfVolume.LITERS

    @property
    def _is_imperial(self) -> bool:
        units = getattr(getattr(self, "hass", None), "config", None)
        units = getattr(units, "units", None)
        return getattr(units, "volume_unit", None) == UnitOfVolume.GALLONS

    @property
    def native_value(self) -> float | None:
        """`None` until the counter has been seen to move at least once."""
        operator = getattr(self._zone_sensor, "_operator", None)
        resolution = getattr(operator, "meter_resolution_l", None) if operator else None
        if not resolution:
            return None
        return round(resolution * LITERS_TO_GALLONS if self._is_imperial else resolution, 3)

    @property
    def extra_state_attributes(self) -> dict:
        """What the resolution costs in waiting, which is why it is published."""
        operator = getattr(self._zone_sensor, "_operator", None)
        if operator is None:
            return {}
        attrs: dict = {}
        if (first_tick := getattr(operator, "time_to_first_tick_s", lambda: None)()) is not None:
            attrs["time_to_first_tick_s"] = round(first_tick, 1)
            # The shortest supervised test that can see anything at all: one
            # increment proves the valve opened, several are needed to measure.
            attrs["shortest_useful_test_min"] = max(1, math.ceil(first_tick * 5 / 60))
        return attrs


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


class WaterBalanceMethodSensor(SensorEntity):
    """Which water-balance method is running, as an entity rather than an attribute.

    An attribute would technically answer the question, but only for someone who
    already knows to look for it. This is the one fact a user needs in order to
    read every other number the integration publishes: a deficit means something
    different depending on whether it was estimated from temperature, computed
    from a full weather station, or measured by a probe.

    It also makes two invisible behaviours visible. ``auto`` has to resolve to
    something, and a stored choice whose sensors have gone degrades to what the
    site can still support — in both cases what runs is not what is written in
    the configuration, and until now nothing said so.

    Diagnostic by category: it describes the installation, not the garden.
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:function-variant"
    _attr_should_poll = False
    # An enum, not free text: the state is an identifier, and the frontend can
    # only translate it — into "Simple (temperature only)" rather than
    # "et_simple" — if the sensor declares the set it draws from.
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_translation_key = "water_balance_method"

    def __init__(self, hub: DrynessIndexSensor, device_info: DeviceInfo | None = None) -> None:
        """Mirror the method of ``hub``, on the hub's own device."""
        from homeassistant.const import EntityCategory

        self._hub = hub
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_unique_id = "water_balance_method"
        # Only the methods that can run: an option the sensor can never report
        # would be a promise in the dropdown of a state that never arrives.
        self._attr_options = [m.method_id for m in MODEL_CATALOGUE if m.input_type in RUNNABLE_INPUTS]
        if device_info:
            self._attr_device_info = device_info

    async def async_added_to_hass(self) -> None:
        """Follow the hub, or the attributes freeze at whatever startup found.

        This entity does not poll and has nothing of its own to react to, so
        without a subscription it writes once — publishing an empty set of model
        inputs forever, because at startup no reading has been built yet. The hub
        writes its own state on every tick; this rides on that.
        """
        self.async_on_remove(self._hub.register_input_listener(self._republish))
        self.async_write_ha_state()

    @callback
    def _republish(self) -> None:
        """Republish: the method rarely changes, the inputs change every tick."""
        self.async_write_ha_state()

    @property
    def native_value(self) -> str:
        """The identifier of the method actually running."""
        return self._hub.active_method

    @property
    def extra_state_attributes(self) -> dict:
        """What was asked for, and what the site can feed — the *why* of the answer."""
        return {
            "configured": self._hub._configured_method,
            "reason": self._hub._method_reason,
            "reference_frame": self._hub.reference_frame.value,
            "declared_sensors": sorted(k.value for k in self._hub.environment.declared_sensors),
            "et_rate_mm_h": round(self._hub.current_et_rate, 4),
            # Everything below is the last reading the model was given, split
            # into measured and derived. It is the only place the two can be
            # compared, which is what makes the estimate checkable instead of
            # merely believable.
            **self._hub._last_inputs,
        }


class ModelInputSensor(SensorEntity):
    """One quantity the model derived, as an entity rather than an attribute.

    Attributes answer "what is it now"; entities also answer "what has it been
    doing", because Home Assistant records them and draws them. For a derived
    value that is the whole point: the way to know whether a daily radiation is
    right is to watch it over a week and see it follow the weather, which an
    attribute cannot show.

    Diagnostic by category, and only created for the model actually running —
    an entity that is permanently unknown teaches the user to ignore the group
    it lives in.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        hub: DrynessIndexSensor,
        key: str,
        name: str,
        unit: str,
        icon: str,
        precision: int = 2,
        device_info: DeviceInfo | None = None,
    ) -> None:
        """Publish ``key`` from the hub's last model inputs."""
        from homeassistant.const import EntityCategory

        self._hub = hub
        self._key = key
        self._attr_name = name
        self._attr_unique_id = f"model_input_{key}"
        self._attr_native_unit_of_measurement = unit
        self._attr_icon = icon
        self._attr_suggested_display_precision = precision
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        if device_info:
            self._attr_device_info = device_info

    async def async_added_to_hass(self) -> None:
        """Follow the hub's inputs directly, not its state.

        And republish once on the way in: entities are added concurrently, so
        whether the hub has already computed by the time this runs is a race.
        Losing it left the entity reading unknown until the next tick, which is
        the failure this whole path exists to remove.
        """
        self.async_on_remove(self._hub.register_input_listener(self._republish))
        self.async_write_ha_state()

    @callback
    def _republish(self) -> None:
        self.async_write_ha_state()

    @property
    def native_value(self) -> float | None:
        """The value, or ``None`` while the model has not produced one."""
        value = self._hub._last_inputs.get(self._key)
        return value if isinstance(value, (int, float)) else None


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
