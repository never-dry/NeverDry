"""Tests for crop coefficient (Kc) computation and per-zone deficit tracking."""

import logging
from unittest.mock import MagicMock

import pytest
from never_dry.const import (
    CONF_ZONE_AREA,
    CONF_ZONE_EFFICIENCY,
    CONF_ZONE_EXPOSURE,
    CONF_ZONE_FLOW_RATE,
    CONF_ZONE_KC,
    CONF_ZONE_MICROCLIMATE_FACTOR,
    CONF_ZONE_NAME,
    CONF_ZONE_PLANT_FAMILY,
    CONF_ZONE_SYSTEM_TYPE,
    CONF_ZONE_VALVE,
    EXPOSURE_CUSTOM,
    EXPOSURE_DEEP_SHADE,
    EXPOSURE_FULL_SUN,
    EXPOSURE_MORNING_SUN,
    EXPOSURE_REFLECTED_HEAT,
    EXPOSURES,
    MICROCLIMATE_FACTOR_MAX,
    MICROCLIMATE_FACTOR_MIN,
    PLANT_FAMILY_CUSTOM,
    SYSTEM_TYPE_CUSTOM,
)
from never_dry.sensor import IrrigationZoneSensor, compute_kc, resolve_microclimate_factor

# ══════════════════════════════════════════════════════════
#  compute_kc pure function
# ══════════════════════════════════════════════════════════


class TestComputeKcOverride:
    """The custom family reads the manual Kc; every other family ignores it."""

    def test_the_custom_family_uses_the_manual_kc(self):
        assert compute_kc(196, PLANT_FAMILY_CUSTOM, 0.5, 45.0) == 0.5

    def test_a_real_family_ignores_the_manual_kc(self):
        """The dropdown decides. Zones configured under the old rule were
        migrated to the custom family, so their number still applies."""
        assert compute_kc(196, "lawn", 0.5, 45.0) == pytest.approx(1.0, abs=0.01)

    def test_no_family_ignores_it_too(self):
        """Not a trap: the config flow warns that the value will not be used."""
        assert compute_kc(196, None, 0.75, 45.0) == 1.0

    def test_the_custom_family_without_a_value_is_neutral(self):
        """The flow rejects this combination; a hand-edited entry could hold it."""
        assert compute_kc(196, PLANT_FAMILY_CUSTOM, None, 45.0) == 1.0


class TestComputeKcDefaults:
    """No family and no override → DEFAULT_KC (1.0)."""

    def test_no_family_no_override(self):
        assert compute_kc(196, None, None, 45.0) == 1.0

    def test_unknown_family(self):
        assert compute_kc(196, "unknown_plant", None, 45.0) == 1.0


class TestComputeKcAnchors:
    """Kc at exact anchor days should match the tuple values."""

    def test_lawn_mid_winter(self):
        # doy=15, lawn kc_seasonal=(0.45, 0.85, 1.00, 0.70)
        assert compute_kc(15, "lawn", None, 45.0) == pytest.approx(0.45, abs=0.01)

    def test_lawn_mid_spring(self):
        assert compute_kc(105, "lawn", None, 45.0) == pytest.approx(0.85, abs=0.01)

    def test_lawn_mid_summer(self):
        assert compute_kc(196, "lawn", None, 45.0) == pytest.approx(1.0, abs=0.01)

    def test_lawn_mid_autumn(self):
        assert compute_kc(288, "lawn", None, 45.0) == pytest.approx(0.70, abs=0.01)

    def test_succulents_mid_summer(self):
        assert compute_kc(196, "succulents", None, 45.0) == pytest.approx(0.35, abs=0.01)

    def test_vegetables_mid_summer(self):
        assert compute_kc(196, "vegetables", None, 45.0) == pytest.approx(1.10, abs=0.01)


class TestComputeKcInterpolation:
    """Kc between anchors should be linearly interpolated."""

    def test_midpoint_winter_spring(self):
        # Midpoint between 15 and 105 = day 60
        # lawn: 0.45 + (60-15)/(105-15) * (0.85-0.45) = 0.45 + 0.5*0.4 = 0.65
        result = compute_kc(60, "lawn", None, 45.0)
        assert result == pytest.approx(0.65, abs=0.01)

    def test_midpoint_summer_autumn(self):
        # Midpoint between 196 and 288 = day 242
        # lawn: 1.0 + (242-196)/(288-196) * (0.70-1.0) = 1.0 + 0.5*(-0.3) = 0.85
        result = compute_kc(242, "lawn", None, 45.0)
        assert result == pytest.approx(0.85, abs=0.01)


