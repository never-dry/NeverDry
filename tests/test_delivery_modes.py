"""Tests for the three valve delivery modes."""

from unittest.mock import AsyncMock, MagicMock

import pytest
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
    DEFAULT_DELIVERY_TIMEOUT_S,
    DELIVERY_DURATION_MARGIN,
    DELIVERY_MODE_ESTIMATED_FLOW,
    DELIVERY_MODE_FLOW_METER,
    DELIVERY_MODE_VOLUME_PRESET,
    FLOW_METER_POLL_INTERVAL_S,
    SYSTEM_TYPE_CUSTOM,
)
from never_dry.controller import IrrigationController
from never_dry.sensor import IrrigationZoneSensor


def _make_zone(hass_mock, di_sensor, **overrides):
    """Create a zone sensor with given overrides."""
    config = {
        CONF_ZONE_NAME: "TestZone",
        CONF_ZONE_VALVE: "switch.valve_test",
        CONF_ZONE_AREA: 20.0,
        CONF_ZONE_SYSTEM_TYPE: SYSTEM_TYPE_CUSTOM,
        CONF_ZONE_EFFICIENCY: 0.90,
        CONF_ZONE_FLOW_RATE: 8.0,
        CONF_ZONE_DELIVERY_MODE: DELIVERY_MODE_ESTIMATED_FLOW,
    }
    config.update(overrides)
    return IrrigationZoneSensor(hass_mock, config, di_sensor)


class TestEstimatedFlowDelivery:
    """Test estimated_flow delivery mode (existing behavior)."""

    @pytest.mark.asyncio
    async def test_opens_waits_closes(self, hass_mock, di_sensor):
        zone = _make_zone(hass_mock, di_sensor)
        zone._zone_deficit = 5.0
        ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)
        ctrl._wait_with_stop_check = AsyncMock(side_effect=lambda d, **kwargs: d)

        await ctrl._deliver_estimated_flow(zone)

        # Valve should have been opened and closed
        calls = hass_mock.services.async_call.call_args_list
        assert any("turn_on" in str(c) for c in calls)
        assert any("turn_off" in str(c) for c in calls)

    @pytest.mark.asyncio
    async def test_skips_zero_duration(self, hass_mock, di_sensor):
        zone = _make_zone(hass_mock, di_sensor)
        zone._zone_deficit = 0.0
        ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)

        result = await ctrl._deliver_estimated_flow(zone)

        assert result == 0.0
        hass_mock.services.async_call.assert_not_called()

    def test_default_delivery_mode(self, hass_mock, di_sensor):
        """Zone without explicit delivery_mode defaults to estimated_flow."""
        zone = IrrigationZoneSensor(
            hass_mock,
            {
                CONF_ZONE_NAME: "Default",
                CONF_ZONE_VALVE: "switch.valve",
                CONF_ZONE_AREA: 10.0,
                CONF_ZONE_FLOW_RATE: 5.0,
            },
            di_sensor,
        )
        assert zone.delivery_mode == DELIVERY_MODE_ESTIMATED_FLOW


class TestVolumePresetDelivery:
    """Test volume_preset delivery mode."""

    @pytest.mark.asyncio
    async def test_sends_volume_to_number_entity(self, hass_mock, di_sensor):
        zone = _make_zone(
            hass_mock,
            di_sensor,
            **{
                CONF_ZONE_DELIVERY_MODE: DELIVERY_MODE_VOLUME_PRESET,
                CONF_ZONE_VOLUME_ENTITY: "number.valve_volume",
                CONF_ZONE_DELIVERY_TIMEOUT: 10,
            },
        )
        zone._zone_deficit = 5.0

        # Simulate valve closing itself after set_value
        valve_state = MagicMock()
        valve_state.state = "off"
        hass_mock.states.get = MagicMock(return_value=valve_state)

        ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)
        result = await ctrl._deliver_volume_preset(zone)

        assert result > 0
        # Check number.set_value was called
        set_value_calls = [
            c
            for c in hass_mock.services.async_call.call_args_list
            if c.args[0] == "number" and c.args[1] == "set_value"
        ]
        assert len(set_value_calls) == 1
        assert set_value_calls[0].args[2]["entity_id"] == "number.valve_volume"

    @pytest.mark.asyncio
    async def test_timeout_forces_close(self, hass_mock, di_sensor):
        zone = _make_zone(
            hass_mock,
            di_sensor,
            **{
                CONF_ZONE_DELIVERY_MODE: DELIVERY_MODE_VOLUME_PRESET,
                CONF_ZONE_VOLUME_ENTITY: "number.valve_volume",
                CONF_ZONE_DELIVERY_TIMEOUT: FLOW_METER_POLL_INTERVAL_S,  # very short timeout
            },
        )
        # AI-150: delivery_timeout scales with the guard-flow duration; keep the
        # deficit tiny so the configured floor stays the effective timeout.
        zone._zone_deficit = 0.01
        assert zone.delivery_timeout == FLOW_METER_POLL_INTERVAL_S

        # Valve never closes itself
        valve_state = MagicMock()
        valve_state.state = "on"
        hass_mock.states.get = MagicMock(return_value=valve_state)

        ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)
        result = await ctrl._deliver_volume_preset(zone)

        assert result > 0
        # Valve should be force-closed
        close_calls = [c for c in hass_mock.services.async_call.call_args_list if "turn_off" in str(c)]
        assert len(close_calls) >= 1

    @pytest.mark.asyncio
    async def test_no_volume_entity_returns_false(self, hass_mock, di_sensor):
        zone = _make_zone(
            hass_mock,
            di_sensor,
            **{CONF_ZONE_DELIVERY_MODE: DELIVERY_MODE_VOLUME_PRESET},
        )
        zone._zone_deficit = 5.0

        ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)
        result = await ctrl._deliver_volume_preset(zone)

        assert result == 0.0

    @pytest.mark.asyncio
    async def test_stop_during_preset(self, hass_mock, di_sensor):
        zone = _make_zone(
            hass_mock,
            di_sensor,
            **{
                CONF_ZONE_DELIVERY_MODE: DELIVERY_MODE_VOLUME_PRESET,
                CONF_ZONE_VOLUME_ENTITY: "number.valve_volume",
                CONF_ZONE_DELIVERY_TIMEOUT: 100,
            },
        )
        zone._zone_deficit = 5.0

        valve_state = MagicMock()
        valve_state.state = "on"
        hass_mock.states.get = MagicMock(return_value=valve_state)

        ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)
        ctrl._stop_requested = True

        result = await ctrl._deliver_volume_preset(zone)

        assert result == 0.0


