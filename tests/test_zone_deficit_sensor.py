"""Tests for ZoneDeficitSensor and zone deficit seeding."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

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
    ZoneDeficitSensor,
    ZoneRainSensor,
    ZoneYearlyWaterSensor,
)


def _make_hass():
    hass = MagicMock()
    hass.config = MagicMock()
    hass.config.latitude = 45.0
    return hass


def _make_zone(di_sensor, name="Orto", area=20.0, efficiency=0.90):
    zone_config = {
        CONF_ZONE_NAME: name,
        CONF_ZONE_VALVE: "switch.valve",
        CONF_ZONE_AREA: area,
        CONF_ZONE_SYSTEM_TYPE: SYSTEM_TYPE_CUSTOM,
        CONF_ZONE_EFFICIENCY: efficiency,
        CONF_ZONE_FLOW_RATE: 8.0,
        CONF_ZONE_THRESHOLD: 15.0,
    }
    return IrrigationZoneSensor(_make_hass(), zone_config, di_sensor)


class TestZoneDeficitSensorProperties:
    """Test ZoneDeficitSensor entity attributes."""

    def test_name(self, di_sensor):
        zone = _make_zone(di_sensor)
        deficit = ZoneDeficitSensor(zone)
        assert deficit._attr_name == "Deficit"

    def test_unique_id(self, di_sensor):
        zone = _make_zone(di_sensor, name="Giardino Melino")
        deficit = ZoneDeficitSensor(zone)
        assert deficit._attr_unique_id == "deficit_zone_giardino_melino"

    def test_unit(self, di_sensor):
        zone = _make_zone(di_sensor)
        deficit = ZoneDeficitSensor(zone)
        assert deficit._attr_native_unit_of_measurement == "mm"

    def test_icon(self, di_sensor):
        zone = _make_zone(di_sensor)
        deficit = ZoneDeficitSensor(zone)
        assert deficit._attr_icon == "mdi:water-percent-alert"

    def test_has_entity_name(self, di_sensor):
        zone = _make_zone(di_sensor)
        deficit = ZoneDeficitSensor(zone)
        assert deficit._attr_has_entity_name is True

    def test_device_info(self, di_sensor):
        zone = _make_zone(di_sensor)
        from homeassistant.helpers.device_registry import DeviceInfo

        device = DeviceInfo(identifiers={("never_dry", "test_orto")})
        deficit = ZoneDeficitSensor(zone, device)
        assert deficit._attr_device_info is device


class TestZoneDeficitSensorValue:
    """Test deficit value tracks zone deficit."""

    def test_initial_zero(self, di_sensor):
        zone = _make_zone(di_sensor)
        deficit = ZoneDeficitSensor(zone)
        assert deficit.native_value == 0.0

    def test_tracks_zone_deficit(self, di_sensor):
        zone = _make_zone(di_sensor)
        deficit = ZoneDeficitSensor(zone)
        zone._zone_deficit = 5.67
        assert deficit.native_value == 5.67

    def test_updates_on_et_broadcast(self, di_sensor):
        zone = _make_zone(di_sensor)
        deficit = ZoneDeficitSensor(zone)
        # Simulate ET broadcast
        zone._on_et_update(1.0, 2.0, 0.0)
        # Zone deficit should have increased
        assert deficit.native_value > 0

    def test_rounds_to_two_decimals(self, di_sensor):
        zone = _make_zone(di_sensor)
        deficit = ZoneDeficitSensor(zone)
        zone._zone_deficit = 3.14159
        assert deficit.native_value == 3.14

    def test_after_reset(self, di_sensor):
        zone = _make_zone(di_sensor)
        deficit = ZoneDeficitSensor(zone)
        zone._zone_deficit = 10.0
        zone.reset_deficit()
        assert deficit.native_value == 0.0


class TestZoneDeficitSeeding:
    """A new zone (no restored state) starts at zero — it does NOT inherit the
    global reference deficit. That global drifts high under per-zone irrigation
    (it only resets when ALL zones are irrigated together) and gave new zones a
    spurious 'irrigation due' (#123, Rasen 2.1). See
    docs/design_water_balance_reference_model.md (D4)."""

    @pytest.mark.asyncio
    async def test_new_zone_starts_at_zero_ignoring_dryness_index(self, di_sensor):
        """New zone (no restore) starts at 0 even when the global DI is high."""
        zone = _make_zone(di_sensor)
        di_sensor._deficit = 8.0  # inflated global — must NOT leak into the zone
        zone.async_get_last_state = AsyncMock(return_value=None)
        await zone.async_added_to_hass()
        assert zone._zone_deficit == 0.0

    @pytest.mark.asyncio
    async def test_restore_overrides_seed(self, di_sensor):
        """Restored state is used instead of the zero start."""
        zone = _make_zone(di_sensor)
        di_sensor._deficit = 8.0
        last_state = MagicMock()
        last_state.attributes = {"deficit_mm": "3.5"}
        zone.async_get_last_state = AsyncMock(return_value=last_state)
        await zone.async_added_to_hass()
        assert zone._zone_deficit == pytest.approx(3.5, abs=0.01)


class TestZoneDeficitSensorAttributes:
    """Test extra_state_attributes on ZoneDeficitSensor."""

    def test_flow_rate_always_present(self, di_sensor):
        zone = _make_zone(di_sensor)
        deficit = ZoneDeficitSensor(zone)
        assert deficit.extra_state_attributes["flow_rate_lpm"] == 8.0

    def test_irrigating_always_present(self, di_sensor):
        zone = _make_zone(di_sensor)
        deficit = ZoneDeficitSensor(zone)
        assert deficit.extra_state_attributes["irrigating"] is False

    def test_last_session_duration_absent_before_irrigation(self, di_sensor):
        zone = _make_zone(di_sensor)
        deficit = ZoneDeficitSensor(zone)
        assert "last_session_duration_s" not in deficit.extra_state_attributes

    def test_last_session_duration_present_after_irrigation(self, di_sensor):
        from datetime import datetime

        zone = _make_zone(di_sensor)
        zone._last_irrigated = datetime.now()
        zone._last_session_duration_s = 120
        deficit = ZoneDeficitSensor(zone)
        assert deficit.extra_state_attributes["last_session_duration_s"] == 120

    def test_fsm_state_absent_without_operator(self, di_sensor):
        zone = _make_zone(di_sensor)
        deficit = ZoneDeficitSensor(zone)
        attrs = deficit.extra_state_attributes
        assert "valve_fsm_state" not in attrs
        assert "valve_in_maintenance" not in attrs

    def test_fsm_state_present_with_operator(self, di_sensor):
        zone = _make_zone(di_sensor)
        op = MagicMock()
        op.state.value = "idle"
        op.is_in_maintenance = False
        zone._operator = op
        deficit = ZoneDeficitSensor(zone)
        attrs = deficit.extra_state_attributes
        assert attrs["valve_fsm_state"] == "idle"
        assert attrs["valve_in_maintenance"] is False

    def test_fsm_maintenance_state(self, di_sensor):
        zone = _make_zone(di_sensor)
        op = MagicMock()
        op.state.value = "maintenance"
        op.is_in_maintenance = True
        zone._operator = op
        deficit = ZoneDeficitSensor(zone)
        attrs = deficit.extra_state_attributes
        assert attrs["valve_fsm_state"] == "maintenance"
        assert attrs["valve_in_maintenance"] is True


class TestYearlyRain:
    """Rain is a system yearly total [mm]; each zone shows it in liters via its
    own area (mm x m2 = L), and Water Yearly = rain + irrigation.
    See docs/design_water_balance_reference_model.md (D3)."""

    def test_accrue_and_year_reset(self, di_sensor):
        di_sensor._yearly_rain = 0.0
        di_sensor._yearly_rain_year = datetime.now().year
        di_sensor._accrue_yearly_rain(3.0)
        di_sensor._accrue_yearly_rain(2.0)
        assert di_sensor._yearly_rain == pytest.approx(5.0)
        # A stale year → the next accrual resets before adding.
        di_sensor._yearly_rain_year -= 1
        di_sensor._accrue_yearly_rain(1.0)
        assert di_sensor._yearly_rain == pytest.approx(1.0)
        assert di_sensor._yearly_rain_year == datetime.now().year

    def test_rain_yearly_is_liters_via_area(self, di_sensor):
        di_sensor._yearly_rain = 4.0  # mm
        zone = _make_zone(di_sensor, area=50.0)
        rain = ZoneRainSensor(zone)
        assert rain.native_value == pytest.approx(200.0)  # 4 mm x 50 m2 = 200 L

    def test_all_zones_same_mm_but_liters_scale_with_area(self, di_sensor):
        di_sensor._yearly_rain = 5.0
        small = ZoneRainSensor(_make_zone(di_sensor, name="Small", area=10.0))
        big = ZoneRainSensor(_make_zone(di_sensor, name="Big", area=40.0))
        assert small.native_value == pytest.approx(50.0)
        assert big.native_value == pytest.approx(200.0)

    def test_irrigated_yearly_is_irrigation_only(self, di_sensor):
        """Irrigated Yearly is the delivered irrigation only — rain excluded
        (WATER device_class = consumption; rain is Rain Yearly)."""
        di_sensor._yearly_rain = 4.0  # must NOT be added
        zone = _make_zone(di_sensor, area=50.0)
        zone._yearly_water_delivered = 120.0
        water = ZoneYearlyWaterSensor(zone)
        assert water.native_value == pytest.approx(120.0)