class TestComputeKcWrapAround:
    """Test year boundary (autumn → winter, crossing Dec-Jan)."""

    def test_day_350(self):
        # Between autumn (288) and winter (15), wrapping around.
        # lawn: autumn=0.70, winter=0.45
        result = compute_kc(350, "lawn", None, 45.0)
        assert 0.45 <= result <= 0.70

    def test_day_1(self):
        # Just after New Year, between autumn and winter anchor
        result = compute_kc(1, "lawn", None, 45.0)
        assert 0.45 <= result <= 0.70

    def test_day_365(self):
        result = compute_kc(365, "lawn", None, 45.0)
        assert 0.45 <= result <= 0.70


class TestComputeKcSouthernHemisphere:
    """Southern hemisphere flips seasons by 182 days."""

    def test_southern_mid_summer_is_northern_winter(self):
        # doy=196 (Jul) in southern hemisphere → shifted to ~Jan → winter Kc
        result = compute_kc(196, "lawn", None, -33.0)
        assert result == pytest.approx(0.45, abs=0.05)

    def test_southern_mid_winter_is_northern_summer(self):
        # doy=15 (Jan) in southern hemisphere → shifted to ~Jul → summer Kc
        result = compute_kc(15, "lawn", None, -33.0)
        assert result == pytest.approx(1.0, abs=0.05)

    def test_equator_is_northern(self):
        # latitude=0 → northern (no shift)
        result = compute_kc(196, "lawn", None, 0.0)
        assert result == pytest.approx(1.0, abs=0.01)


# ══════════════════════════════════════════════════════════
#  resolve_microclimate_factor — exposure preset → kmc
# ══════════════════════════════════════════════════════════


class TestResolveMicroclimateFactor:
    """Exposure presets resolve to their table factor; anything odd → 1.0."""

    def test_presets_match_the_table(self):
        for key, preset in EXPOSURES.items():
            if preset["factor"] is not None:
                assert resolve_microclimate_factor(key) == preset["factor"]

    def test_default_full_sun_is_neutral(self):
        assert resolve_microclimate_factor(EXPOSURE_FULL_SUN) == 1.0

    def test_unset_exposure_is_neutral(self):
        assert resolve_microclimate_factor(None) == 1.0

    def test_unknown_exposure_is_neutral(self):
        assert resolve_microclimate_factor("under_a_rock") == 1.0

    def test_preset_ignores_a_stale_custom_value(self):
        """The dropdown wins: a leftover number never overrides a preset."""
        assert resolve_microclimate_factor(EXPOSURE_DEEP_SHADE, 1.2) == 0.60

    def test_custom_uses_the_number(self):
        assert resolve_microclimate_factor(EXPOSURE_CUSTOM, 0.42) == 0.42

    def test_custom_accepts_above_one(self):
        assert resolve_microclimate_factor(EXPOSURE_CUSTOM, 1.35) == 1.35

    def test_custom_without_a_number_is_neutral(self):
        assert resolve_microclimate_factor(EXPOSURE_CUSTOM, None) == 1.0

    def test_custom_with_garbage_is_neutral(self):
        assert resolve_microclimate_factor(EXPOSURE_CUSTOM, "shady") == 1.0

    def test_zero_is_floored_not_honoured(self):
        """At 0 the deficit never accrues and every trigger goes silent."""
        assert resolve_microclimate_factor(EXPOSURE_CUSTOM, 0.0) == MICROCLIMATE_FACTOR_MIN

    def test_negative_is_floored(self):
        assert resolve_microclimate_factor(EXPOSURE_CUSTOM, -3.0) == MICROCLIMATE_FACTOR_MIN

    def test_above_max_is_clamped(self):
        assert resolve_microclimate_factor(EXPOSURE_CUSTOM, 99.0) == MICROCLIMATE_FACTOR_MAX

    def test_infinite_is_neutral(self):
        assert resolve_microclimate_factor(EXPOSURE_CUSTOM, float("inf")) == 1.0

    def test_nan_is_neutral(self):
        assert resolve_microclimate_factor(EXPOSURE_CUSTOM, float("nan")) == 1.0

    def test_numeric_string_is_honoured(self):
        """A hand-edited .storage entry stores "0.65" rather than 0.65."""
        assert resolve_microclimate_factor(EXPOSURE_CUSTOM, "0.65") == 0.65

    def test_unhashable_exposure_is_neutral(self):
        """Must not raise: __init__ would abort setup for every zone in the entry."""
        assert resolve_microclimate_factor(["deep_shade"]) == 1.0
        assert resolve_microclimate_factor({"a": 1}) == 1.0

    def test_non_string_exposure_is_neutral(self):
        assert resolve_microclimate_factor(0.75) == 1.0


