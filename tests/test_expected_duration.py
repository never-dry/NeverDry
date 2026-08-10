"""Expected-duration source chain for flow-metered zones (AI-150).

duration_s must be meaningful for every delivery mode:
1. live flow-meter rate (flow_meter mode, rate sensor reading > 0)
2. configured guard flow rate
3. 0 (no source — only the delivery_timeout floor guards the valve)

delivery_timeout scales with the guard-flow estimate only, never the
live rate, so a momentary high meter reading cannot tighten the watchdog.
"""

import logging
from unittest.mock import MagicMock

import pytest
from never_dry import flow_utils
from never_dry.const import (
    CONF_ZONE_AREA,
    CONF_ZONE_DELIVERY_MODE,
    CONF_ZONE_DELIVERY_TIMEOUT,
    CONF_ZONE_EFFICIENCY,
    CONF_ZONE_FLOW_METER_SENSOR,
    CONF_ZONE_FLOW_RATE,
    CONF_ZONE_NAME,
    CONF_ZONE_SYSTEM_TYPE,
    CONF_ZONE_VALVE,
    CONF_ZONE_VOLUME_ENTITY,
    DELIVERY_MODE_ESTIMATED_FLOW,
    DELIVERY_MODE_FLOW_METER,
    DELIVERY_MODE_VOLUME_PRESET,
    SYSTEM_TYPE_CUSTOM,
)
from never_dry.sensor import IrrigationZoneSensor

METER = "sensor.flow_meter"


def _state(value, unit):
    return MagicMock(state=str(value), attributes={"unit_of_measurement": unit})


def _hass_with_meter(value, unit):
    hass = MagicMock()
    hass.config = MagicMock()
    hass.config.latitude = 45.0
    hass.states.get = MagicMock(return_value=_state(value, unit))
    return hass


def _make_zone(
    di_sensor,
    hass,
    mode=DELIVERY_MODE_FLOW_METER,
    flow_rate=None,
    meter=METER,
    timeout=None,
):
    config = {
        CONF_ZONE_NAME: "Test",
        CONF_ZONE_VALVE: "switch.valve",
        CONF_ZONE_AREA: 45.0,
        CONF_ZONE_SYSTEM_TYPE: SYSTEM_TYPE_CUSTOM,
        CONF_ZONE_EFFICIENCY: 0.85,
        CONF_ZONE_DELIVERY_MODE: mode,
    }
    if flow_rate is not None:
        config[CONF_ZONE_FLOW_RATE] = flow_rate
    if meter is not None and mode == DELIVERY_MODE_FLOW_METER:
        config[CONF_ZONE_FLOW_METER_SENSOR] = meter
    if mode == DELIVERY_MODE_VOLUME_PRESET:
        config[CONF_ZONE_VOLUME_ENTITY] = "number.volume"
    if timeout is not None:
        config[CONF_ZONE_DELIVERY_TIMEOUT] = timeout
    return IrrigationZoneSensor(hass, config, di_sensor)


class TestReadFlowRateLpm:
    """flow_utils.read_flow_rate_lpm — unit normalization and fallbacks."""

    def test_lpm_passthrough(self):
        hass = _hass_with_meter(5.0, "L/min")
        assert flow_utils.read_flow_rate_lpm(hass, METER) == pytest.approx(5.0)

    def test_lph_converted(self):
        hass = _hass_with_meter(120.0, "L/h")
        assert flow_utils.read_flow_rate_lpm(hass, METER) == pytest.approx(2.0)

    def test_m3h_converted(self):
        hass = _hass_with_meter(0.6, "m³/h")
        assert flow_utils.read_flow_rate_lpm(hass, METER) == pytest.approx(10.0)

    def test_gal_min_converted(self):
        hass = _hass_with_meter(1.0, "gal/min")
        assert flow_utils.read_flow_rate_lpm(hass, METER) == pytest.approx(3.785, abs=0.01)

    def test_gal_h_converted(self):
        hass = _hass_with_meter(60.0, "gal/h")
        assert flow_utils.read_flow_rate_lpm(hass, METER) == pytest.approx(3.785, abs=0.01)

    def test_volume_sensor_returns_none(self):
        """Cumulative volume meters have no instantaneous rate."""
        hass = _hass_with_meter(42.0, "L")
        assert flow_utils.read_flow_rate_lpm(hass, METER) is None

    def test_zero_rate_returns_none(self):
        hass = _hass_with_meter(0.0, "L/min")
        assert flow_utils.read_flow_rate_lpm(hass, METER) is None

    def test_unavailable_returns_none(self):
        hass = MagicMock()
        hass.states.get = MagicMock(
            return_value=MagicMock(state="unavailable", attributes={"unit_of_measurement": "L/min"})
        )
        assert flow_utils.read_flow_rate_lpm(hass, METER) is None

    def test_missing_entity_returns_none(self):
        hass = MagicMock()
        hass.states.get = MagicMock(return_value=None)
        assert flow_utils.read_flow_rate_lpm(hass, METER) is None