class TestFlowMeterDelivery:
    """Test flow_meter delivery mode."""

    @pytest.mark.asyncio
    async def test_closes_at_target_volume(self, hass_mock, di_sensor):
        zone = _make_zone(
            hass_mock,
            di_sensor,
            **{
                CONF_ZONE_DELIVERY_MODE: DELIVERY_MODE_FLOW_METER,
                CONF_ZONE_FLOW_METER_SENSOR: "sensor.flow_meter",
                CONF_ZONE_DELIVERY_TIMEOUT: 100,
            },
        )
        zone._zone_deficit = 5.0
        target_volume = zone.volume_liters

        # Simulate flow meter: starts at 100, ends at 100 + target (cumulative L)
        readings = iter([100.0, 100.0, 100.0 + target_volume + 1])
        meter_state = MagicMock()
        meter_state.attributes = {"unit_of_measurement": "L"}

        def get_state(entity_id):
            if entity_id == "sensor.flow_meter":
                meter_state.state = str(next(readings))
                return meter_state
            return None

        hass_mock.states.get = MagicMock(side_effect=get_state)

        ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)
        result = await ctrl._deliver_flow_meter(zone)

        assert result > 0
        # Valve should have been opened and closed
        close_calls = [c for c in hass_mock.services.async_call.call_args_list if "turn_off" in str(c)]
        assert len(close_calls) >= 1

    @pytest.mark.asyncio
    async def test_unavailable_sensor_skips(self, hass_mock, di_sensor):
        zone = _make_zone(
            hass_mock,
            di_sensor,
            **{
                CONF_ZONE_DELIVERY_MODE: DELIVERY_MODE_FLOW_METER,
                CONF_ZONE_FLOW_METER_SENSOR: "sensor.flow_meter",
            },
        )
        zone._zone_deficit = 5.0

        unavailable = MagicMock()
        unavailable.state = "unavailable"
        hass_mock.states.get = MagicMock(return_value=unavailable)

        ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)
        result = await ctrl._deliver_flow_meter(zone)

        assert result == 0.0
        # No valve should have been opened
        open_calls = [c for c in hass_mock.services.async_call.call_args_list if "turn_on" in str(c)]
        assert len(open_calls) == 0

    @pytest.mark.asyncio
    async def test_no_flow_meter_entity_returns_false(self, hass_mock, di_sensor):
        zone = _make_zone(
            hass_mock,
            di_sensor,
            **{CONF_ZONE_DELIVERY_MODE: DELIVERY_MODE_FLOW_METER},
        )
        zone._zone_deficit = 5.0

        ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)
        result = await ctrl._deliver_flow_meter(zone)

        assert result == 0.0

    @pytest.mark.asyncio
    async def test_meter_reset_adjusts_baseline(self, hass_mock, di_sensor):
        zone = _make_zone(
            hass_mock,
            di_sensor,
            **{
                CONF_ZONE_DELIVERY_MODE: DELIVERY_MODE_FLOW_METER,
                CONF_ZONE_FLOW_METER_SENSOR: "sensor.flow_meter",
                CONF_ZONE_DELIVERY_TIMEOUT: 100,
            },
        )
        zone._zone_deficit = 5.0
        target_volume = zone.volume_liters

        # Simulate: unit check, initial=100, then meter resets to 50, then reaches target
        readings = iter([100.0, 100.0, 50.0, target_volume + 1])
        meter_state = MagicMock()
        meter_state.attributes = {"unit_of_measurement": "L"}

        def get_state(entity_id):
            if entity_id == "sensor.flow_meter":
                meter_state.state = str(next(readings))
                return meter_state
            return None

        hass_mock.states.get = MagicMock(side_effect=get_state)

        ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)
        result = await ctrl._deliver_flow_meter(zone)

        assert result > 0

    @pytest.mark.asyncio
    async def test_stop_during_flow_meter(self, hass_mock, di_sensor):
        zone = _make_zone(
            hass_mock,
            di_sensor,
            **{
                CONF_ZONE_DELIVERY_MODE: DELIVERY_MODE_FLOW_METER,
                CONF_ZONE_FLOW_METER_SENSOR: "sensor.flow_meter",
                CONF_ZONE_DELIVERY_TIMEOUT: 100,
            },
        )
        zone._zone_deficit = 5.0

        meter_state = MagicMock()
        meter_state.state = "0.0"
        meter_state.attributes = {"unit_of_measurement": "L"}
        hass_mock.states.get = MagicMock(return_value=meter_state)

        ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)
        ctrl._stop_requested = True

        result = await ctrl._deliver_flow_meter(zone)

        assert result == 0.0

    @pytest.mark.asyncio
    async def test_stop_zone_ends_flow_meter(self, hass_mock, di_sensor):
        """A per-zone stop request aborts the flow_meter loop like the global stop."""
        zone = _make_zone(
            hass_mock,
            di_sensor,
            **{
                CONF_ZONE_DELIVERY_MODE: DELIVERY_MODE_FLOW_METER,
                CONF_ZONE_FLOW_METER_SENSOR: "sensor.flow_meter",
                CONF_ZONE_DELIVERY_TIMEOUT: 100,
            },
        )
        zone._zone_deficit = 5.0

        meter_state = MagicMock()
        meter_state.state = "0.0"
        meter_state.attributes = {"unit_of_measurement": "L"}
        hass_mock.states.get = MagicMock(return_value=meter_state)

        ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)
        ctrl._stop_zone = zone.zone_name

        result = await ctrl._deliver_flow_meter(zone)

        assert result == 0.0

    @pytest.mark.asyncio
    async def test_stop_during_flow_rate(self, hass_mock, di_sensor):
        """A stop request aborts the flow_rate loop and closes the valve."""
        zone = _make_zone(
            hass_mock,
            di_sensor,
            **{
                CONF_ZONE_DELIVERY_MODE: DELIVERY_MODE_FLOW_METER,
                CONF_ZONE_FLOW_METER_SENSOR: "sensor.flow_rate",
                CONF_ZONE_DELIVERY_TIMEOUT: 100,
            },
        )
        zone._zone_deficit = 5.0

        meter_state = MagicMock()
        meter_state.state = "10.0"
        meter_state.attributes = {"unit_of_measurement": "L/min"}
        hass_mock.states.get = MagicMock(return_value=meter_state)

        ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)
        ctrl._stop_requested = True

        result = await ctrl._deliver_flow_rate(zone, "sensor.flow_rate", 100.0)

        # Stopped before any integration step: nothing delivered, valve closed.
        assert result == 0.0
        close_calls = [c for c in hass_mock.services.async_call.call_args_list if "turn_off" in str(c)]
        assert len(close_calls) >= 1

    @pytest.mark.asyncio
    async def test_external_close_ends_flow_rate(self, hass_mock, di_sensor):
        """Flow-rate delivery ends as soon as the valve switch reads 'off'
        (hardware auto-close) rather than integrating for the full timeout."""
        zone = _make_zone(
            hass_mock,
            di_sensor,
            **{
                CONF_ZONE_DELIVERY_MODE: DELIVERY_MODE_FLOW_METER,
                CONF_ZONE_FLOW_METER_SENSOR: "sensor.flow_rate",
                CONF_ZONE_DELIVERY_TIMEOUT: 10,
            },
        )
        zone._zone_deficit = 50.0

        polls = {"valve": 0}

        def get_state(entity_id):
            if entity_id == "sensor.flow_rate":
                s = MagicMock()
                s.state = "10.0"
                s.attributes = {"unit_of_measurement": "L/min"}
                return s
            if entity_id == zone.valve:
                polls["valve"] += 1
                s = MagicMock()
                # On for the first poll, then the hardware auto-closes.
                s.state = "on" if polls["valve"] <= 1 else "off"
                return s
            return None

        hass_mock.states.get = MagicMock(side_effect=get_state)

        ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)
        # Large target so the loop would otherwise run to the timeout.
        result = await ctrl._deliver_flow_rate(zone, "sensor.flow_rate", 1000.0)

        # Some water was credited before the valve closed, and the loop exited
        # early instead of polling all timeout/poll_interval iterations.
        assert result > 0
        assert polls["valve"] <= 3
        close_calls = [c for c in hass_mock.services.async_call.call_args_list if "turn_off" in str(c)]
        assert len(close_calls) >= 1