# ══════════════════════════════════════════════════════════
#  compute_kc * microclimate factor
# ══════════════════════════════════════════════════════════


class TestComputeKcMicroclimate:
    """The exposure factor multiplies the Kc instead of replacing it."""

    def test_default_factor_leaves_kc_untouched(self):
        assert compute_kc(196, "lawn", None, 45.0) == compute_kc(196, "lawn", None, 45.0, 1.0)

    def test_factor_scales_the_family_curve(self):
        # lawn mid-summer = 1.00, morning-sun exposure = 0.75
        assert compute_kc(196, "lawn", None, 45.0, 0.75) == pytest.approx(0.75, abs=0.001)

    def test_factor_scales_the_manual_override(self):
        """Site exposure describes the site, not the planting — it applies to both."""
        assert compute_kc(196, PLANT_FAMILY_CUSTOM, 0.80, 45.0, 0.75) == pytest.approx(0.60, abs=0.001)

    def test_factor_above_one_raises_kc(self):
        # lawn mid-summer 1.00 * reflected heat 1.20
        assert compute_kc(196, "lawn", None, 45.0, 1.20) == pytest.approx(1.20, abs=0.001)

    def test_seasonal_shape_is_preserved(self):
        """The point of #146: a shaded zone tracks the season, a frozen Kc does not."""
        factor = 0.75
        august = compute_kc(217, "lawn", None, 45.0, factor)
        october = compute_kc(288, "lawn", None, 45.0, factor)
        assert august > october
        # October: lawn autumn anchor 0.70 * 0.75 = 0.525 — where a frozen
        # manual Kc of 0.70 would over-water by ~33%.
        assert october == pytest.approx(0.525, abs=0.001)

    def test_no_family_no_override_scales_default(self):
        assert compute_kc(196, None, None, 45.0, 0.60) == pytest.approx(0.60, abs=0.001)


# ══════════════════════════════════════════════════════════
#  Per-zone deficit tracking
# ══════════════════════════════════════════════════════════


def _make_hass():
    hass = MagicMock()
    hass.config = MagicMock()
    hass.config.latitude = 45.0
    return hass


def _make_zone_sensor(di_sensor, plant_family=None, kc=None, exposure=None, microclimate_factor=None):
    """Helper for zone sensors with Kc config."""
    zone_config = {
        CONF_ZONE_NAME: "Test",
        CONF_ZONE_VALVE: "switch.valve",
        CONF_ZONE_AREA: 20.0,
        CONF_ZONE_SYSTEM_TYPE: SYSTEM_TYPE_CUSTOM,
        CONF_ZONE_EFFICIENCY: 0.85,
        CONF_ZONE_FLOW_RATE: 10.0,
    }
    if plant_family is not None:
        zone_config[CONF_ZONE_PLANT_FAMILY] = plant_family
    if kc is not None:
        zone_config[CONF_ZONE_KC] = kc
        # A manual Kc is read only behind the custom family, so a caller
        # asking for one means the custom family unless it says otherwise.
        zone_config.setdefault(CONF_ZONE_PLANT_FAMILY, PLANT_FAMILY_CUSTOM)
    if exposure is not None:
        zone_config[CONF_ZONE_EXPOSURE] = exposure
    if microclimate_factor is not None:
        zone_config[CONF_ZONE_MICROCLIMATE_FACTOR] = microclimate_factor
    return IrrigationZoneSensor(_make_hass(), zone_config, di_sensor)


