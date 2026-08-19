"""Tests for ZoneFlowRateSensor, ZoneDurationSensor and ZoneLastDurationSensor."""

from datetime import datetime
from unittest.mock import MagicMock

import pytest
from never_dry.const import (
    CONF_ZONE_AREA,
    CONF_ZONE_EFFICIENCY,
    CONF_ZONE_FLOW_RATE,
    CONF_ZONE_NAME,
    CONF_ZONE_SYSTEM_TYPE,
    CONF_ZONE_THRESHOLD,
    CONF_ZONE_VALVE,
    SYSTEM_TYPE_CUSTOM,
)
from never_dry.sensor import (
    IrrigationZoneSensor,
    ZoneDurationSensor,
    ZoneFlowRateSensor,
    ZoneLastDurationSensor,
)


def _make_hass():
    hass = MagicMock()
    hass.config = MagicMock()
    hass.config.latitude = 45.0
    return hass


def _make_imperial_hass():
    """Hass whose unit system reports gallons as the volume unit (US-customary)."""
    hass = MagicMock()
    hass.config.units.volume_unit = "gal"
    return hass


def _make_zone(di_sensor, name="Orto", flow_rate=8.0, area=20.0, efficiency=0.90):
    zone_config = {
        CONF_ZONE_NAME: name,
        CONF_ZONE_VALVE: "switch.valve",
        CONF_ZONE_AREA: area,
        CONF_ZONE_SYSTEM_TYPE: SYSTEM_TYPE_CUSTOM,
        CONF_ZONE_EFFICIENCY: efficiency,
        CONF_ZONE_FLOW_RATE: flow_rate,
        CONF_ZONE_THRESHOLD: 15.0,
    }
    return IrrigationZoneSensor(_make_hass(), zone_config, di_sensor)


class TestZoneFlowRateSensor:
    def test_name(self, di_sensor):
        zone = _make_zone(di_sensor)
        sensor = ZoneFlowRateSensor(zone)
        assert sensor._attr_name == "Design flow rate"

    def test_unit_metric(self, di_sensor):
        zone = _make_zone(di_sensor)
        sensor = ZoneFlowRateSensor(zone)
        # No hass attached → defaults to metric.
        assert sensor.native_unit_of_measurement == "L/h"

    def test_icon(self, di_sensor):
        zone = _make_zone(di_sensor)
        sensor = ZoneFlowRateSensor(zone)
        assert sensor._attr_icon == "mdi:gauge"

    def test_unique_id(self, di_sensor):
        zone = _make_zone(di_sensor, name="Giardino Orto")
        sensor = ZoneFlowRateSensor(zone)
        assert sensor._attr_unique_id == "flow_rate_zone_giardino_orto"

    def test_value(self, di_sensor):
        zone = _make_zone(di_sensor, flow_rate=3.33)
        sensor = ZoneFlowRateSensor(zone)
        assert sensor.native_value == pytest.approx(199.8, abs=0.1)

    def test_value_rounded(self, di_sensor):
        zone = _make_zone(di_sensor, flow_rate=8.0)
        sensor = ZoneFlowRateSensor(zone)
        assert sensor.native_value == 480.0

    def test_unit_imperial(self, di_sensor):
        """US-customary → gal/h (HA does not auto-convert volume_flow_rate)."""
        zone = _make_zone(di_sensor)
        sensor = ZoneFlowRateSensor(zone)
        sensor.hass = _make_imperial_hass()
        assert sensor.native_unit_of_measurement == "gal/h"

    def test_value_imperial(self, di_sensor):
        """8 L/min = 480 L/h = 126.8 gal/h (1 US gal = 3.785411784 L)."""
        zone = _make_zone(di_sensor, flow_rate=8.0)
        sensor = ZoneFlowRateSensor(zone)
        sensor.hass = _make_imperial_hass()
        assert sensor.native_value == pytest.approx(126.8, abs=0.1)