class TestDeliveryModeDispatch:
    """Test the _deliver_water dispatch method."""

    @pytest.mark.asyncio
    async def test_dispatches_estimated_flow(self, hass_mock, di_sensor):
        zone = _make_zone(hass_mock, di_sensor)
        zone._zone_deficit = 5.0
        ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)
        ctrl._wait_with_stop_check = AsyncMock(side_effect=lambda d, **kwargs: d)

        result = await ctrl._deliver_water(zone)

        assert result > 0

    @pytest.mark.asyncio
    async def test_unknown_mode_returns_false(self, hass_mock, di_sensor):
        zone = _make_zone(hass_mock, di_sensor)
        zone._delivery_mode = "nonexistent_mode"
        zone._zone_deficit = 5.0
        ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)

        result = await ctrl._deliver_water(zone)

        assert result == 0.0


class TestDurationByMode:
    """Test that duration_s returns 0 for non-estimated_flow modes."""

    def test_estimated_flow_has_duration(self, hass_mock, di_sensor):
        zone = _make_zone(hass_mock, di_sensor)
        zone._zone_deficit = 5.0
        assert zone.duration_s > 0

    def test_flow_meter_guard_duration(self, hass_mock, di_sensor):
        """AI-150: flow_meter zones estimate duration from the guard flow rate."""
        zone = _make_zone(
            hass_mock,
            di_sensor,
            **{CONF_ZONE_DELIVERY_MODE: DELIVERY_MODE_FLOW_METER},
        )
        zone._zone_deficit = 5.0
        assert zone.duration_s == round(zone.volume_liters / 8.0 * 60)

    def test_volume_preset_guard_duration(self, hass_mock, di_sensor):
        """AI-150: volume_preset zones estimate duration from the guard flow rate."""
        zone = _make_zone(
            hass_mock,
            di_sensor,
            **{CONF_ZONE_DELIVERY_MODE: DELIVERY_MODE_VOLUME_PRESET},
        )
        zone._zone_deficit = 5.0
        assert zone.duration_s == round(zone.volume_liters / 8.0 * 60)