class TestFlowMeterDuration:
    """duration_s for flow_meter zones — live rate, guard fallback, zero."""

    def test_live_rate_used(self, di_sensor):
        """Rate sensor reading 10 L/min → duration from live rate."""
        hass = _hass_with_meter(10.0, "L/min")
        zone = _make_zone(di_sensor, hass, flow_rate=5.0)
        zone._zone_deficit = 10.0
        expected = round(zone.volume_liters / 10.0 * 60)
        assert zone.duration_s == expected

    def test_live_rate_zero_falls_back_to_guard(self, di_sensor):
        """Valve closed (meter reads 0) → guard flow estimate."""
        hass = _hass_with_meter(0.0, "L/min")
        zone = _make_zone(di_sensor, hass, flow_rate=5.0)
        zone._zone_deficit = 10.0
        expected = round(zone.volume_liters / 5.0 * 60)
        assert zone.duration_s == expected

    def test_volume_meter_falls_back_to_guard(self, di_sensor):
        """Cumulative meter (no rate) → guard flow estimate."""
        hass = _hass_with_meter(42.0, "L")
        zone = _make_zone(di_sensor, hass, flow_rate=5.0)
        zone._zone_deficit = 10.0
        expected = round(zone.volume_liters / 5.0 * 60)
        assert zone.duration_s == expected

    def test_unavailable_meter_falls_back_to_guard(self, di_sensor):
        hass = MagicMock()
        hass.config = MagicMock()
        hass.config.latitude = 45.0
        hass.states.get = MagicMock(return_value=None)
        zone = _make_zone(di_sensor, hass, flow_rate=5.0)
        zone._zone_deficit = 10.0
        expected = round(zone.volume_liters / 5.0 * 60)
        assert zone.duration_s == expected

    def test_no_guard_flow_returns_zero_and_warns(self, di_sensor, caplog):
        hass = _hass_with_meter(42.0, "L")
        zone = _make_zone(di_sensor, hass, flow_rate=None)
        zone._zone_deficit = 10.0
        with caplog.at_level(logging.WARNING, logger="custom_components.never_dry"):
            assert zone.duration_s == 0
            assert zone.duration_s == 0  # second read: warning stays once-per-zone
        guard_warnings = [r for r in caplog.records if "guard flow rate" in r.message]
        assert len(guard_warnings) == 1

    def test_zero_deficit_with_guard_flow_does_not_warn(self, di_sensor, caplog):
        """Field false positive (2026-07-15): guard duration is 0 at the end
        of every session because the VOLUME is 0 — with a guard flow
        configured that must not trigger the 'no guard flow' warning."""
        hass = _hass_with_meter(42.0, "L")
        zone = _make_zone(di_sensor, hass, flow_rate=5.0)
        zone._zone_deficit = 0.0
        with caplog.at_level(logging.WARNING, logger="custom_components.never_dry"):
            assert zone.duration_s == 0
        assert not any("guard flow rate" in r.message for r in caplog.records)

    def test_imperial_live_rate(self, di_sensor):
        """gal/min live rate is normalized to L/min before use."""
        hass = _hass_with_meter(1.0, "gal/min")
        zone = _make_zone(di_sensor, hass, flow_rate=5.0)
        zone._zone_deficit = 10.0
        expected = round(zone.volume_liters / 3.785411784 * 60)
        assert zone.duration_s == expected


