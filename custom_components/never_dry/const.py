"""Constants for the NeverDry integration."""

DOMAIN = "never_dry"
CONFIG_VERSION = 3

# ── Sensor inputs ─────────────────────────────────────────
CONF_TEMP_SENSOR = "temperature_sensor"
CONF_RAIN_SENSOR = "rain_sensor"
CONF_VWC_SENSOR = "vwc_sensor"
CONF_RAIN_SENSOR_TYPE = "rain_sensor_type"

# Rain sensor types
RAIN_TYPE_EVENT = "event"  # mm per event (tipping bucket pulse)
RAIN_TYPE_DAILY_TOTAL = "daily_total"  # cumulative mm since midnight

# Accumulator rain sensors (daily_total, rolling 24h, lifetime cumulative)
# credit only positive increments between readings: a decrease is a reset,
# a window age-out, or a glitch — never precipitation. This replaced the
# earlier near-zero reset heuristic, which both wiped deficits at 05:00 with
# clear skies (rolling sensor, 2026-07-18) and dropped legitimate overnight
# rain on true daily totals (#123).

# ── ET model parameters ──────────────────────────────────
CONF_ALPHA = "alpha"
CONF_T_BASE = "t_base"
CONF_D_MAX = "d_max"
CONF_FIELD_CAPACITY = "field_capacity"
CONF_ROOT_DEPTH = "root_depth_m"

# ── Zone parameters ──────────────────────────────────────
CONF_ZONES = "zones"
CONF_ZONE_NAME = "name"
CONF_ZONE_VALVE = "valve"
CONF_ZONE_AREA = "area_m2"
CONF_ZONE_EFFICIENCY = "efficiency"
CONF_ZONE_FLOW_RATE = "flow_rate_lpm"
CONF_ZONE_THRESHOLD = "threshold"
CONF_ZONE_SYSTEM_TYPE = "system_type"
CONF_ZONE_PLANT_FAMILY = "plant_family"
CONF_ZONE_KC = "kc"
CONF_ZONE_EXPOSURE = "exposure"
CONF_ZONE_MICROCLIMATE_FACTOR = "microclimate_factor"
CONF_ZONE_DELIVERY_MODE = "delivery_mode"
CONF_ZONE_VOLUME_ENTITY = "volume_entity"
CONF_ZONE_FLOW_METER_SENSOR = "flow_meter_sensor"
CONF_ZONE_DELIVERY_TIMEOUT = "delivery_timeout"
CONF_ZONE_BATTERY_SENSOR = "battery_sensor"
CONF_ZONE_IRRIGATION_MODE = "irrigation_mode"
CONF_ZONE_IRRIGATION_TIME = "irrigation_time"
CONF_ZONE_HW_MAX_DURATION_TOPIC = "hw_max_duration_topic"
CONF_ZONE_HW_MAX_DURATION_PAYLOAD = "hw_max_duration_payload"

# Irrigation modes
IRRIGATION_MODE_MANUAL = "manual"
IRRIGATION_MODE_REACTIVE = "reactive"
IRRIGATION_MODE_SCHEDULED = "scheduled"
DEFAULT_IRRIGATION_MODE = IRRIGATION_MODE_MANUAL

# ── Controller parameters ────────────────────────────────
CONF_INTER_ZONE_DELAY = "inter_zone_delay"

# ── Irrigation system types ──────────────────────────────
SYSTEM_TYPE_DRIP = "drip"
SYSTEM_TYPE_MICRO_SPRINKLER = "micro_sprinkler"
SYSTEM_TYPE_SPRINKLER = "sprinkler"
SYSTEM_TYPE_MANUAL = "manual"
SYSTEM_TYPE_CUSTOM = "custom"

# ── The preset/override contract ─────────────────────────
# Three zone settings work the same way: a dropdown of presets, plus a box
# for a value the presets do not cover. THE DROPDOWN DECIDES. A ``None``
# preset value marks the "custom" entry, and only then is the box read.
# Picking a preset while the box holds a value is not an error — the value
# is ignored and the form says so — but picking custom without one is.
#
#   system type   -> default_efficiency    (box: efficiency)
#   plant family  -> kc_seasonal           (box: manual Kc)
#   exposure      -> factor                (box: microclimate factor)
#
# Within a pair it is always one or the other, never both. Across pairs the
# results compose: Kc = base x kmc.
SYSTEM_TYPES = {
    SYSTEM_TYPE_DRIP: {"label": "Drip irrigation", "default_efficiency": 0.92},
    SYSTEM_TYPE_MICRO_SPRINKLER: {"label": "Micro-sprinklers", "default_efficiency": 0.80},
    SYSTEM_TYPE_SPRINKLER: {"label": "Pop-up sprinklers", "default_efficiency": 0.68},
    SYSTEM_TYPE_MANUAL: {"label": "Manual / hose", "default_efficiency": 0.55},
    SYSTEM_TYPE_CUSTOM: {"label": "Custom (set the efficiency)", "default_efficiency": None},
}