class TestDeliveryModeAttributes:
    """Test delivery mode in zone state attributes."""

    def test_delivery_mode_in_attributes(self, hass_mock, di_sensor):
        zone = _make_zone(hass_mock, di_sensor)
        assert zone.extra_state_attributes["delivery_mode"] == DELIVERY_MODE_ESTIMATED_FLOW

    def test_volume_entity_in_attributes(self, hass_mock, di_sensor):
        zone = _make_zone(
            hass_mock,
            di_sensor,
            **{
                CONF_ZONE_DELIVERY_MODE: DELIVERY_MODE_VOLUME_PRESET,
                CONF_ZONE_VOLUME_ENTITY: "number.valve_vol",
            },
        )
        attrs = zone.extra_state_attributes
        assert attrs["volume_entity"] == "number.valve_vol"
        assert "delivery_timeout_s" in attrs

    def test_flow_meter_in_attributes(self, hass_mock, di_sensor):
        zone = _make_zone(
            hass_mock,
            di_sensor,
            **{
                CONF_ZONE_DELIVERY_MODE: DELIVERY_MODE_FLOW_METER,
                CONF_ZONE_FLOW_METER_SENSOR: "sensor.flow",
            },
        )
        attrs = zone.extra_state_attributes
        assert attrs["flow_meter_sensor"] == "sensor.flow"
        assert "delivery_timeout_s" in attrs

    def test_flow_rate_lph_in_attributes(self, hass_mock, di_sensor):
        # Internal storage stays L/min; the UI attribute exposes L/h (x60).
        zone = _make_zone(hass_mock, di_sensor, **{CONF_ZONE_FLOW_RATE: 8.0})
        attrs = zone.extra_state_attributes
        assert attrs["flow_rate_lpm"] == pytest.approx(8.0)
        assert attrs["flow_rate_lph"] == pytest.approx(480.0)


class TestSettleWaterAccounting:
    """Regression: water counters must record the ACTUAL delivered volume.

    Flow-metered delivery depletes ``_zone_deficit`` in real time during the
    cycle. The end-of-cycle settle for a full delivery must therefore credit
    the measured ``delivered`` volume, not ``volume_liters`` recomputed from
    the already-depleted deficit (which would be ~0).
    """

    @pytest.mark.asyncio
    async def test_full_flow_meter_delivery_credits_actual_volume(self, controller, zone_orto):
        zone = zone_orto
        zone._zone_deficit = 5.0
        target = zone.volume_liters  # snapshot taken before delivery
        assert target > 0
        total_before = zone._total_water_delivered

        async def fake_deliver(z):
            # Mimic flow_meter/flow_rate: real-time deficit depletion to ~0.
            controller._update_deficit_realtime(z, target)
            return target

        controller._deliver_water = fake_deliver
        await controller._irrigate_zones(["Orto"])

        assert zone._zone_deficit == 0.0
        assert zone._total_water_delivered == pytest.approx(total_before + target, abs=0.2)
        assert zone._session_water_delivered == pytest.approx(target, abs=0.2)
        assert zone._last_volume_delivered == pytest.approx(target, abs=0.2)

    @pytest.mark.asyncio
    async def test_partial_flow_meter_delivery_credits_actual_volume(self, controller, zone_orto):
        zone = zone_orto
        zone._zone_deficit = 5.0
        target = zone.volume_liters
        partial = target * 0.4

        async def fake_deliver(z):
            controller._update_deficit_realtime(z, partial)
            return partial

        controller._deliver_water = fake_deliver
        await controller._irrigate_zones(["Orto"])

        # Partial: deficit reduced but not zero, counters reflect partial volume.
        assert zone._zone_deficit > 0.0
        assert zone._total_water_delivered == pytest.approx(partial, abs=0.2)
        assert zone._session_water_delivered == pytest.approx(partial, abs=0.2)

    def test_estimated_flow_no_timeout_in_attributes(self, hass_mock, di_sensor):
        zone = _make_zone(hass_mock, di_sensor)
        attrs = zone.extra_state_attributes
        assert "delivery_timeout_s" not in attrs


