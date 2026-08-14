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
    *hardware* auto-shutoff fired. His words: on a valve without one, "that
    could be very bad".

    These tests characterise what NeverDry does today. Both will need updating
    when the failsafe lands: the expected duration is computed, logged, and
    then discarded, so nothing bounds the run except a timeout that defaults to
    an hour regardless of how long the zone was ever going to take.
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

    def test_the_expected_duration_is_computed_and_then_discarded(self, hass_mock, di_sensor):
        """The two numbers sit on the same log line; only the larger is used.

        `delivery_timeout` takes the *greater* of the configured floor and the
        guard-flow estimate, so it can only ever loosen. With the default floor
        of one hour, a zone that was going to take five minutes is guarded at
        twelve times its own expected duration.
        """
        zone = self._zone(hass_mock, di_sensor)
        hass_mock.states.get = MagicMock(return_value=None)  # no live rate → guard flow

        expected_s = round(self.VOLUME_L / self.FLOW_LPM * 60)

        assert zone.volume_liters == pytest.approx(self.VOLUME_L, abs=0.05)
        assert zone.duration_s == expected_s  # ≈ 282 s: we know the right answer
        assert zone.delivery_timeout == 3600  # ...and guard with an hour anyway
        assert zone.delivery_timeout > 12 * expected_s

    @pytest.mark.asyncio
    async def test_stalled_meter_keeps_the_valve_open_for_the_whole_timeout(self, hass_mock, di_sensor, monkeypatch):
        """One litre in, then nothing: the run continues for the full hour.

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
        ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)
        delivered = await ctrl._deliver_flow_meter(zone)

        elapsed_s = sleep.await_count * FLOW_METER_POLL_INTERVAL_S
        assert delivered == pytest.approx(1.0)  # 1 L of the 5.6 L target
        assert elapsed_s == 3600  # ran the full timeout...
        assert elapsed_s > 12 * expected_s  # ...for a zone needing ~282 s