# ── Plant families (seasonal Kc profiles) ───────────────
# Tuple order: (winter, spring, summer, autumn) — northern hemisphere
# Anchor days: 15 (mid-Jan), 105 (mid-Apr), 196 (mid-Jul), 288 (mid-Oct)
# Southern hemisphere: day_of_year shifted by 182 days automatically.
PLANT_FAMILIES = {
    "lawn": {"label": "Lawn / Turf grass", "kc_seasonal": (0.45, 0.85, 1.00, 0.70)},
    "vegetables": {"label": "Vegetables (seasonal)", "kc_seasonal": (0.30, 0.70, 1.10, 0.50)},
    "fruit_trees": {"label": "Fruit trees (deciduous)", "kc_seasonal": (0.35, 0.70, 0.95, 0.55)},
    "ornamental_shrubs": {"label": "Ornamental shrubs", "kc_seasonal": (0.40, 0.65, 0.80, 0.55)},
    "herbs": {"label": "Herbs (Mediterranean)", "kc_seasonal": (0.30, 0.55, 0.70, 0.40)},
    "citrus": {"label": "Citrus / Evergreen fruit", "kc_seasonal": (0.60, 0.65, 0.70, 0.65)},
    "roses": {"label": "Roses", "kc_seasonal": (0.35, 0.75, 0.95, 0.55)},
    "succulents": {"label": "Succulents / Cacti", "kc_seasonal": (0.15, 0.25, 0.35, 0.20)},
    "native_ground_cover": {"label": "Native ground cover", "kc_seasonal": (0.25, 0.45, 0.55, 0.35)},
    "mixed_garden": {"label": "Mixed garden (default)", "kc_seasonal": (0.40, 0.70, 0.90, 0.55)},
    # No curve: the zone follows the manual Kc instead. See the preset/override
    # contract above.
    "custom": {"label": "Custom (set the Kc)", "kc_seasonal": None},
}

PLANT_FAMILY_CUSTOM = "custom"

KC_ANCHOR_DAYS = (15, 105, 196, 288)

# ── Site exposure (microclimate factor, kmc) ─────────────
# Landscape coefficient method: KL = ks * kd * kmc (Costello et al. 2000).
# The plant family is ks, this is kmc — multiplied onto the Kc so a shaded
# zone keeps its seasonal curve instead of being frozen by a constant Kc
# override (#146). Above 1.0 is intentional: paving and walls push a zone
# past reference ET.
EXPOSURE_DEEP_SHADE = "deep_shade"
EXPOSURE_MORNING_SUN = "morning_sun"
EXPOSURE_AFTERNOON_SUN = "afternoon_sun"
EXPOSURE_FULL_SUN = "full_sun"
EXPOSURE_WINDY = "windy"
EXPOSURE_REFLECTED_HEAT = "reflected_heat"
EXPOSURE_CUSTOM = "custom"

# ``factor: None`` marks the custom entry, per the preset/override contract
# above. ``label`` is developer-facing only, as in PLANT_FAMILIES: the
# dropdown text comes from selector.exposure in the translations.
EXPOSURES = {
    EXPOSURE_DEEP_SHADE: {"label": "Deep / all-day shade", "factor": 0.60},
    EXPOSURE_MORNING_SUN: {"label": "Morning sun, afternoon shade", "factor": 0.75},
    EXPOSURE_AFTERNOON_SUN: {"label": "Morning shade, afternoon sun", "factor": 0.85},
    EXPOSURE_FULL_SUN: {"label": "Full sun, open", "factor": 1.00},
    EXPOSURE_WINDY: {"label": "Windy / exposed", "factor": 1.15},
    EXPOSURE_REFLECTED_HEAT: {"label": "Reflected heat (paving, south-facing wall)", "factor": 1.20},
    EXPOSURE_CUSTOM: {"label": "Custom (set the factor)", "factor": None},
}

DEFAULT_EXPOSURE = EXPOSURE_FULL_SUN
DEFAULT_MICROCLIMATE_FACTOR = 1.0
# Floored above zero: at 0 the deficit never accrues, so every irrigation
# trigger goes silent with nothing in the UI to explain why.
MICROCLIMATE_FACTOR_MIN = 0.1
MICROCLIMATE_FACTOR_MAX = 1.5

# ── Valve delivery modes ────────────────────────────────
DELIVERY_MODE_VOLUME_PRESET = "volume_preset"
DELIVERY_MODE_FLOW_METER = "flow_meter"
DELIVERY_MODE_ESTIMATED_FLOW = "estimated_flow"
DEFAULT_DELIVERY_MODE = DELIVERY_MODE_ESTIMATED_FLOW