class TestZeroFlowTimeoutFallback:
    """Regression: delivery timeout with zero measured flow must still settle.

    Field report: the valve stayed open for the whole ``delivery_timeout``
    (~1h), was closed by the timeout, yet the zone deficit was unchanged —
    so the scheduler immediately wanted to irrigate again. Root cause: the
    flow-based modes returned the measured 0.0, ``_irrigate_zones`` only
    settles zones with ``delivered > 0``, and the hour of real watering was
    never credited. The fix estimates the volume from the configured
    nominal flow_rate whenever the sensor measured nothing while the valve
    was open.
    """

    @staticmethod
    def _stuck_states(zone, meter_entity, meter_value, unit):
        """states.get side effect: flow sensor frozen, valve always 'on'."""

        def get_state(entity_id):
            if entity_id == meter_entity:
                s = MagicMock()
                s.state = meter_value
                s.attributes = {"unit_of_measurement": unit}
                return s
            if entity_id == zone.valve:
                s = MagicMock()
                s.state = "on"
                return s
            return None

        return get_state

    @pytest.mark.asyncio
    async def test_timeout_with_dead_flow_meter_settles_deficit(self, hass_mock, di_sensor):
        """End-to-end reproduction of the reported bug via _irrigate_zones.

        AI-150: delivery_timeout now scales with the guard-flow duration, so
        the deficit is kept small enough that the configured floor stays the
        effective timeout (a large deficit would legitimately stretch it).
        The estimated credit at timeout exceeds the small deficit, so the
        settle path fully resets it — the point is that the hour of real
        watering is credited instead of being silently dropped.
        """
        timeout_s = 2 * FLOW_METER_POLL_INTERVAL_S
        zone = _make_zone(
            hass_mock,
            di_sensor,
            **{
                CONF_ZONE_DELIVERY_MODE: DELIVERY_MODE_FLOW_METER,
                CONF_ZONE_FLOW_METER_SENSOR: "sensor.flow_meter",
                CONF_ZONE_DELIVERY_TIMEOUT: timeout_s,
            },
        )
        zone._zone_deficit = 0.02
        assert zone.delivery_timeout == timeout_s  # guard duration below the floor
        hass_mock.states.get = MagicMock(
            side_effect=self._stuck_states(zone, "sensor.flow_meter", "100.0", "L"),
        )

        ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)
        await ctrl._irrigate_zones(["TestZone"])

        # The valve was open for the full timeout at the configured 8 L/min:
        # the settle must credit that water even though the meter read 0.
        expected_liters = 8.0 * timeout_s / 60.0
        assert zone._zone_deficit == 0.0  # estimate covers the small deficit
        assert zone._total_water_delivered == pytest.approx(expected_liters, abs=0.2)
        assert zone._last_irrigated is not None

    @pytest.mark.asyncio
    async def test_flow_meter_timeout_zero_flow_credits_estimate(self, hass_mock, di_sensor):
        timeout_s = 2 * FLOW_METER_POLL_INTERVAL_S
        zone = _make_zone(
            hass_mock,
            di_sensor,
            **{
                CONF_ZONE_DELIVERY_MODE: DELIVERY_MODE_FLOW_METER,
                CONF_ZONE_FLOW_METER_SENSOR: "sensor.flow_meter",
                CONF_ZONE_DELIVERY_TIMEOUT: timeout_s,
            },
        )
        # AI-150: small deficit keeps the configured floor as the effective timeout.
        zone._zone_deficit = 0.02
        assert zone.delivery_timeout == timeout_s
        hass_mock.states.get = MagicMock(
            side_effect=self._stuck_states(zone, "sensor.flow_meter", "100.0", "L"),
        )

        ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)
        result = await ctrl._deliver_flow_meter(zone)

        assert result == pytest.approx(8.0 * timeout_s / 60.0, abs=0.01)

    @pytest.mark.asyncio
    async def test_flow_rate_timeout_zero_flow_credits_estimate(self, hass_mock, di_sensor):
        timeout_s = 2 * FLOW_METER_POLL_INTERVAL_S
        zone = _make_zone(
            hass_mock,
            di_sensor,
            **{
                CONF_ZONE_DELIVERY_MODE: DELIVERY_MODE_FLOW_METER,
                CONF_ZONE_FLOW_METER_SENSOR: "sensor.flow_rate",
                CONF_ZONE_DELIVERY_TIMEOUT: timeout_s,
            },
        )
        # AI-150: small deficit keeps the configured floor as the effective timeout.
        zone._zone_deficit = 0.02
        assert zone.delivery_timeout == timeout_s
        hass_mock.states.get = MagicMock(
            side_effect=self._stuck_states(zone, "sensor.flow_rate", "0.0", "L/min"),
        )

        ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)
        result = await ctrl._deliver_flow_rate(zone, "sensor.flow_rate", 1000.0)

        assert result == pytest.approx(8.0 * timeout_s / 60.0, abs=0.01)

    @pytest.mark.asyncio
    async def test_zero_flow_without_flow_rate_cannot_estimate(self, hass_mock, di_sensor):
        """Without a configured flow_rate there is no basis for an estimate."""
        timeout_s = 2 * FLOW_METER_POLL_INTERVAL_S
        zone = _make_zone(
            hass_mock,
            di_sensor,
            **{
                CONF_ZONE_FLOW_RATE: 0.0,
                CONF_ZONE_DELIVERY_MODE: DELIVERY_MODE_FLOW_METER,
                CONF_ZONE_FLOW_METER_SENSOR: "sensor.flow_meter",
                CONF_ZONE_DELIVERY_TIMEOUT: timeout_s,
            },
        )
        zone._zone_deficit = 5.0
        hass_mock.states.get = MagicMock(
            side_effect=self._stuck_states(zone, "sensor.flow_meter", "100.0", "L"),
        )

        ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)
        result = await ctrl._deliver_flow_meter(zone)

        assert result == 0.0

    @pytest.mark.asyncio
    async def test_measured_flow_wins_over_estimate(self, hass_mock, di_sensor):
        """When the meter DID measure water, the fallback must not replace it."""
        zone = _make_zone(
            hass_mock,
            di_sensor,
            **{
                CONF_ZONE_DELIVERY_MODE: DELIVERY_MODE_FLOW_METER,
                CONF_ZONE_FLOW_METER_SENSOR: "sensor.flow_meter",
                CONF_ZONE_DELIVERY_TIMEOUT: 100,
            },
        )
        ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)

        assert ctrl._fallback_volume_estimate(zone, 3600, 42.0) == 42.0