class TestPerZoneDeficit:
    """Test zone-level deficit accumulation via _on_et_update."""

    def test_deficit_accumulates_with_kc(self, di_sensor):
        """Zone deficit = ET_h * Kc * dt_h."""
        zone = _make_zone_sensor(di_sensor, kc=0.5)
        zone._on_et_update(dt_h=1.0, et_h=0.15, rain=0.0)
        # 0.15 * 0.5 * 1.0 = 0.075
        assert zone._zone_deficit == pytest.approx(0.075, abs=0.001)

    def test_rain_reduces_zone_deficit(self, di_sensor):
        """Rain reduces zone deficit."""
        zone = _make_zone_sensor(di_sensor, kc=1.0)
        zone._zone_deficit = 5.0
        zone._on_et_update(dt_h=1.0, et_h=0.0, rain=3.0)
        assert zone._zone_deficit == pytest.approx(2.0, abs=0.01)

    def test_deficit_never_negative(self, di_sensor):
        zone = _make_zone_sensor(di_sensor, kc=1.0)
        zone._zone_deficit = 1.0
        zone._on_et_update(dt_h=1.0, et_h=0.0, rain=10.0)
        assert zone._zone_deficit == 0.0

    def test_deficit_clamped_at_d_max(self, di_sensor):
        zone = _make_zone_sensor(di_sensor, kc=1.0)
        zone._zone_deficit = 99.0
        zone._on_et_update(dt_h=10.0, et_h=0.5, rain=0.0)
        assert zone._zone_deficit == di_sensor._d_max

    def test_different_kc_different_deficits(self, di_sensor):
        """Two zones with different Kc accumulate differently."""
        lawn = _make_zone_sensor(di_sensor, kc=1.0)
        succulent = _make_zone_sensor(di_sensor, kc=0.3)

        lawn._on_et_update(dt_h=1.0, et_h=0.15, rain=0.0)
        succulent._on_et_update(dt_h=1.0, et_h=0.15, rain=0.0)

        assert lawn._zone_deficit > succulent._zone_deficit
        assert lawn._zone_deficit == pytest.approx(0.15, abs=0.001)
        assert succulent._zone_deficit == pytest.approx(0.045, abs=0.001)

    def test_volume_uses_zone_deficit(self, di_sensor):
        """Volume should use _zone_deficit, not shared deficit."""
        zone = _make_zone_sensor(di_sensor, kc=1.0)
        di_sensor._deficit = 50.0  # shared deficit is high
        zone._zone_deficit = 5.0  # zone deficit is low
        # Volume = 5 * 20 / 0.85 = 117.6 (not 50 * 20 / 0.85)
        assert zone.volume_liters == pytest.approx(117.6, abs=0.1)

    def test_reset_deficit(self, di_sensor):
        """reset_deficit zeroes only this zone."""
        zone = _make_zone_sensor(di_sensor, kc=1.0)
        zone._zone_deficit = 15.0
        zone.reset_deficit()
        assert zone._zone_deficit == 0.0
        assert zone.volume_liters == 0.0


class TestKcInAttributes:
    """Test Kc-related fields in extra_state_attributes."""

    def test_kc_in_attributes(self, di_sensor):
        zone = _make_zone_sensor(di_sensor, plant_family="lawn")
        attrs = zone.extra_state_attributes
        assert "kc" in attrs
        assert "plant_family" in attrs
        assert attrs["plant_family"] == "lawn"
        assert attrs["kc"] > 0

    def test_kc_override_in_attributes(self, di_sensor):
        zone = _make_zone_sensor(di_sensor, plant_family=PLANT_FAMILY_CUSTOM, kc=0.6)
        attrs = zone.extra_state_attributes
        assert attrs["kc_override"] == 0.6
        assert attrs["kc"] == 0.6

    def test_an_override_behind_a_real_family_is_published_but_not_applied(self, di_sensor):
        """The attribute still reports what is stored — the Kc shows it is unused."""
        zone = _make_zone_sensor(di_sensor, plant_family="lawn", kc=0.6)
        attrs = zone.extra_state_attributes
        assert attrs["kc_override"] == 0.6
        assert attrs["kc"] != 0.6

    def test_no_family_kc_is_1(self, di_sensor):
        zone = _make_zone_sensor(di_sensor)
        attrs = zone.extra_state_attributes
        assert attrs["kc"] == 1.0
        assert attrs["plant_family"] is None