DELIVERY_MODES = {
    DELIVERY_MODE_ESTIMATED_FLOW: "Simple on/off (timer-based)",
    DELIVERY_MODE_FLOW_METER: "Valve with flow meter sensor",
    DELIVERY_MODE_VOLUME_PRESET: "Smart valve with volume dosing",
}

DEFAULT_DELIVERY_TIMEOUT_S = 3600  # 1 hour safety timeout — the ceiling, not the job
FLOW_METER_POLL_INTERVAL_S = 2

# How far past the expected job duration a delivery may run before the safety
# timeout cuts it. The job duration comes from the *declared* guard flow rate,
# which is an approximation — real flow moves with pressure, emitter fouling and
# water temperature — so the margin has to absorb an honest shortfall without
# cutting a healthy run short. 2.0 tolerates a real flow half the declared one.
# Tighten this only once the flow rate is measured rather than declared.
DELIVERY_DURATION_MARGIN = 2.0

# Spacing between the three safety layers. Each one exists to catch the failure
# of the one before it — the delivery loop stops a normal run, the watchdog
# stops a loop that is stuck or gone, the on-device timer stops a valve when
# Home Assistant itself is gone — so each must fire *after* the one it protects,
# or it steals the ending instead of catching a failure. A quarter more each
# step: delivery bound → x1.25 watchdog → x1.25 hardware timer.
SAFETY_LAYER_SPREAD = 1.25

# ── Services ─────────────────────────────────────────────
SERVICE_RESET = "reset"
SERVICE_RESET_YEARLY_RAIN = "reset_yearly_rain"
SERVICE_RESET_YEARLY_WATER = "reset_yearly_water"
SERVICE_IRRIGATE_ZONE = "irrigate_zone"
SERVICE_IRRIGATE_ALL = "irrigate_all"
SERVICE_STOP = "stop"
SERVICE_STOP_ZONE = "stop_zone"
SERVICE_MARK_IRRIGATED = "mark_irrigated"
SERVICE_RESET_VALVE = "reset_valve"
SERVICE_SET_DEFICIT = "set_deficit"

ATTR_ZONE_NAME = "zone_name"
ATTR_DEFICIT_MM = "deficit_mm"

# ── Events ──────────────────────────────────────────────
EVENT_IRRIGATION_COMPLETE = "never_dry_irrigation_complete"

# ── Defaults ─────────────────────────────────────────────
DEFAULT_ALPHA = 0.22
DEFAULT_T_BASE = 9.0
DEFAULT_D_MAX = 100.0
DEFAULT_EFFICIENCY = 0.85
DEFAULT_THRESHOLD = 20.0
DEFAULT_FIELD_CAPACITY = 0.30
DEFAULT_ROOT_DEPTH = 0.30
DEFAULT_INTER_ZONE_DELAY = 30
DEFAULT_KC = 1.0
DEFAULT_RAIN_SENSOR_TYPE = RAIN_TYPE_EVENT
DEFAULT_BACKFILL_DAYS = 90
DEFAULT_IRRIGATION_TIME = "06:00"  # default daily irrigation check time
DEFAULT_BATTERY_LOW_THRESHOLD = 15  # percent
ANOMALY_DEFICIT_MULTIPLIER = 2  # alert when deficit > threshold * this
CONF_BACKFILL_DAYS = "backfill_days"

# ── ET sensor robustness buffer ──────────────────────────
ET_BUFFER_SIZE = 10  # rolling window of valid readings
ET_BUFFER_MIN_READINGS = 1  # minimum readings before median is trusted
ET_TEMP_VALID_RANGE = (-50.0, 70.0)  # °C physical bounds

# ── Valve reachability ───────────────────────────────────
# How long after setup a valve that has never been seen is reported as
# "unknown" rather than "not responding". Zigbee entities are not available
# for the first minute or two after a restart, and every options-flow save is
# a reload: without this, three zones out of four raise a warning on every
# restart, which is the surest way to teach the user to ignore it.
# The window closes early the moment the valve is first seen alive — it is a
# grace for the absence of evidence, not a blanket delay.
VALVE_STARTUP_GRACE_S = 300  # 5 minutes

# ── Runtime safety limits ────────────────────────────────
MAX_ZONES = 50
MAX_ZONE_NAME_LENGTH = 64
MIN_SERVICE_INTERVAL_S = 10  # minimum seconds between service calls

# ── Zone config plausibility guards (soft-confirm, not blocking) ──
# Values outside these ranges are almost always unit mistakes (e.g. flow
# entered in L/min instead of L/h). The config flow asks for confirmation
# instead of rejecting, since tiny planter zones and high-flow sprinkler
# manifolds do legitimately exist.
UNUSUAL_AREA_MIN_M2 = 5.0
UNUSUAL_FLOW_MIN_LPM = 10.0 / 60.0  # 10 L/h
UNUSUAL_FLOW_MAX_LPM = 30.0  # 1800 L/h — same bound as the runtime warning in sensor.py