class TestStalledFlowMeter:
    """GH #173, second report: the meter counts once, then stops.

    The reporter's meter registered 1 L about a minute in — enough for the run
    to start — and never incremented again. The valve stayed open until its
    *hardware* auto-shutoff fired, an hour later, on a zone with five minutes
    of work to do. His words: on a valve without one, "that could be very bad".

    The bound now comes from the job (volume over the declared flow rate, times
    a margin) instead of a constant that could only ever loosen it.
    """

    # The reporter's zone: 18.9 gal/h ≈ 1.19 L/min, 5.6 L target.
    FLOW_LPM = 1.19
    VOLUME_L = 5.6
    AREA_M2 = 20.0
    EFFICIENCY = 0.9

    def _zone(self, hass_mock, di_sensor):
        zone = _make_zone(
            hass_mock,
            di_sensor,
            **{
                CONF_ZONE_AREA: self.AREA_M2,
                CONF_ZONE_EFFICIENCY: self.EFFICIENCY,
                CONF_ZONE_FLOW_RATE: self.FLOW_LPM,
                CONF_ZONE_DELIVERY_MODE: DELIVERY_MODE_FLOW_METER,
                CONF_ZONE_FLOW_METER_SENSOR: "sensor.flow_meter",
            },
        )
        # deficit chosen so the target volume is the reporter's 5.6 L
        zone._zone_deficit = self.VOLUME_L * self.EFFICIENCY / self.AREA_M2
        return zone

    @staticmethod
    def _one_litre_then_stall(zone, meter_entity):
        """states.get: a totalizer that counts a single litre, then freezes.

        The single litre lands about a minute in, as reported — a minute is
        ~30 polls at the 2 s interval — and the reading never grows again.
        Counting reads rather than scripting them keeps the fake honest: the
        controller reads the meter twice before the loop even starts (once to
        detect the sensor type, once for the baseline).
        """
        reads = {"n": 0}

        def get_state(entity_id):
            if entity_id == meter_entity:
                reads["n"] += 1
                s = MagicMock()
                s.state = "100.0" if reads["n"] <= 30 else "101.0"
                s.attributes = {"unit_of_measurement": "L"}
                return s
            if entity_id == zone.valve:
                s = MagicMock()
                s.state = "on"
                return s
            return None

        return get_state

    def test_the_bound_follows_the_job(self, hass_mock, di_sensor):
        """The two numbers used to sit on the same log line; only one was used.

        `duration_s` said 282 s and `delivery_timeout` said 3600, because the
        timeout took the *greater* of the configured floor and the estimate.
        The zone is now guarded at its own scale.
        """
        zone = self._zone(hass_mock, di_sensor)
        hass_mock.states.get = MagicMock(return_value=None)  # no live rate → guard flow

        expected_s = round(self.VOLUME_L / self.FLOW_LPM * 60)

        assert zone.volume_liters == pytest.approx(self.VOLUME_L, abs=0.05)
        assert zone.duration_s == expected_s  # ≈ 282 s
        assert zone.delivery_timeout == round(expected_s * DELIVERY_DURATION_MARGIN)
        assert zone.delivery_timeout < DEFAULT_DELIVERY_TIMEOUT_S

    @pytest.mark.asyncio
    async def test_stalled_meter_no_longer_runs_for_the_whole_hour(self, hass_mock, di_sensor, monkeypatch):
        """One litre in, then nothing: the run ends at the job's own bound.

        `asyncio.sleep` is neutralised so the loop's simulated clock advances
        without the test waiting for it — elapsed time is read back from the
        number of polls, which is exactly how the controller counts it.
        """
        zone = self._zone(hass_mock, di_sensor)
        hass_mock.states.get = MagicMock(
            side_effect=self._one_litre_then_stall(zone, "sensor.flow_meter"),
        )
        sleep = AsyncMock()
        monkeypatch.setattr("never_dry.controller.asyncio.sleep", sleep)

        expected_s = zone.duration_s
        bound_s = zone.delivery_timeout
        ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)
        delivered = await ctrl._deliver_flow_meter(zone)

        elapsed_s = sleep.await_count * FLOW_METER_POLL_INTERVAL_S
        assert delivered == pytest.approx(1.0)  # still only 1 L of the 5.6 L target
        assert elapsed_s == bound_s  # stopped at the job's bound...
        assert elapsed_s <= 2 * expected_s  # ...roughly ten minutes, not an hour
        assert elapsed_s < DEFAULT_DELIVERY_TIMEOUT_S / 6


class TestTimeoutCapIsVisibleWhereItApplies:
    """The safety timeout can be shorter than the job, and that must be visible.

    Found in the field: a zone whose real flow was four times lower than the
    configured guard needed 77 minutes to deliver its target and had a one-hour
    allowance. It opens, runs out of allowance, closes — under-watering every
    single cycle with nothing in the interface to say so. The backend already
    logged a warning; a log nobody reads is not a signal.

    The comparison stays in the entity, not in the card: a second copy in
    JavaScript would be a second source of truth for a safety bound.
    """

    def _zone(self, hass_mock, di_sensor, **over):
        zone = _make_zone(hass_mock, di_sensor, **over)
        return zone

    def test_estimated_mode_publishes_neither_key(self, hass_mock, di_sensor):
        """In estimated mode the duration is the criterion, so the bound cannot bite."""
        attrs = self._zone(hass_mock, di_sensor).extra_state_attributes
        assert "delivery_timeout_s" not in attrs
        assert "timeout_caps_duration" not in attrs

    def test_flow_meter_mode_publishes_the_bound_and_the_verdict(self, hass_mock, di_sensor):
        from never_dry.const import CONF_ZONE_DELIVERY_MODE, CONF_ZONE_FLOW_METER_SENSOR, DELIVERY_MODE_FLOW_METER

        zone = self._zone(
            hass_mock,
            di_sensor,
            **{
                CONF_ZONE_DELIVERY_MODE: DELIVERY_MODE_FLOW_METER,
                CONF_ZONE_FLOW_METER_SENSOR: "sensor.meter",
            },
        )
        attrs = zone.extra_state_attributes
        assert "delivery_timeout_s" in attrs
        assert isinstance(attrs["timeout_caps_duration"], bool)

    def test_the_verdict_follows_the_clamp_and_not_a_guess(self, hass_mock, di_sensor):
        """A generous allowance is not flagged; a short one is — same input, one field apart."""
        from never_dry.const import (
            CONF_ZONE_DELIVERY_MODE,
            CONF_ZONE_DELIVERY_TIMEOUT,
            CONF_ZONE_FLOW_METER_SENSOR,
            DELIVERY_MODE_FLOW_METER,
        )

        base = {
            CONF_ZONE_DELIVERY_MODE: DELIVERY_MODE_FLOW_METER,
            CONF_ZONE_FLOW_METER_SENSOR: "sensor.meter",
        }
        roomy = self._zone(hass_mock, di_sensor, **{**base, CONF_ZONE_DELIVERY_TIMEOUT: 36000})
        tight = self._zone(hass_mock, di_sensor, **{**base, CONF_ZONE_DELIVERY_TIMEOUT: 1})

        # There has to BE water to deliver, or both answers are "no" for the
        # boring reason and the test proves nothing. A fresh zone starts dry of
        # deficit, so the first version of this test passed on zeroes.
        roomy._zone_deficit = tight._zone_deficit = 10.0

        # Same zone, same water to deliver: only the allowance differs.
        assert roomy._guard_duration_s == tight._guard_duration_s > 0
        assert roomy.timeout_caps_duration is False
        assert tight.timeout_caps_duration is True

    def test_no_water_to_deliver_is_not_a_capped_job(self, hass_mock, di_sensor):
        """At the end of every session the volume is zero — that must not flag."""
        from never_dry.const import (
            CONF_ZONE_DELIVERY_MODE,
            CONF_ZONE_DELIVERY_TIMEOUT,
            CONF_ZONE_FLOW_METER_SENSOR,
            DELIVERY_MODE_FLOW_METER,
        )

        zone = self._zone(
            hass_mock,
            di_sensor,
            **{
                CONF_ZONE_DELIVERY_MODE: DELIVERY_MODE_FLOW_METER,
                CONF_ZONE_FLOW_METER_SENSOR: "sensor.meter",
                CONF_ZONE_DELIVERY_TIMEOUT: 1,
            },
        )
        zone._zone_deficit = 0.0
        assert zone._guard_duration_s == 0
        assert zone.timeout_caps_duration is False