class TestZoneExposure:
    """Site exposure on the zone sensor: resolution, deficit, attributes."""

    def test_no_exposure_keeps_previous_behaviour(self, di_sensor):
        zone = _make_zone_sensor(di_sensor, plant_family="lawn")
        assert zone._microclimate_factor == 1.0
        assert zone._get_current_kc() == zone._get_base_kc()

    def test_preset_scales_the_zone_kc(self, di_sensor):
        shaded = _make_zone_sensor(di_sensor, plant_family="lawn", exposure=EXPOSURE_MORNING_SUN)
        open_site = _make_zone_sensor(di_sensor, plant_family="lawn", exposure=EXPOSURE_FULL_SUN)
        assert shaded._get_current_kc() == pytest.approx(open_site._get_current_kc() * 0.75, abs=0.001)
        # The base curve is untouched — only the effective Kc moves.
        assert shaded._get_base_kc() == open_site._get_base_kc()

    def test_custom_factor_is_used(self, di_sensor):
        zone = _make_zone_sensor(
            di_sensor,
            plant_family="lawn",
            exposure=EXPOSURE_CUSTOM,
            microclimate_factor=0.65,
        )
        assert zone._microclimate_factor == 0.65

    def test_custom_without_value_falls_back_to_neutral(self, di_sensor):
        zone = _make_zone_sensor(di_sensor, plant_family="lawn", exposure=EXPOSURE_CUSTOM)
        assert zone._microclimate_factor == 1.0

    def test_unknown_exposure_falls_back_to_neutral(self, di_sensor):
        zone = _make_zone_sensor(di_sensor, plant_family="lawn", exposure="mystery")
        assert zone._microclimate_factor == 1.0

    def test_numeric_string_factor_does_not_warn(self, di_sensor, caplog):
        """The stored value was honoured, so a 'using X instead' warning would lie."""
        zone = _make_zone_sensor(
            di_sensor,
            plant_family="lawn",
            exposure=EXPOSURE_CUSTOM,
            microclimate_factor="0.65",
        )
        assert zone._microclimate_factor == 0.65
        assert "microclimate factor" not in caplog.text

    def test_out_of_range_factor_warns(self, di_sensor, caplog):
        with caplog.at_level(logging.WARNING):
            zone = _make_zone_sensor(
                di_sensor,
                plant_family="lawn",
                exposure=EXPOSURE_CUSTOM,
                microclimate_factor=9.0,
            )
        assert zone._microclimate_factor == MICROCLIMATE_FACTOR_MAX
        assert "outside" in caplog.text

    def test_effective_kc_stays_consistent_with_base(self, di_sensor):
        """kc must always equal kc_base * factor — the attributes explain each other."""
        zone = _make_zone_sensor(di_sensor, plant_family="vegetables", exposure=EXPOSURE_REFLECTED_HEAT)
        attrs = zone.extra_state_attributes
        assert attrs["kc"] == pytest.approx(attrs["kc_base"] * attrs["microclimate_factor"], abs=0.001)

    def test_two_identical_zones_differ_by_exposure_only(self, di_sensor):
        """The #146 use case: same lawn, one shaded from 14:00."""
        sunny = _make_zone_sensor(di_sensor, plant_family="lawn", exposure=EXPOSURE_FULL_SUN)
        shaded = _make_zone_sensor(di_sensor, plant_family="lawn", exposure=EXPOSURE_MORNING_SUN)
        sunny._on_et_update(dt_h=1.0, et_h=0.20, rain=0.0)
        shaded._on_et_update(dt_h=1.0, et_h=0.20, rain=0.0)
        assert shaded._zone_deficit == pytest.approx(sunny._zone_deficit * 0.75, abs=0.001)

    def test_exposure_scales_deficit_in_vwc_mode(self, di_sensor):
        """VWC mode multiplies the reference deficit by the same effective Kc."""
        di_sensor._deficit = 10.0
        zone = _make_zone_sensor(di_sensor, plant_family="lawn", exposure=EXPOSURE_DEEP_SHADE)
        zone._on_et_update(dt_h=0.0, et_h=0.0, rain=0.0)
        assert zone._zone_deficit == pytest.approx(10.0 * zone._get_current_kc(), abs=0.001)

    def test_reflected_heat_raises_the_deficit(self, di_sensor):
        hot = _make_zone_sensor(di_sensor, plant_family="lawn", exposure=EXPOSURE_REFLECTED_HEAT)
        open_site = _make_zone_sensor(di_sensor, plant_family="lawn", exposure=EXPOSURE_FULL_SUN)
        hot._on_et_update(dt_h=1.0, et_h=0.20, rain=0.0)
        open_site._on_et_update(dt_h=1.0, et_h=0.20, rain=0.0)
        assert hot._zone_deficit > open_site._zone_deficit

    def test_exposure_in_attributes(self, di_sensor):
        zone = _make_zone_sensor(di_sensor, plant_family="lawn", exposure=EXPOSURE_MORNING_SUN)
        attrs = zone.extra_state_attributes
        assert attrs["exposure"] == EXPOSURE_MORNING_SUN
        assert attrs["microclimate_factor"] == 0.75
        assert attrs["kc"] == pytest.approx(attrs["kc_base"] * 0.75, abs=0.002)

    def test_attributes_without_exposure(self, di_sensor):
        attrs = _make_zone_sensor(di_sensor, plant_family="lawn").extra_state_attributes
        assert attrs["exposure"] is None
        assert attrs["microclimate_factor"] == 1.0
        assert attrs["kc"] == attrs["kc_base"]
