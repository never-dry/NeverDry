"""Pure unit-conversion helpers for the config flow.

NeverDry always stores and computes in metric (mm, m², L, L/min, °C). When
Home Assistant runs in US-customary mode the config flow shows imperial labels
and must convert user input back to metric before persisting it. These helpers
are deliberately free of any Home Assistant import so they can be unit-tested
in isolation.
"""

from __future__ import annotations

from .const import (
    CONF_D_MAX,
    CONF_T_BASE,
    CONF_ZONE_AREA,
    CONF_ZONE_FLOW_RATE,
    CONF_ZONE_THRESHOLD,
)

# ── Conversion factors ─────────────────────────────────────
MM_TO_IN = 1.0 / 25.4
IN_TO_MM = 25.4
M2_TO_FT2 = 10.7639
FT2_TO_M2 = 1.0 / 10.7639
#: One US liquid gallon in litres; volumes convert with its inverse.
LITERS_TO_GALLONS = 0.264172
LPM_TO_GPM = 0.264172
GPM_TO_LPM = 1.0 / 0.264172
# Flow rate is shown in the UI as L/h (metric) or gal/h (imperial) but stored
# internally in L/min.
LPM_TO_LPH = 60.0
LPH_TO_LPM = 1.0 / 60.0
LPM_TO_GPH = LPM_TO_GPM * 60.0
GPH_TO_LPM = GPM_TO_LPM / 60.0


def c_to_f(celsius: float) -> float:
    """Celsius → Fahrenheit, rounded to 0.1°."""
    return round(celsius * 9 / 5 + 32, 1)


def f_to_c(fahrenheit: float) -> float:
    """Fahrenheit → Celsius."""
    return (fahrenheit - 32) * 5 / 9


def sensors_input_to_metric(user_input: dict, is_imperial: bool) -> dict:
    """Convert T_base °F→°C and D_max in→mm when the form was shown in imperial."""
    if not is_imperial:
        return user_input
    out = dict(user_input)
    if out.get(CONF_T_BASE) is not None:
        out[CONF_T_BASE] = f_to_c(out[CONF_T_BASE])
    if out.get(CONF_D_MAX) is not None:
        out[CONF_D_MAX] = out[CONF_D_MAX] * IN_TO_MM
    return out


def zone_input_to_metric(user_input: dict, is_imperial: bool) -> dict:
    """Convert UI values to metric storage.

    Flow rate is entered in L/h (metric) or gal/h (imperial) and is always
    stored in L/min. Area (ft²→m²) and threshold (in→mm) are converted only
    when the form was shown in imperial units.
    """
    out = dict(user_input)
    if out.get(CONF_ZONE_FLOW_RATE) is not None:
        if is_imperial:
            out[CONF_ZONE_FLOW_RATE] = out[CONF_ZONE_FLOW_RATE] * GPH_TO_LPM
        else:
            out[CONF_ZONE_FLOW_RATE] = out[CONF_ZONE_FLOW_RATE] * LPH_TO_LPM
    if is_imperial:
        if out.get(CONF_ZONE_AREA) is not None:
            out[CONF_ZONE_AREA] = out[CONF_ZONE_AREA] * FT2_TO_M2
        if out.get(CONF_ZONE_THRESHOLD) is not None:
            out[CONF_ZONE_THRESHOLD] = out[CONF_ZONE_THRESHOLD] * IN_TO_MM
    return out