class TestWarningsAreCodesNotSentences:
    """The zone publishes what is wrong as codes; the card owns the wording.

    A rendered sentence here would need a second home for every language, and
    would put user-facing copy in the layer that decides irrigation.
    """

    def _flow_meter_zone(self, hass_mock, di_sensor, **over):
        from never_dry.const import CONF_ZONE_DELIVERY_MODE, CONF_ZONE_FLOW_METER_SENSOR, DELIVERY_MODE_FLOW_METER

        base = {
            CONF_ZONE_DELIVERY_MODE: DELIVERY_MODE_FLOW_METER,
            CONF_ZONE_FLOW_METER_SENSOR: "sensor.meter",
        }
        return _make_zone(hass_mock, di_sensor, **{**base, **over})

    def test_a_healthy_zone_publishes_no_warnings_key_at_all(self, hass_mock, di_sensor):
        """Absent, not an empty list: the card collapses the box on absence."""
        zone = self._flow_meter_zone(hass_mock, di_sensor)
        zone._zone_deficit = 1.0
        assert zone.active_warnings == []
        assert "warnings" not in zone.extra_state_attributes

    def test_a_short_allowance_is_reported(self, hass_mock, di_sensor):
        from never_dry.const import CONF_ZONE_DELIVERY_TIMEOUT

        zone = self._flow_meter_zone(hass_mock, di_sensor, **{CONF_ZONE_DELIVERY_TIMEOUT: 1})
        zone._zone_deficit = 10.0
        assert "timeout_caps_duration" in zone.extra_state_attributes["warnings"]

    def test_a_missing_guard_flow_is_reported(self, hass_mock, di_sensor):
        from never_dry.const import CONF_ZONE_FLOW_RATE

        zone = self._flow_meter_zone(hass_mock, di_sensor, **{CONF_ZONE_FLOW_RATE: 0.0})
        zone._zone_deficit = 10.0
        assert "no_guard_flow" in zone.extra_state_attributes["warnings"]

    def test_estimated_mode_reports_none_of_them(self, hass_mock, di_sensor):
        """The timeout is not the closing criterion there, so neither bound applies."""
        from never_dry.const import CONF_ZONE_DELIVERY_TIMEOUT

        zone = _make_zone(hass_mock, di_sensor, **{CONF_ZONE_DELIVERY_TIMEOUT: 1})
        zone._zone_deficit = 10.0
        assert zone.active_warnings == []

    def test_every_code_has_a_string_in_both_languages(self):
        """A code the card cannot name would render as the raw code to a user."""
        import re
        from pathlib import Path

        card = Path(__file__).resolve().parents[1] / "custom_components/never_dry/www/never-dry-zone-card.js"
        js = card.read_text(encoding="utf-8")
        codes = {"timeout_caps_duration", "no_guard_flow", "valve_unreachable"}
        for code in codes:
            found = len(re.findall(rf"\bwarn_{code}\s*:", js))
            assert found == 2, f"warn_{code} appears {found} times, expected one per language"

    # `monkeypatch`, not a hand-rolled swap: patching the property on the class and
    # then *deleting* it in a finally removes the real one, and every later test in
    # the run finds no `valve_reachable` at all. That mistake cost 45 red tests.
    def test_an_unreachable_valve_is_reported_in_every_mode(self, hass_mock, di_sensor, monkeypatch):
        """Not a configuration nuance, and not gated on delivery mode."""
        from never_dry.sensor import IrrigationZoneSensor

        zone = _make_zone(hass_mock, di_sensor)  # estimated_flow, the gated mode
        zone._zone_deficit = 10.0
        monkeypatch.setattr(IrrigationZoneSensor, "valve_reachable", property(lambda self: False))
        assert zone.active_warnings == ["valve_unreachable"]

    def test_reachability_not_yet_judged_is_not_trouble(self, hass_mock, di_sensor, monkeypatch):
        """`None` means nobody has asked the valve yet — silence, not an alarm."""
        from never_dry.sensor import IrrigationZoneSensor

        zone = _make_zone(hass_mock, di_sensor)
        zone._zone_deficit = 10.0
        monkeypatch.setattr(IrrigationZoneSensor, "valve_reachable", property(lambda self: None))
        assert zone.active_warnings == []