class TestVolumePresetDuration:
    """volume_preset zones estimate duration from the guard flow."""

    def test_guard_flow_used(self, di_sensor):
        hass = _hass_with_meter(0.0, "L")
        zone = _make_zone(di_sensor, hass, mode=DELIVERY_MODE_VOLUME_PRESET, flow_rate=8.0)
        zone._zone_deficit = 10.0
        expected = round(zone.volume_liters / 8.0 * 60)
        assert zone.duration_s == expected

    def test_no_guard_flow_zero(self, di_sensor):
        hass = _hass_with_meter(0.0, "L")
        zone = _make_zone(di_sensor, hass, mode=DELIVERY_MODE_VOLUME_PRESET, flow_rate=None)
        zone._zone_deficit = 10.0
        assert zone.duration_s == 0


class TestEstimatedFlowRegression:
    """estimated_flow behaviour is unchanged (and never reads the meter)."""

    def test_guard_flow_duration(self, di_sensor):
        hass = _hass_with_meter(99.0, "L/min")  # would give a wrong answer if read
        zone = _make_zone(di_sensor, hass, mode=DELIVERY_MODE_ESTIMATED_FLOW, flow_rate=10.0, meter=None)
        zone._zone_deficit = 10.0
        expected = round(zone.volume_liters / 10.0 * 60)
        assert zone.duration_s == expected

    def test_no_flow_rate_zero_without_warning(self, di_sensor, caplog):
        """estimated_flow without flow_rate is a config-flow error, not a runtime warning."""
        hass = _hass_with_meter(0.0, "L")
        zone = _make_zone(di_sensor, hass, mode=DELIVERY_MODE_ESTIMATED_FLOW, flow_rate=None, meter=None)
        zone._zone_deficit = 10.0
        with caplog.at_level(logging.WARNING, logger="custom_components.never_dry"):
            assert zone.duration_s == 0
        assert not any("guard flow rate" in r.message for r in caplog.records)


class TestDeliveryTimeoutScaling:
    """delivery_timeout = max(floor, 1.1 x guard-flow duration)."""

    def test_floor_when_duration_small(self, di_sensor):
        hass = _hass_with_meter(0.0, "L")
        zone = _make_zone(di_sensor, hass, flow_rate=10.0, timeout=3600)
        zone._zone_deficit = 1.0  # small deficit → short duration
        assert zone.delivery_timeout == 3600

    def test_scales_with_large_deficit(self, di_sensor):
        hass = _hass_with_meter(0.0, "L")
        zone = _make_zone(di_sensor, hass, flow_rate=2.0, timeout=3600)
        zone._zone_deficit = 50.0
        guard_s = round(zone.volume_liters / 2.0 * 60)
        assert guard_s > 3600
        assert zone.delivery_timeout == max(3600, round(guard_s * 1.1))

    def test_ignores_live_rate(self, di_sensor):
        """A momentary high meter reading must not shrink the timeout."""
        hass = _hass_with_meter(100.0, "L/min")
        zone = _make_zone(di_sensor, hass, flow_rate=2.0, timeout=3600)
        zone._zone_deficit = 50.0
        guard_s = round(zone.volume_liters / 2.0 * 60)
        assert zone.delivery_timeout == max(3600, round(guard_s * 1.1))

    def test_no_guard_flow_stays_at_floor(self, di_sensor):
        hass = _hass_with_meter(0.0, "L")
        zone = _make_zone(di_sensor, hass, flow_rate=None, timeout=3600)
        zone._zone_deficit = 50.0
        assert zone.delivery_timeout == 3600


class TestSessionListeners:
    """Dependent entities refresh while a session runs."""

    def test_set_irrigating_notifies(self, di_sensor):
        hass = _hass_with_meter(0.0, "L")
        zone = _make_zone(di_sensor, hass, flow_rate=5.0)
        calls = []
        zone.register_session_listener(lambda: calls.append(1))
        zone.set_irrigating(True)
        zone.set_irrigating(False)
        assert len(calls) == 2

    def test_notify_session_listeners_direct(self, di_sensor):
        hass = _hass_with_meter(0.0, "L")
        zone = _make_zone(di_sensor, hass, flow_rate=5.0)
        calls = []
        zone.register_session_listener(lambda: calls.append(1))
        zone.notify_session_listeners()
        assert calls == [1]
