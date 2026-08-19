"""Config flow for the NeverDry integration.

Provides a multi-step UI setup:
  1. Select temperature and rain sensors, ET model parameters
  2. Add irrigation zones (repeatable)
  3. Options flow to edit parameters and add/remove zones later
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import selector

from . import zone_device_identifier
from .const import (
    CONF_ALPHA,
    CONF_D_MAX,
    CONF_ET_METHOD,
    CONF_HUMIDITY_SENSOR,
    CONF_NET_RADIATION_SENSOR,
    CONF_RAIN_SENSOR,
    CONF_RAIN_SENSOR_TYPE,
    CONF_T_BASE,
    CONF_TEMP_MAX_SENSOR,
    CONF_TEMP_MIN_SENSOR,
    CONF_TEMP_SENSOR,
    CONF_VWC_SENSOR,
    CONF_WIND_SPEED_SENSOR,
    CONF_ZONE_AREA,
    CONF_ZONE_DELIVERY_MODE,
    CONF_ZONE_DELIVERY_TIMEOUT,
    CONF_ZONE_EFFICIENCY,
    CONF_ZONE_EXPOSURE,
    CONF_ZONE_FLOW_METER_SENSOR,
    CONF_ZONE_FLOW_RATE,
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
    CONFIG_VERSION,
    DEFAULT_ALPHA,
    DEFAULT_D_MAX,
    DEFAULT_DELIVERY_MODE,
    DEFAULT_DELIVERY_TIMEOUT_S,
    DEFAULT_ET_METHOD,
    DEFAULT_EXPOSURE,
    DEFAULT_IRRIGATION_MODE,
    DEFAULT_IRRIGATION_TIME,
    DEFAULT_RAIN_SENSOR_TYPE,
    DEFAULT_T_BASE,
    DEFAULT_THRESHOLD,
    DELIVERY_MODE_ESTIMATED_FLOW,
    DELIVERY_MODE_FLOW_METER,
    DELIVERY_MODE_VOLUME_PRESET,
    DOMAIN,
    ET_METHOD_AUTO,
    ET_METHOD_OPTIONS,
    EXPOSURES,
    IRRIGATION_MODE_MANUAL,
    IRRIGATION_MODE_REACTIVE,
    IRRIGATION_MODE_SCHEDULED,
    MAX_ZONE_NAME_LENGTH,
    MAX_ZONES,
    MICROCLIMATE_FACTOR_MAX,
    MICROCLIMATE_FACTOR_MIN,
    PLANT_FAMILIES,
    RAIN_TYPE_DAILY_TOTAL,
    RAIN_TYPE_EVENT,
    SYSTEM_TYPE_CUSTOM,
    SYSTEM_TYPE_DRIP,
    SYSTEM_TYPE_MANUAL,
    SYSTEM_TYPE_MICRO_SPRINKLER,
    SYSTEM_TYPE_SPRINKLER,
    SYSTEM_TYPES,
    UNUSUAL_AREA_MIN_M2,
    UNUSUAL_FLOW_MAX_LPM,
    UNUSUAL_FLOW_MIN_LPM,
)
from .environment import Environment
from .unit_convert import (
    LPM_TO_GPH,
    LPM_TO_GPM,
    LPM_TO_LPH,
    M2_TO_FT2,
    MM_TO_IN,
    c_to_f,
    sensors_input_to_metric,
    zone_input_to_metric,
)
from .water_balance_model import RUNNABLE_INPUTS, model_by_id

_LOGGER = logging.getLogger(__name__)

# Unit-conversion helpers live in a HA-free module so they stay unit-testable.
# Aliased here to keep the call sites below terse.
_MM_TO_IN = MM_TO_IN
_M2_TO_FT2 = M2_TO_FT2
_LPM_TO_GPM = LPM_TO_GPM
_LPM_TO_LPH = LPM_TO_LPH
_LPM_TO_GPH = LPM_TO_GPH
_c_to_f = c_to_f
_sensors_input_to_metric = sensors_input_to_metric
_zone_input_to_metric = zone_input_to_metric


def _is_imperial(hass) -> bool:
    from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM

    return hass.config.units is US_CUSTOMARY_SYSTEM


#: The optional bindings that unlock the richer ET methods, with the device
#: class that makes the entity picker useful. Declared once and rendered into
#: both forms: setup and options must offer the same vocabulary, or a site
#: could declare a sensor it can never edit.
#:
#: The daily temperature extremes are **deliberately absent**. NeverDry already
#: reads a thermometer continuously, so asking for its own max and min is asking
#: the user to build with helpers something the integration can observe — and it
#: invites the worst version of the mistake: the same entity in both boxes gives
#: a zero diurnal range, which Hargreaves turns into an evapotranspiration of
#: exactly zero. A deficit that never grows is a garden that is never watered,
#: and nothing anywhere says so.
#:
#: Radiation is asked for as **solar** radiation — what a pyranometer reports,
#: and what a consumer weather station exposes. The *net* radiation the FAO-56
#: equation uses is computed from it, per eq. 38-40; asking for it directly
#: would be asking for an instrument almost nobody owns.
_EXTRA_SENSORS: tuple[tuple[str, str | None], ...] = (
    (CONF_HUMIDITY_SENSOR, "humidity"),
    (CONF_WIND_SPEED_SENSOR, "wind_speed"),
    (CONF_NET_RADIATION_SENSOR, "irradiance"),
)


def _extra_sensor_fields(current: dict | None = None) -> dict:
    """Entity pickers for the optional inputs, pre-filled when editing."""
    fields = {}
    for key, device_class in _EXTRA_SENSORS:
        marker = (
            vol.Optional(key, description={"suggested_value": current.get(key)})
            if current is not None
            else vol.Optional(key)
        )
        config = selector.EntitySelectorConfig(domain="sensor")
        if device_class:
            config = selector.EntitySelectorConfig(domain="sensor", device_class=device_class)
        fields[marker] = selector.EntitySelector(config)
    return fields


def _et_method_field(current: dict | None = None) -> dict:
    """The method dropdown, offering every method rather than only the usable ones.

    Deliberately not filtered to what the site currently satisfies. The form does
    not react to what you type, so a list narrowed at render time would go stale
    the moment a sensor is picked in the same submission — and a user who cannot
    see Penman-Monteith has no way to learn which sensor unlocks it. The choice
    is validated on submit instead, and the error names the missing sensors.
    """
    stored = (current or {}).get(CONF_ET_METHOD, DEFAULT_ET_METHOD)
    return {
        vol.Optional(CONF_ET_METHOD, default=stored): selector.SelectSelector(
            selector.SelectSelectorConfig(
                # A list, not the tuple in const: Home Assistant validates this
                # field with voluptuous, which refuses a tuple outright.
                options=list(ET_METHOD_OPTIONS),
                translation_key="et_method",
                mode="dropdown",
            )
        )
    }


def _et_method_error(user_input: dict) -> str | None:
    """Reject a method the declared sensors cannot support, naming what is missing.

    The same rule the integration runs on decides the form's answer: an
    :class:`~.environment.Environment` is built from what was just submitted and
    asked whether it satisfies the model. Duplicating the check in a form-shaped
    variant is how the two would drift, and the drift would only show up as a
    model silently degrading after setup.

    ``auto`` is always valid: it *is* the promise to pick what the sensors allow.
    """
    method = user_input.get(CONF_ET_METHOD, DEFAULT_ET_METHOD)
    if method == ET_METHOD_AUTO:
        return None
    model = model_by_id(method)
    if model is None:
        return "et_method_unknown"
    if model.input_type not in RUNNABLE_INPUTS:
        # Written and tested, but nothing builds its input yet. It is not in the
        # dropdown either; this is the second lock, for an entry edited by hand
        # or restored from a version where the option existed.
        return "et_method_unknown"
    env = Environment(
        temperature_sensor=user_input.get(CONF_TEMP_SENSOR) or "",
        rain_sensor=user_input.get(CONF_RAIN_SENSOR) or "",
        soil_moisture_sensor=user_input.get(CONF_VWC_SENSOR),
        humidity_sensor=user_input.get(CONF_HUMIDITY_SENSOR),
        wind_speed_sensor=user_input.get(CONF_WIND_SPEED_SENSOR),
        net_radiation_sensor=user_input.get(CONF_NET_RADIATION_SENSOR),
        temp_max_sensor=user_input.get(CONF_TEMP_MAX_SENSOR),
        temp_min_sensor=user_input.get(CONF_TEMP_MIN_SENSOR),
    )
    return None if env.satisfies(model.required_sensors) else "et_method_missing_sensors"


def _sensors_schema(is_imperial: bool) -> vol.Schema:
    """Sensors + ET parameters form for initial setup, unit-aware.

    **No soil probe here.** A probe measures one patch of soil, with one kind of
    planting above it and its own watering history, so a probe declared for the
    installation was answering a question nobody asked: it drove zones it knows
    nothing about. It is declared per zone now.

    The key survives in ``const`` and ``Environment`` on purpose: installations
    that already have one keep working until they say which zone it belongs to
    (the repair issue), and removing the field is what stops a *new* one being
    created.
    """
    t_unit = "°F" if is_imperial else "°C"
    d_unit = "in" if is_imperial else "mm"
    t_base_default = _c_to_f(DEFAULT_T_BASE) if is_imperial else DEFAULT_T_BASE
    d_max_default = round(DEFAULT_D_MAX * _MM_TO_IN, 2) if is_imperial else DEFAULT_D_MAX

    return vol.Schema(
        {
            vol.Required(CONF_TEMP_SENSOR): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor", device_class="temperature")
            ),
            vol.Required(CONF_RAIN_SENSOR): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
            vol.Optional(CONF_RAIN_SENSOR_TYPE, default=DEFAULT_RAIN_SENSOR_TYPE): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[RAIN_TYPE_EVENT, RAIN_TYPE_DAILY_TOTAL],
                    translation_key="rain_sensor_type",
                    mode="dropdown",
                )
            ),
            vol.Optional(CONF_D_MAX, default=d_max_default): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0.5 if is_imperial else 10.0,
                    max=20.0 if is_imperial else 500.0,
                    step=0.01 if is_imperial else 10.0,
                    mode="box",
                    unit_of_measurement=d_unit,
                )
            ),
            # The method comes immediately before alpha, and that adjacency is the
            # whole design: a form cannot show or hide a field in reaction to a
            # dropdown within one step, so the only way to express "this box
            # belongs to that choice" is to put it underneath it. The label says
            # which method uses it; the confirm step says so again if it is inert.
            **_et_method_field(),
            vol.Optional(CONF_ALPHA, default=DEFAULT_ALPHA): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0.05,
                    max=1.0,
                    step=0.01,
                    mode="box",
                    unit_of_measurement="mm/°C/day",
                )
            ),
            vol.Optional(CONF_T_BASE, default=t_base_default): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=23.0 if is_imperial else -5.0,
                    max=68.0 if is_imperial else 20.0,
                    step=1.0 if is_imperial else 0.5,
                    mode="box",
                    unit_of_measurement=t_unit,
                )
            ),
            **_extra_sensor_fields(),
        }
    )


def _model_params_schema(is_imperial: bool, current: dict) -> vol.Schema:
    """System sensors + ET parameters form for options flow, pre-filled from the entry."""
    t_unit = "°F" if is_imperial else "°C"
    d_unit = "in" if is_imperial else "mm"
    t_stored = current.get(CONF_T_BASE, DEFAULT_T_BASE)
    d_stored = current.get(CONF_D_MAX, DEFAULT_D_MAX)
    t_display = _c_to_f(t_stored) if is_imperial else t_stored
    d_display = round(d_stored * _MM_TO_IN, 2) if is_imperial else d_stored

    return vol.Schema(
        {
            vol.Required(
                CONF_TEMP_SENSOR,
                description={"suggested_value": current.get(CONF_TEMP_SENSOR)},
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor", device_class="temperature")),
            vol.Required(
                CONF_RAIN_SENSOR,
                description={"suggested_value": current.get(CONF_RAIN_SENSOR)},
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
            vol.Optional(
                CONF_RAIN_SENSOR_TYPE,
                default=current.get(CONF_RAIN_SENSOR_TYPE, DEFAULT_RAIN_SENSOR_TYPE),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[RAIN_TYPE_EVENT, RAIN_TYPE_DAILY_TOTAL],
                    translation_key="rain_sensor_type",
                    mode="dropdown",
                )
            ),
            vol.Optional(CONF_D_MAX, default=d_display): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0.5 if is_imperial else 10.0,
                    max=20.0 if is_imperial else 500.0,
                    step=0.01 if is_imperial else 10.0,
                    mode="box",
                    unit_of_measurement=d_unit,
                )
            ),
            **_et_method_field(current),
            vol.Optional(CONF_ALPHA, default=current.get(CONF_ALPHA, DEFAULT_ALPHA)): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0.05,
                    max=1.0,
                    step=0.01,
                    mode="box",
                    unit_of_measurement="mm/°C/day",
                )
            ),
            vol.Optional(CONF_T_BASE, default=t_display): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=23.0 if is_imperial else -5.0,
                    max=68.0 if is_imperial else 20.0,
                    step=1.0 if is_imperial else 0.5,
                    mode="box",
                    unit_of_measurement=t_unit,
                )
            ),
            **_extra_sensor_fields(current),
        }
    )


def _zone_schema_initial(is_imperial: bool) -> vol.Schema:
    """Zone form for initial setup and add_zone options flow (no pre-existing values)."""
    area_unit = "ft²" if is_imperial else "m²"
    flow_unit = "gal/h" if is_imperial else "L/h"
    depth_unit = "in" if is_imperial else "mm"
    threshold_default = round(DEFAULT_THRESHOLD * _MM_TO_IN, 2) if is_imperial else DEFAULT_THRESHOLD

    # Nothing is collapsed here: a zone being created has to be seen once in
    # full. The edit form collapses everything instead — there you already
    # know what you came to change.
    return vol.Schema(
        {
            vol.Required(CONF_ZONE_NAME): selector.TextSelector(),
            vol.Required(SECTION_GROUND): section(
                vol.Schema(
                    {
                        vol.Required(CONF_ZONE_AREA): selector.NumberSelector(
                            selector.NumberSelectorConfig(
                                min=1.0 if is_imperial else 0.1,
                                max=107000.0 if is_imperial else 10000.0,
                                step=1.0 if is_imperial else 0.1,
                                mode="box",
                                unit_of_measurement=area_unit,
                            )
                        ),
                        vol.Optional(CONF_ZONE_PLANT_FAMILY): selector.SelectSelector(
                            selector.SelectSelectorConfig(
                                options=list(PLANT_FAMILIES.keys()),
                                translation_key="plant_family",
                                mode="dropdown",
                            )
                        ),
                        vol.Optional(CONF_ZONE_KC): selector.NumberSelector(
                            selector.NumberSelectorConfig(min=0.1, max=2.0, step=0.01, mode="box")
                        ),
                        # The probe belongs here, in the zone, not to the
                        # installation: it measures one patch of soil with one
                        # planting above it and its own watering history, and a
                        # reading from somebody else's patch says nothing about
                        # this one. A zone that declares one stops estimating
                        # and starts measuring.
                        vol.Optional(CONF_ZONE_VWC_SENSOR): selector.EntitySelector(
                            selector.EntitySelectorConfig(domain="sensor", device_class="moisture")
                        ),
                        vol.Optional(CONF_ZONE_EXPOSURE, default=DEFAULT_EXPOSURE): selector.SelectSelector(
                            selector.SelectSelectorConfig(
                                options=list(EXPOSURES.keys()),
                                translation_key="exposure",
                                mode="dropdown",
                            )
                        ),
                        vol.Optional(CONF_ZONE_MICROCLIMATE_FACTOR): selector.NumberSelector(
                            selector.NumberSelectorConfig(
                                min=MICROCLIMATE_FACTOR_MIN,
                                max=MICROCLIMATE_FACTOR_MAX,
                                step=0.01,
                                mode="box",
                            )
                        ),
                    }
                ),
                {"collapsed": False},
            ),
            vol.Required(SECTION_VALVE): section(
                vol.Schema(
                    {
                        # Both domains: a `valve.*` entity is driven with valve
                        # services by the same adapter the driver uses, so the
                        # selector no longer has to lie about what is supported
                        # (GH #94). Widened only after the command path and the
                        # controller's own state checks went through that adapter
                        # — offering it earlier would have shown a valve that
                        # saved without error and never opened.
                        vol.Optional(CONF_ZONE_VALVE): selector.EntitySelector(
                            selector.EntitySelectorConfig(domain=["switch", "valve"])
                        ),
                        vol.Optional(CONF_ZONE_DELIVERY_MODE, default=DEFAULT_DELIVERY_MODE): selector.SelectSelector(
                            selector.SelectSelectorConfig(
                                options=[
                                    DELIVERY_MODE_ESTIMATED_FLOW,
                                    DELIVERY_MODE_FLOW_METER,
                                    DELIVERY_MODE_VOLUME_PRESET,
                                ],
                                translation_key="delivery_mode",
                                mode="dropdown",
                            )
                        ),
                        vol.Optional(CONF_ZONE_FLOW_RATE): selector.NumberSelector(
                            selector.NumberSelectorConfig(
                                min=2.0 if is_imperial else 1.0,
                                max=3200.0 if is_imperial else 12000.0,
                                step=0.5 if is_imperial else 1.0,
                                mode="box",
                                unit_of_measurement=flow_unit,
                            )
                        ),
                        vol.Optional(CONF_ZONE_FLOW_METER_SENSOR): selector.EntitySelector(
                            selector.EntitySelectorConfig(domain="sensor")
                        ),
                        vol.Optional(CONF_ZONE_VOLUME_ENTITY): selector.EntitySelector(
                            selector.EntitySelectorConfig(domain="number")
                        ),
                        vol.Required(CONF_ZONE_SYSTEM_TYPE): selector.SelectSelector(
                            selector.SelectSelectorConfig(
                                options=[
                                    SYSTEM_TYPE_DRIP,
                                    SYSTEM_TYPE_MICRO_SPRINKLER,
                                    SYSTEM_TYPE_SPRINKLER,
                                    SYSTEM_TYPE_MANUAL,
                                    SYSTEM_TYPE_CUSTOM,
                                ],
                                translation_key="system_type",
                                mode="dropdown",
                            )
                        ),
                        vol.Optional(CONF_ZONE_EFFICIENCY): selector.NumberSelector(
                            # box, not slider: a slider always submits a value, so an
                            # override set by accident could never be cleared again
                            # (GH #165). step 0.01 so every system-type default is
                            # reachable exactly — 0.92 for drip and 0.68 for pop-up
                            # sprinklers are not multiples of 0.05.
                            selector.NumberSelectorConfig(min=0.1, max=1.0, step=0.01, mode="box")
                        ),
                        vol.Optional(
                            CONF_ZONE_DELIVERY_TIMEOUT, default=DEFAULT_DELIVERY_TIMEOUT_S
                        ): selector.NumberSelector(
                            selector.NumberSelectorConfig(
                                min=60, max=7200, step=60, mode="box", unit_of_measurement="s"
                            )
                        ),
                    }
                ),
                {"collapsed": False},
            ),
            vol.Required(SECTION_SCHEDULING): section(
                vol.Schema(
                    {
                        vol.Optional(
                            CONF_ZONE_IRRIGATION_MODE, default=DEFAULT_IRRIGATION_MODE
                        ): selector.SelectSelector(
                            selector.SelectSelectorConfig(
                                options=[
                                    IRRIGATION_MODE_MANUAL,
                                    IRRIGATION_MODE_REACTIVE,
                                    IRRIGATION_MODE_SCHEDULED,
                                ],
                                translation_key="irrigation_mode",
                                mode="dropdown",
                            )
                        ),
                        vol.Optional(CONF_ZONE_IRRIGATION_TIME, default=DEFAULT_IRRIGATION_TIME): (
                            selector.TimeSelector()
                        ),
                        vol.Optional(CONF_ZONE_THRESHOLD, default=threshold_default): selector.NumberSelector(
                            selector.NumberSelectorConfig(
                                min=0.1 if is_imperial else 1.0,
                                max=4.0 if is_imperial else 100.0,
                                step=0.01 if is_imperial else 1.0,
                                mode="box",
                                unit_of_measurement=depth_unit,
                            )
                        ),
                    }
                ),
                {"collapsed": False},
            ),
        }
    )


def _unusual_zone_values(zone: dict, imperial: bool) -> list[str]:
    """Detect implausible zone values for the soft-confirm guard.

    Takes a zone dict in metric storage units (area m², flow L/min) and
    returns human-readable warning lines in the user's display units.
    An empty list means all values look plausible.
    """
    warnings: list[str] = []
    area = zone.get(CONF_ZONE_AREA)
    if area is not None and area < UNUSUAL_AREA_MIN_M2:
        if imperial:
            warnings.append(f"area {area * _M2_TO_FT2:.1f} ft² < {UNUSUAL_AREA_MIN_M2 * _M2_TO_FT2:.0f} ft²")
        else:
            warnings.append(f"area {area:.1f} m² < {UNUSUAL_AREA_MIN_M2:.0f} m²")
    flow = zone.get(CONF_ZONE_FLOW_RATE)
    mode = zone.get(CONF_ZONE_DELIVERY_MODE, DEFAULT_DELIVERY_MODE)
    if mode in (DELIVERY_MODE_FLOW_METER, DELIVERY_MODE_VOLUME_PRESET) and not flow:
        # Deprecation-style notice, not an error: gives existing installs a
        # smooth transition window before the guard flow becomes mandatory.
        warnings.append(
            "guard flow rate not set — used for expected duration and safety-timeout"
            " scaling; will become required in a future release (target: v1.0)"
        )
    if flow is not None and flow > 0:
        if imperial:
            shown, unit = flow * _LPM_TO_GPH, "gal/h"
            low, high = UNUSUAL_FLOW_MIN_LPM * _LPM_TO_GPH, UNUSUAL_FLOW_MAX_LPM * _LPM_TO_GPH
        else:
            shown, unit = flow * _LPM_TO_LPH, "L/h"
            low, high = UNUSUAL_FLOW_MIN_LPM * _LPM_TO_LPH, UNUSUAL_FLOW_MAX_LPM * _LPM_TO_LPH
        if flow < UNUSUAL_FLOW_MIN_LPM:
            warnings.append(f"flow rate {shown:.1f} {unit} < {low:.1f} {unit}")
        elif flow > UNUSUAL_FLOW_MAX_LPM:
            warnings.append(f"flow rate {shown:.0f} {unit} > {high:.0f} {unit}")
    return warnings


def _confirm_zone_schema() -> vol.Schema:
    """Checkbox form for the unusual-values confirmation step."""
    return vol.Schema({vol.Required("confirm", default=False): bool})


# The three preset/override pairs, as declared in const: the dropdown key,
# the table it draws from, the table field whose ``None`` marks the custom
# entry, the box key, the error shown when custom has no value, and the label
# used when telling the user their value is being ignored.
PRESET_OVERRIDE_PAIRS = (
    (
        CONF_ZONE_SYSTEM_TYPE,
        SYSTEM_TYPES,
        "default_efficiency",
        CONF_ZONE_EFFICIENCY,
        "efficiency_required",
        "Efficiency",
    ),
    (CONF_ZONE_PLANT_FAMILY, PLANT_FAMILIES, "kc_seasonal", CONF_ZONE_KC, "kc_required", "Kc"),
    (
        CONF_ZONE_EXPOSURE,
        EXPOSURES,
        "factor",
        CONF_ZONE_MICROCLIMATE_FACTOR,
        "microclimate_factor_required",
        "Microclimate factor",
    ),
)


# The zone form is 17 fields. Grouped, it reads as three questions, and the
# split is not cosmetic: it is the domain model's own — what is watered, what
# waters it, and when.
SECTION_GROUND = "ground_and_location"
SECTION_VALVE = "valve_and_pipe"
SECTION_SCHEDULING = "scheduling"
ZONE_SECTIONS = (SECTION_GROUND, SECTION_VALVE, SECTION_SCHEDULING)


def _flatten_sections(user_input: dict) -> dict:
    """Undo the nesting a sectioned form introduces.

    A section returns its fields under its own key, so ``efficiency`` arrives
    as ``user_input["valve_and_pipe"]["efficiency"]``. A zone is stored flat
    and every reader — sensors, controller, migrations — expects it flat, so
    the nesting stops here at the boundary rather than rippling through.

    Tolerant of already-flat input on purpose: the confirm step re-submits a
    zone that has been through here once, and it keeps every caller that
    builds a plain dict working.
    """
    if not any(isinstance(user_input.get(s), dict) for s in ZONE_SECTIONS):
        return user_input
    flat = {k: v for k, v in user_input.items() if k not in ZONE_SECTIONS}
    for name in ZONE_SECTIONS:
        block = user_input.get(name)
        if isinstance(block, dict):
            flat.update(block)
    return flat


# Which section each override box is rendered in, so an error can be pointed
# at a field the frontend is actually showing.
_SECTION_OF_FIELD = {
    CONF_ZONE_EFFICIENCY: SECTION_VALVE,
    CONF_ZONE_KC: SECTION_GROUND,
    CONF_ZONE_MICROCLIMATE_FACTOR: SECTION_GROUND,
}


def _preset_is_custom(table: dict, key: str | None, field: str) -> bool:
    """True when the selected entry defers to the box (``None`` in the table)."""
    return isinstance(key, str) and key in table and table[key][field] is None


def _override_errors(user_input: dict) -> dict[str, str]:
    """Reject 'custom' with an empty box — the one combination that means nothing.

    Custom says "the value is mine to give"; without a value the zone falls
    back to a neutral default and behaves as if nothing had been chosen, which
    is precisely the silent no-op the dropdown exists to prevent.
    """
    errors: dict[str, str] = {}
    for preset_key, table, field, override_key, error, _label in PRESET_OVERRIDE_PAIRS:
        if not _preset_is_custom(table, user_input.get(preset_key), field):
            continue
        if user_input.get(override_key) is not None:
            continue
        # Three keys for one problem, because the field lives inside a
        # collapsed section and the frontend has to be able to find it:
        #   - the bare name, which is what an unsectioned form matches;
        #   - the section-qualified name;
        #   - "base", which is rendered at the top of the form no matter what.
        # Without the last one the form simply refuses to close with nothing
        # on screen to say why, which is worse than the bug being fixed.
        errors[override_key] = error
        errors[f"{_SECTION_OF_FIELD[override_key]}.{override_key}"] = error
        errors.setdefault("base", error)
    return errors


def _ignored_override_warnings(zone: dict) -> list[str]:
    """Tell the user which values will not be used, and why.

    A preset is selected *and* the box holds a value: the preset wins, so the
    value is dead weight. Not an error — the number may be a leftover from an
    earlier attempt, and refusing to save over it would trap the user the way
    GH #165 did. Warned instead, once per pair, on the existing soft-confirm
    step.
    """
    warnings: list[str] = []
    for preset_key, table, field, override_key, _error, label in PRESET_OVERRIDE_PAIRS:
        value = zone.get(override_key)
        if value is None:
            continue
        selected = zone.get(preset_key)
        known = isinstance(selected, str) and selected in table
        if known and table[selected][field] is None:
            continue  # custom: the value is exactly what gets used
        # Nothing selected at all counts too. A zone with a Kc and no plant
        # family reads as "no family, here is my number", but the value is
        # only ever read behind Custom — so it would be dropped in silence,
        # which is the failure mode this whole rule exists to remove.
        chosen = f"'{table[selected]['label']}' is selected" if known else "nothing is selected"
        warnings.append(
            f"{label}: {chosen}, so your custom value {value} will not be used"
            f" — choose 'Custom' to apply it, or clear the field"
        )
    return warnings


def _coerce_delivery_mode(user_input: dict) -> dict:
    """Downgrade delivery_mode to estimated_flow when the required sensor is missing."""
    dm = user_input.get(CONF_ZONE_DELIVERY_MODE)
    if (dm == DELIVERY_MODE_FLOW_METER and not user_input.get(CONF_ZONE_FLOW_METER_SENSOR)) or (
        dm == DELIVERY_MODE_VOLUME_PRESET and not user_input.get(CONF_ZONE_VOLUME_ENTITY)
    ):
        user_input = dict(user_input)
        user_input[CONF_ZONE_DELIVERY_MODE] = DELIVERY_MODE_ESTIMATED_FLOW
    return user_input


class NeverDryConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for NeverDry."""

    VERSION = CONFIG_VERSION

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._data: dict[str, Any] = {}
        self._zones: list[dict[str, Any]] = []
        self._pending_zone: dict[str, Any] | None = None
        self._pending_warnings: list[str] = []

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> config_entries.ConfigFlowResult:
        """Step 1: Select sensors and ET model parameters."""
        imperial = _is_imperial(self.hass)
        errors: dict[str, str] = {}
        if user_input is not None:
            if error := _et_method_error(user_input):
                errors[CONF_ET_METHOD] = error
            else:
                self._data = _sensors_input_to_metric(user_input, imperial)
                return await self.async_step_zone()

        return self.async_show_form(
            step_id="user",
            data_schema=_sensors_schema(imperial),
            errors=errors,
        )

    async def async_step_zone(self, user_input: dict[str, Any] | None = None) -> config_entries.ConfigFlowResult:
        """Step 2: Add an irrigation zone."""
        imperial = _is_imperial(self.hass)
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input = _flatten_sections(user_input)
            name = user_input.get(CONF_ZONE_NAME, "")
            mode = user_input.get(CONF_ZONE_DELIVERY_MODE, DEFAULT_DELIVERY_MODE)
            if len(name) > MAX_ZONE_NAME_LENGTH:
                errors[CONF_ZONE_NAME] = "zone_name_too_long"
            elif len(self._zones) >= MAX_ZONES:
                errors["base"] = "too_many_zones"
            elif mode == DELIVERY_MODE_ESTIMATED_FLOW and not user_input.get(CONF_ZONE_FLOW_RATE):
                errors[CONF_ZONE_FLOW_RATE] = "flow_rate_required"
            elif mode == DELIVERY_MODE_FLOW_METER and not user_input.get(CONF_ZONE_FLOW_METER_SENSOR):
                errors[CONF_ZONE_FLOW_METER_SENSOR] = "flow_meter_required"
            elif mode == DELIVERY_MODE_VOLUME_PRESET and not user_input.get(CONF_ZONE_VOLUME_ENTITY):
                errors[CONF_ZONE_VOLUME_ENTITY] = "volume_entity_required"
            elif override_errors := _override_errors(user_input):
                errors.update(override_errors)
            else:
                zone_metric = _zone_input_to_metric(user_input, imperial)
                self._pending_warnings = _unusual_zone_values(zone_metric, imperial) + _ignored_override_warnings(
                    zone_metric
                )
                if self._pending_warnings:
                    self._pending_zone = zone_metric
                    return await self.async_step_confirm_zone()
                self._zones.append(zone_metric)
                return await self.async_step_add_another()

        return self.async_show_form(
            step_id="zone",
            data_schema=_zone_schema_initial(imperial),
            errors=errors,
            description_placeholders={
                "zone_count": str(len(self._zones)),
            },
        )

    async def async_step_confirm_zone(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Soft guard: unusual zone values need explicit confirmation.

        Values outside the plausibility ranges (tiny area, flow rate that
        smells like a L/min-vs-L/h mix-up) are confirmed, not rejected —
        planter zones and sprinkler manifolds legitimately exceed them.
        Declining returns to the zone form to re-enter the values.
        """
        if user_input is not None:
            pending = self._pending_zone
            self._pending_zone = None
            if user_input.get("confirm") and pending is not None:
                self._zones.append(pending)
                return await self.async_step_add_another()
            return await self.async_step_zone()

        return self.async_show_form(
            step_id="confirm_zone",
            data_schema=_confirm_zone_schema(),
            description_placeholders={
                "zone_name": (self._pending_zone or {}).get(CONF_ZONE_NAME, ""),
                "warnings": "\n".join(f"- {w}" for w in self._pending_warnings),
            },
        )

    async def async_step_add_another(self, user_input: dict[str, Any] | None = None) -> config_entries.ConfigFlowResult:
        """Step 3: Ask whether to add another zone or finish."""
        if user_input is not None:
            if user_input.get("add_another"):
                return await self.async_step_zone()
            return self._create_entry()

        return self.async_show_form(
            step_id="add_another",
            data_schema=vol.Schema(
                {
                    vol.Required("add_another", default=False): bool,
                }
            ),
            description_placeholders={
                "zone_count": str(len(self._zones)),
                "zone_names": ", ".join(z[CONF_ZONE_NAME] for z in self._zones),
            },
        )

    def _create_entry(self) -> config_entries.ConfigFlowResult:
        """Create the config entry with all collected data."""
        self._data[CONF_ZONES] = self._zones
        title = f"NeverDry ({len(self._zones)} zone{'s' if len(self._zones) != 1 else ''})"
        return self.async_create_entry(title=title, data=self._data)

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> NeverDryOptionsFlow:
        """Get the options flow handler."""
        return NeverDryOptionsFlow(config_entry)


class NeverDryOptionsFlow(config_entries.OptionsFlow):
    """Handle options for NeverDry (edit after setup)."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        self._config_entry = config_entry
        self._pending_zone: dict[str, Any] | None = None
        self._pending_warnings: list[str] = []
        self._pending_action: str = ""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> config_entries.ConfigFlowResult:
        """Show menu: edit model params or manage zones."""
        return self.async_show_menu(
            step_id="init",
            menu_options=["model_params", "add_zone", "edit_zone", "check_zones", "remove_zone"],
        )

    async def async_step_model_params(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Edit ET model parameters."""
        imperial = _is_imperial(self.hass)
        errors: dict[str, str] = {}
        if user_input is not None:
            if error := _et_method_error(user_input):
                errors[CONF_ET_METHOD] = error
            else:
                user_input = _sensors_input_to_metric(user_input, imperial)
                new_data = {**self._config_entry.data, **user_input}
                # An optional entity field cleared by the user is simply absent
                # from user_input — the merge above would silently keep the old
                # value, so drop it explicitly. Every optional binding needs
                # this, not just the probe: a method stays available on a sensor
                # the user believes they removed, which is worse than the method
                # disappearing, because the number keeps looking authoritative.
                for key in (CONF_VWC_SENSOR, *(k for k, _ in _EXTRA_SENSORS)):
                    if key not in user_input:
                        new_data.pop(key, None)
                if new_data != dict(self._config_entry.data):
                    changed = [k for k in new_data if new_data[k] != self._config_entry.data.get(k)]
                    _LOGGER.debug("Config updated via model_params — changed keys: %s", changed)
                    self.hass.config_entries.async_update_entry(self._config_entry, data=new_data)
                return self.async_create_entry(data={})

        return self.async_show_form(
            step_id="model_params",
            data_schema=_model_params_schema(imperial, self._config_entry.data),
            errors=errors,
        )

    async def async_step_add_zone(self, user_input: dict[str, Any] | None = None) -> config_entries.ConfigFlowResult:
        """Add a new irrigation zone."""
        imperial = _is_imperial(self.hass)
        if user_input is not None:
            user_input = _flatten_sections(user_input)
            user_input = _zone_input_to_metric(user_input, imperial)
            user_input = _coerce_delivery_mode(user_input)
            new_data = dict(self._config_entry.data)
            zones = list(new_data.get(CONF_ZONES, []))
            # Reject duplicate zone names
            new_name = user_input[CONF_ZONE_NAME]
            existing_names = {z[CONF_ZONE_NAME] for z in zones}
            if new_name in existing_names:
                return self.async_show_form(
                    step_id="add_zone",
                    data_schema=_zone_schema_initial(imperial),
                    errors={"base": "zone_already_exists"},
                )
            if override_errors := _override_errors(user_input):
                return self.async_show_form(
                    step_id="add_zone",
                    data_schema=_zone_schema_initial(imperial),
                    errors=override_errors,
                )
            self._pending_warnings = _unusual_zone_values(user_input, imperial) + _ignored_override_warnings(user_input)
            if self._pending_warnings:
                self._pending_zone = user_input
                self._pending_action = "add"
                return await self.async_step_confirm_zone()
            return self._save_added_zone(user_input)

        return self.async_show_form(
            step_id="add_zone",
            data_schema=_zone_schema_initial(imperial),
        )

    def _save_added_zone(self, zone: dict[str, Any]) -> config_entries.ConfigFlowResult:
        """Append a new zone to the config entry and finish the flow."""
        new_data = dict(self._config_entry.data)
        zones = list(new_data.get(CONF_ZONES, []))
        zones.append(zone)
        new_data[CONF_ZONES] = zones
        if new_data != dict(self._config_entry.data):
            _LOGGER.debug("Config updated via add_zone — zone added: %s", zone.get(CONF_ZONE_NAME))
            self.hass.config_entries.async_update_entry(self._config_entry, data=new_data)
        return self.async_create_entry(data={})

    async def async_step_edit_zone(self, user_input: dict[str, Any] | None = None) -> config_entries.ConfigFlowResult:
        """Select a zone to edit."""
        zones = list(self._config_entry.data.get(CONF_ZONES, []))
        zone_names = [z[CONF_ZONE_NAME] for z in zones]

        if not zone_names:
            return self.async_abort(reason="no_zones")

        if user_input is not None:
            self._edit_zone_name = user_input["zone_to_edit"]
            return await self.async_step_edit_zone_detail()

        return self.async_show_form(
            step_id="edit_zone",
            data_schema=vol.Schema(
                {
                    vol.Required("zone_to_edit"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=zone_names,
                            mode="dropdown",
                        )
                    ),
                }
            ),
        )

    async def async_step_edit_zone_detail(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Edit zone details with current values as defaults."""
        zones = list(self._config_entry.data.get(CONF_ZONES, []))
        cur = next(
            (z for z in zones if z[CONF_ZONE_NAME] == self._edit_zone_name),
            {},
        )

        imperial = _is_imperial(self.hass)
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input = _flatten_sections(user_input)
            user_input = _zone_input_to_metric(user_input, imperial)
            errors = _override_errors(user_input)
            if not errors:
                user_input = _coerce_delivery_mode(user_input)
                self._pending_warnings = _unusual_zone_values(user_input, imperial) + _ignored_override_warnings(
                    user_input
                )
                if self._pending_warnings:
                    self._pending_zone = user_input
                    self._pending_action = "edit"
                    return await self.async_step_confirm_zone()
                return self._save_edited_zone(user_input)
            # Seed the redrawn form from what was submitted, not merged over
            # the stored zone: a cleared optional is absent from user_input, so
            # a merge would resurrect the value this error asks the user for.
            cur = dict(user_input)

        area_unit = "ft²" if imperial else "m²"
        flow_unit = "gal/h" if imperial else "L/h"
        depth_unit = "in" if imperial else "mm"

        # Helper to get current value or UNDEFINED
        def _d(key, fallback=vol.UNDEFINED):
            return cur.get(key, fallback)

        def _d_area(fallback):
            v = cur.get(CONF_ZONE_AREA, fallback)
            return round(v * _M2_TO_FT2, 1) if (imperial and v is not None) else v

        def _d_flow():
            v = cur.get(CONF_ZONE_FLOW_RATE)
            if v is None:
                return vol.UNDEFINED
            # Stored in L/min; UI shows gal/h (imperial) or L/h (metric).
            return round(v * _LPM_TO_GPH, 1) if imperial else round(v * _LPM_TO_LPH, 1)

        def _d_threshold(fallback):
            v = cur.get(CONF_ZONE_THRESHOLD, fallback)
            return round(v * _MM_TO_IN, 2) if (imperial and v is not None) else v

        dm_opts = [
            DELIVERY_MODE_ESTIMATED_FLOW,
            DELIVERY_MODE_FLOW_METER,
            DELIVERY_MODE_VOLUME_PRESET,
        ]
        st_opts = [
            SYSTEM_TYPE_DRIP,
            SYSTEM_TYPE_MICRO_SPRINKLER,
            SYSTEM_TYPE_SPRINKLER,
            SYSTEM_TYPE_MANUAL,
            SYSTEM_TYPE_CUSTOM,
        ]
        pf_opts = list(PLANT_FAMILIES.keys())
        ex_opts = list(EXPOSURES.keys())
        # Both domains, as in the creation form: the adapter drives a `valve.*`
        # with valve services, so restricting this to switches would hide a
        # capability that works (GH #94).
        ent_sw = selector.EntitySelectorConfig(domain=["switch", "valve"])
        ent_sn = selector.EntitySelectorConfig(domain="sensor")
        ent_nr = selector.EntitySelectorConfig(domain="number")

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_ZONE_NAME,
                    default=_d(CONF_ZONE_NAME, ""),
                ): selector.TextSelector(),
                vol.Required(SECTION_GROUND): section(
                    vol.Schema(
                        {
                            vol.Required(
                                CONF_ZONE_AREA,
                                default=_d_area(10.0),
                            ): selector.NumberSelector(
                                selector.NumberSelectorConfig(
                                    min=1.0 if imperial else 0.1,
                                    max=107000.0 if imperial else 10000.0,
                                    step=1.0 if imperial else 0.1,
                                    mode="box",
                                    unit_of_measurement=area_unit,
                                )
                            ),
                            vol.Optional(
                                CONF_ZONE_PLANT_FAMILY,
                                default=_d(CONF_ZONE_PLANT_FAMILY),
                            ): selector.SelectSelector(
                                selector.SelectSelectorConfig(
                                    options=pf_opts,
                                    translation_key="plant_family",
                                    mode="dropdown",
                                )
                            ),
                            vol.Optional(
                                CONF_ZONE_KC,
                                description={"suggested_value": _d(CONF_ZONE_KC, None)},
                            ): selector.NumberSelector(
                                selector.NumberSelectorConfig(
                                    min=0.1,
                                    max=2.0,
                                    step=0.01,
                                    mode="box",
                                )
                            ),
                            vol.Optional(
                                CONF_ZONE_VWC_SENSOR,
                                description={"suggested_value": _d(CONF_ZONE_VWC_SENSOR, None)},
                            ): selector.EntitySelector(
                                selector.EntitySelectorConfig(domain="sensor", device_class="moisture")
                            ),
                            vol.Optional(
                                CONF_ZONE_EXPOSURE,
                                description={"suggested_value": _d(CONF_ZONE_EXPOSURE, DEFAULT_EXPOSURE)},
                            ): selector.SelectSelector(
                                selector.SelectSelectorConfig(
                                    options=ex_opts,
                                    translation_key="exposure",
                                    mode="dropdown",
                                )
                            ),
                            vol.Optional(
                                CONF_ZONE_MICROCLIMATE_FACTOR,
                                description={"suggested_value": _d(CONF_ZONE_MICROCLIMATE_FACTOR, None)},
                            ): selector.NumberSelector(
                                selector.NumberSelectorConfig(
                                    min=MICROCLIMATE_FACTOR_MIN,
                                    max=MICROCLIMATE_FACTOR_MAX,
                                    step=0.01,
                                    mode="box",
                                )
                            ),
                        }
                    ),
                    {"collapsed": True},
                ),
                vol.Required(SECTION_VALVE): section(
                    vol.Schema(
                        {
                            vol.Optional(
                                CONF_ZONE_VALVE,
                                description={"suggested_value": _d(CONF_ZONE_VALVE, None)},
                            ): selector.EntitySelector(ent_sw),
                            vol.Optional(
                                CONF_ZONE_DELIVERY_MODE,
                                default=_d(CONF_ZONE_DELIVERY_MODE, DEFAULT_DELIVERY_MODE),
                            ): selector.SelectSelector(
                                selector.SelectSelectorConfig(
                                    options=dm_opts,
                                    translation_key="delivery_mode",
                                    mode="dropdown",
                                )
                            ),
                            vol.Optional(
                                CONF_ZONE_FLOW_RATE,
                                default=_d_flow(),
                            ): selector.NumberSelector(
                                selector.NumberSelectorConfig(
                                    min=2.0 if imperial else 1.0,
                                    max=3200.0 if imperial else 12000.0,
                                    step=0.5 if imperial else 1.0,
                                    mode="box",
                                    unit_of_measurement=flow_unit,
                                )
                            ),
                            vol.Optional(
                                CONF_ZONE_FLOW_METER_SENSOR,
                                description={"suggested_value": _d(CONF_ZONE_FLOW_METER_SENSOR, None)},
                            ): selector.EntitySelector(ent_sn),
                            vol.Optional(
                                CONF_ZONE_VOLUME_ENTITY,
                                description={"suggested_value": _d(CONF_ZONE_VOLUME_ENTITY, None)},
                            ): selector.EntitySelector(ent_nr),
                            vol.Required(
                                CONF_ZONE_SYSTEM_TYPE,
                                default=_d(CONF_ZONE_SYSTEM_TYPE, SYSTEM_TYPE_DRIP),
                            ): selector.SelectSelector(
                                selector.SelectSelectorConfig(
                                    options=st_opts,
                                    translation_key="system_type",
                                    mode="dropdown",
                                )
                            ),
                            # Overrides below use suggested_value, never default=. With
                            # default=, voluptuous re-injects the stored value whenever the
                            # field comes back empty, so an override can never be removed:
                            # clearing it silently restores what was there (GH #165).
                            vol.Optional(
                                CONF_ZONE_EFFICIENCY,
                                description={"suggested_value": _d(CONF_ZONE_EFFICIENCY, None)},
                            ): selector.NumberSelector(
                                selector.NumberSelectorConfig(
                                    min=0.1,
                                    max=1.0,
                                    step=0.01,
                                    mode="box",
                                )
                            ),
                            vol.Optional(
                                CONF_ZONE_DELIVERY_TIMEOUT,
                                description={
                                    "suggested_value": _d(
                                        CONF_ZONE_DELIVERY_TIMEOUT,
                                        DEFAULT_DELIVERY_TIMEOUT_S,
                                    )
                                },
                            ): selector.NumberSelector(
                                selector.NumberSelectorConfig(
                                    min=60,
                                    max=7200,
                                    step=60,
                                    mode="box",
                                    unit_of_measurement="s",
                                )
                            ),
                        }
                    ),
                    {"collapsed": True},
                ),
                vol.Required(SECTION_SCHEDULING): section(
                    vol.Schema(
                        {
                            vol.Optional(
                                CONF_ZONE_IRRIGATION_MODE,
                                default=_d(
                                    CONF_ZONE_IRRIGATION_MODE,
                                    DEFAULT_IRRIGATION_MODE,
                                ),
                            ): selector.SelectSelector(
                                selector.SelectSelectorConfig(
                                    options=[
                                        IRRIGATION_MODE_MANUAL,
                                        IRRIGATION_MODE_REACTIVE,
                                        IRRIGATION_MODE_SCHEDULED,
                                    ],
                                    translation_key="irrigation_mode",
                                    mode="dropdown",
                                )
                            ),
                            vol.Optional(
                                CONF_ZONE_IRRIGATION_TIME,
                                description={
                                    "suggested_value": _d(
                                        CONF_ZONE_IRRIGATION_TIME,
                                        DEFAULT_IRRIGATION_TIME,
                                    )
                                },
                            ): selector.TimeSelector(),
                            vol.Optional(
                                CONF_ZONE_THRESHOLD,
                                default=_d_threshold(DEFAULT_THRESHOLD),
                            ): selector.NumberSelector(
                                selector.NumberSelectorConfig(
                                    min=0.1 if imperial else 1.0,
                                    max=4.0 if imperial else 100.0,
                                    step=0.01 if imperial else 1.0,
                                    mode="box",
                                    unit_of_measurement=depth_unit,
                                )
                            ),
                        }
                    ),
                    {"collapsed": True},
                ),
            }
        )

        return self.async_show_form(
            step_id="edit_zone_detail",
            data_schema=schema,
            errors=errors,
            description_placeholders={"zone_name": self._edit_zone_name},
        )

    def _save_edited_zone(self, zone: dict[str, Any]) -> config_entries.ConfigFlowResult:
        """Replace the zone being edited in the config entry and finish the flow."""
        zones = list(self._config_entry.data.get(CONF_ZONES, []))
        new_data = dict(self._config_entry.data)
        new_zones = [z for z in zones if z[CONF_ZONE_NAME] != self._edit_zone_name]
        new_zones.append(zone)
        new_data[CONF_ZONES] = new_zones
        if new_data != dict(self._config_entry.data):
            _LOGGER.debug("Config updated via edit_zone — zone edited: %s", self._edit_zone_name)
            self.hass.config_entries.async_update_entry(
                self._config_entry,
                data=new_data,
            )
        return self.async_create_entry(data={})

    async def async_step_confirm_zone(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Soft guard: unusual zone values need explicit confirmation.

        Same semantics as the initial-setup guard: confirm saves the zone
        as entered, declining returns to the add/edit form.
        """
        if user_input is not None:
            pending = self._pending_zone
            self._pending_zone = None
            if user_input.get("confirm") and pending is not None:
                if self._pending_action == "edit":
                    return self._save_edited_zone(pending)
                return self._save_added_zone(pending)
            if self._pending_action == "edit":
                return await self.async_step_edit_zone_detail()
            return await self.async_step_add_zone()

        return self.async_show_form(
            step_id="confirm_zone",
            data_schema=_confirm_zone_schema(),
            description_placeholders={
                "zone_name": (self._pending_zone or {}).get(CONF_ZONE_NAME, ""),
                "warnings": "\n".join(f"- {w}" for w in self._pending_warnings),
            },
        )

    async def async_step_check_zones(self, user_input: dict[str, Any] | None = None) -> config_entries.ConfigFlowResult:
        """Audit all configured zones against the plausibility guards.

        Read-only report for installations configured before the guards
        existed — nothing is modified; fixes go through 'Edit zone'.
        """
        if user_input is not None:
            return self.async_create_entry(data={})

        imperial = _is_imperial(self.hass)
        zones = self._config_entry.data.get(CONF_ZONES, [])
        findings = []
        for z in zones:
            findings.extend(f"- {z[CONF_ZONE_NAME]}: {w}" for w in _unusual_zone_values(z, imperial))
        return self.async_show_form(
            step_id="check_zones",
            data_schema=vol.Schema({}),
            description_placeholders={
                "zone_count": str(len(zones)),
                "findings_count": str(len(findings)),
                "report": "\n".join(findings) if findings else "✓",
            },
        )

    def _remove_zone_device(self, zone_name: str) -> None:
        """Remove the device registry entry for a deleted zone.

        Without this the zone device lingers in the registry after its
        entities are torn down on reload, leaving an undeletable orphan
        that blocks a clean uninstall.
        """
        device_registry = dr.async_get(self.hass)
        identifier = zone_device_identifier(self._config_entry.entry_id, zone_name)
        device = device_registry.async_get_device(identifiers={identifier})
        if device is not None:
            _LOGGER.debug("Removing stale zone device %s (%s)", device.id, zone_name)
            device_registry.async_remove_device(device.id)

    async def async_step_remove_zone(self, user_input: dict[str, Any] | None = None) -> config_entries.ConfigFlowResult:
        """Remove an existing irrigation zone."""
        zones = list(self._config_entry.data.get(CONF_ZONES, []))
        zone_names = [z[CONF_ZONE_NAME] for z in zones]

        if not zone_names:
            return self.async_abort(reason="no_zones")

        if user_input is not None:
            name_to_remove = user_input["zone_to_remove"]
            new_data = dict(self._config_entry.data)
            new_data[CONF_ZONES] = [z for z in zones if z[CONF_ZONE_NAME] != name_to_remove]
            if new_data != dict(self._config_entry.data):
                _LOGGER.debug("Config updated via remove_zone — zone removed: %s", name_to_remove)
                self._remove_zone_device(name_to_remove)
                self.hass.config_entries.async_update_entry(self._config_entry, data=new_data)
            return self.async_create_entry(data={})

        return self.async_show_form(
            step_id="remove_zone",
            data_schema=vol.Schema(
                {
                    vol.Required("zone_to_remove"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=zone_names,
                            mode="dropdown",
                        )
                    ),
                }
            ),
        )