class TestTheCapVerdictIsAboutTheJobNotItsHeadroom:
    """Boundary test for the comparison that decides the warning.

    The first version compared the job *times the safety margin* against the
    allowance, which is a different quantity: that product is the bound used to
    tighten a short job's timeout, not the time the job needs. With a 4969 s job
    and a 5400 s allowance it announced that the zone would stop short — of
    nothing. Reported from the field within an hour of the warning becoming
    visible, by a user who had just raised the timeout and saw no change.
    """

    def _zone_needing(self, hass_mock, di_sensor, timeout_s):
        from never_dry.const import (
            CONF_ZONE_DELIVERY_MODE,
            CONF_ZONE_DELIVERY_TIMEOUT,
            CONF_ZONE_FLOW_METER_SENSOR,
            DELIVERY_MODE_FLOW_METER,
        )

        zone = _make_zone(
            hass_mock,
            di_sensor,
            **{
                CONF_ZONE_DELIVERY_MODE: DELIVERY_MODE_FLOW_METER,
                CONF_ZONE_FLOW_METER_SENSOR: "sensor.meter",
                CONF_ZONE_DELIVERY_TIMEOUT: timeout_s,
            },
        )
        zone._zone_deficit = 10.0
        return zone

    def test_an_allowance_above_the_job_does_not_warn(self, hass_mock, di_sensor):
        probe = self._zone_needing(hass_mock, di_sensor, 10**6)
        needed = probe._guard_duration_s
        assert needed > 0
        assert self._zone_needing(hass_mock, di_sensor, needed + 1).timeout_caps_duration is False

    def test_an_allowance_below_the_job_warns(self, hass_mock, di_sensor):
        needed = self._zone_needing(hass_mock, di_sensor, 10**6)._guard_duration_s
        assert self._zone_needing(hass_mock, di_sensor, needed - 1).timeout_caps_duration is True

    def test_exactly_enough_is_not_a_warning(self, hass_mock, di_sensor):
        """Equality means it finishes on the last second, which is not stopping short."""
        needed = self._zone_needing(hass_mock, di_sensor, 10**6)._guard_duration_s
        assert self._zone_needing(hass_mock, di_sensor, needed).timeout_caps_duration is False

    def test_the_safety_margin_alone_never_triggers_it(self, hass_mock, di_sensor):
        """The regression itself: allowance between the job and job x margin."""
        from never_dry.const import DELIVERY_DURATION_MARGIN

        needed = self._zone_needing(hass_mock, di_sensor, 10**6)._guard_duration_s
        between = int(needed * (1 + DELIVERY_DURATION_MARGIN) / 2)
        assert needed < between < needed * DELIVERY_DURATION_MARGIN
        assert self._zone_needing(hass_mock, di_sensor, between).timeout_caps_duration is False


class TestTheMeasuredFlowShowsTheHistoryNotTheLastRun:
    """The sensor reports the median of real sessions, next to the design rate.

    It used to read the last supervised test, and a button wrote that one run
    over the configured value. Two things were wrong with that: flow follows
    mains pressure, so a single run describes a moment rather than a zone; and
    overwriting the design rate destroyed the only pair that means anything —
    what the zone was built to deliver against what it delivers.
    """

    def _zone_with_history(self, hass_mock, di_sensor, samples=(), **test):
        from never_dry.const import CONF_ZONE_DELIVERY_MODE, CONF_ZONE_FLOW_METER_SENSOR, DELIVERY_MODE_FLOW_METER

        zone = _make_zone(
            hass_mock,
            di_sensor,
            **{
                CONF_ZONE_DELIVERY_MODE: DELIVERY_MODE_FLOW_METER,
                CONF_ZONE_FLOW_METER_SENSOR: "sensor.meter",
            },
        )
        if samples:
            from never_dry.session_flow import SessionFlowWindow

            window = SessionFlowWindow()
            for value in samples:
                window.record(value)
            operator = MagicMock()
            operator.measured_flow_lpm = window.median_lpm()
            operator.session_flow_diagnostics = window.as_dict()
            zone.set_operator(operator)
        if test:
            zone.record_valve_test(test)
        return zone

    def test_no_history_yet_reads_none_and_not_a_zero(self, hass_mock, di_sensor):
        """A zero would look like a measurement of no flow. Absence is not zero."""
        from never_dry.sensor import ZoneMeasuredFlowSensor

        sensor = ZoneMeasuredFlowSensor(self._zone_with_history(hass_mock, di_sensor))
        assert sensor.native_value is None

    def test_a_single_session_is_not_enough_to_report(self, hass_mock, di_sensor):
        """One run is an anecdote — the median stays silent below its minimum."""
        from never_dry.sensor import ZoneMeasuredFlowSensor

        zone = self._zone_with_history(hass_mock, di_sensor, samples=(6.0,))
        assert ZoneMeasuredFlowSensor(zone).native_value is None

    def test_it_reports_the_median_in_litres_per_hour(self, hass_mock, di_sensor):
        from never_dry.sensor import ZoneMeasuredFlowSensor

        zone = self._zone_with_history(hass_mock, di_sensor, samples=(6.0, 6.0, 6.0))
        assert ZoneMeasuredFlowSensor(zone).native_value == 360.0

    def test_it_carries_the_design_rate_and_the_gap_beside_it(self, hass_mock, di_sensor):
        """Seeing 205 next to 360 is the argument; the sensor must hold both."""
        from never_dry.sensor import ZoneMeasuredFlowSensor

        zone = self._zone_with_history(hass_mock, di_sensor, samples=(6.0, 6.0, 6.0), updates=6, smallest_step=1.0)
        attrs = ZoneMeasuredFlowSensor(zone).extra_state_attributes
        assert attrs["design_flow_lph"] == pytest.approx(zone._flow_rate * 60.0, abs=0.1)
        assert attrs["vs_design_pct"] == pytest.approx(6.0 / zone._flow_rate * 100.0, abs=0.1)
        assert attrs["sample_count"] == 3
        assert attrs["smallest_step"] == 1.0
        assert attrs["updates"] == 6

    def test_it_is_diagnostic_and_recorded_as_a_measurement(self, hass_mock, di_sensor):
        """The series is the point: statistics need a measurement state class."""
        from homeassistant.components.sensor import SensorStateClass
        from homeassistant.const import EntityCategory
        from never_dry.sensor import ZoneMeasuredFlowSensor

        sensor = ZoneMeasuredFlowSensor(self._zone_with_history(hass_mock, di_sensor))
        assert sensor._attr_state_class == SensorStateClass.MEASUREMENT
        assert sensor._attr_entity_category == EntityCategory.DIAGNOSTIC