class TestZoneDurationSensor:
    def test_name(self, di_sensor):
        zone = _make_zone(di_sensor)
        sensor = ZoneDurationSensor(zone)
        assert sensor._attr_name == "Duration"

    def test_unit(self, di_sensor):
        zone = _make_zone(di_sensor)
        sensor = ZoneDurationSensor(zone)
        assert sensor._attr_native_unit_of_measurement == "s"

    def test_icon(self, di_sensor):
        zone = _make_zone(di_sensor)
        sensor = ZoneDurationSensor(zone)
        assert sensor._attr_icon == "mdi:timer"

    def test_unique_id(self, di_sensor):
        zone = _make_zone(di_sensor, name="Giardino Melino")
        sensor = ZoneDurationSensor(zone)
        assert sensor._attr_unique_id == "duration_zone_giardino_melino"

    def test_device_info_assigned(self, di_sensor):
        from homeassistant.helpers.device_registry import DeviceInfo

        zone = _make_zone(di_sensor)
        device = DeviceInfo(identifiers={("never_dry", "orto")})
        sensor = ZoneDurationSensor(zone, device)
        assert sensor._attr_device_info is device

    def test_initial_value_zero_when_no_deficit(self, di_sensor):
        zone = _make_zone(di_sensor)
        sensor = ZoneDurationSensor(zone)
        assert sensor.native_value == 0

    def test_value_with_deficit(self, di_sensor):
        # volume = deficit * area / efficiency = 10 * 20 / 0.90 ≈ 222.2 L
        # duration = round(222.2 / 8 * 60) = 1667 s
        zone = _make_zone(di_sensor, flow_rate=8.0, area=20.0, efficiency=0.90)
        zone._zone_deficit = 10.0
        sensor = ZoneDurationSensor(zone)
        assert sensor.native_value == pytest.approx(1667, abs=2)

    def test_value_tracks_deficit_changes(self, di_sensor):
        zone = _make_zone(di_sensor)
        sensor = ZoneDurationSensor(zone)
        zone._zone_deficit = 5.0
        v1 = sensor.native_value
        zone._zone_deficit = 10.0
        v2 = sensor.native_value
        assert v2 > v1

    def test_registers_dryness_listener(self, di_sensor):
        zone = _make_zone(di_sensor)
        initial_count = len(di_sensor._zone_listeners)
        ZoneDurationSensor(zone)
        assert len(di_sensor._zone_listeners) == initial_count + 1

    def test_on_update_writes_state_when_hass_set(self, di_sensor):
        zone = _make_zone(di_sensor)
        sensor = ZoneDurationSensor(zone)
        sensor.hass = MagicMock()
        sensor.async_write_ha_state = MagicMock()
        sensor._on_update(1.0, 0.5, 0.0)
        sensor.async_write_ha_state.assert_called_once()

    def test_on_update_no_write_without_hass(self, di_sensor):
        zone = _make_zone(di_sensor)
        sensor = ZoneDurationSensor(zone)
        sensor.async_write_ha_state = MagicMock()
        sensor._on_update(1.0, 0.5, 0.0)
        sensor.async_write_ha_state.assert_not_called()


class TestZoneLastDurationSensor:
    def test_name(self, di_sensor):
        zone = _make_zone(di_sensor)
        sensor = ZoneLastDurationSensor(zone)
        assert sensor._attr_name == "Last duration"

    def test_unit(self, di_sensor):
        zone = _make_zone(di_sensor)
        sensor = ZoneLastDurationSensor(zone)
        assert sensor._attr_native_unit_of_measurement == "s"

    def test_icon(self, di_sensor):
        zone = _make_zone(di_sensor)
        sensor = ZoneLastDurationSensor(zone)
        assert sensor._attr_icon == "mdi:timer"

    def test_unique_id(self, di_sensor):
        zone = _make_zone(di_sensor, name="Giardino Pino")
        sensor = ZoneLastDurationSensor(zone)
        assert sensor._attr_unique_id == "last_duration_zone_giardino_pino"

    def test_device_info_assigned(self, di_sensor):
        from homeassistant.helpers.device_registry import DeviceInfo

        zone = _make_zone(di_sensor)
        device = DeviceInfo(identifiers={("never_dry", "orto")})
        sensor = ZoneLastDurationSensor(zone, device)
        assert sensor._attr_device_info is device

    def test_none_before_first_irrigation(self, di_sensor):
        zone = _make_zone(di_sensor)
        sensor = ZoneLastDurationSensor(zone)
        assert sensor.native_value is None

    def test_value_after_irrigation(self, di_sensor):
        zone = _make_zone(di_sensor)
        zone._last_irrigated = datetime.now()
        zone._last_session_duration_s = 270
        sensor = ZoneLastDurationSensor(zone)
        assert sensor.native_value == 270

    def test_value_tracks_zone(self, di_sensor):
        zone = _make_zone(di_sensor)
        zone._last_irrigated = datetime.now()
        zone._last_session_duration_s = 180
        sensor = ZoneLastDurationSensor(zone)
        assert sensor.native_value == 180
        zone._last_session_duration_s = 360
        assert sensor.native_value == 360

    def test_registers_dryness_listener(self, di_sensor):
        zone = _make_zone(di_sensor)
        initial_count = len(di_sensor._zone_listeners)
        ZoneLastDurationSensor(zone)
        assert len(di_sensor._zone_listeners) == initial_count + 1

    def test_on_update_writes_state_when_hass_set(self, di_sensor):
        zone = _make_zone(di_sensor)
        sensor = ZoneLastDurationSensor(zone)
        sensor.hass = MagicMock()
        sensor.async_write_ha_state = MagicMock()
        sensor._on_update(1.0, 0.5, 0.0)
        sensor.async_write_ha_state.assert_called_once()

    def test_on_update_no_write_without_hass(self, di_sensor):
        zone = _make_zone(di_sensor)
        sensor = ZoneLastDurationSensor(zone)
        sensor.async_write_ha_state = MagicMock()
        sensor._on_update(1.0, 0.5, 0.0)
        sensor.async_write_ha_state.assert_not_called()
