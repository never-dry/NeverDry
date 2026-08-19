"""Regression: hardware self-close mid-session must not look manual.

Field bug (2026-07-15, zone Giardino Pino, Sonoff SWV-ZFE): the device's
on-board dose closed the valve 640 s into a commanded 1055 s
estimated_flow session. The valve-state listener processed the 'off'
event one second before the delivery loop noticed the external close —
and because the ValveOperator had already returned to IDLE (it handles
the same event first), the ``operator.state != IDLE`` gate let the event
through as a *manual* close: full ``reset_deficit("manual")`` on a zone
without a flow meter, wiping the ~40% residual the loop was about to
preserve. Log signature:

    21:14:42 Manual irrigation detected (no flow meter): zone='Giardino Pino', deficit reset
    21:14:43 Valve 'switch.giardino_pino' closed externally after 640s — aborting estimated_flow wait

The fix suppresses valve-state events for the valve the controller is
actively delivering to (``_running`` + ``_active_valve``), regardless of
the operator FSM state.
"""

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from never_dry.const import (
    CONF_ZONE_AREA,
    CONF_ZONE_DELIVERY_MODE,
    CONF_ZONE_EFFICIENCY,
    CONF_ZONE_FLOW_RATE,
    CONF_ZONE_NAME,
    CONF_ZONE_SYSTEM_TYPE,
    CONF_ZONE_VALVE,
    DELIVERY_MODE_ESTIMATED_FLOW,
    SYSTEM_TYPE_CUSTOM,
)
from never_dry.controller import IrrigationController
from never_dry.sensor import IrrigationZoneSensor
from never_dry.valve_fsm import ValveState
from never_dry.valve_operator import OperationResult, OperationStatus


def _idle_operator():
    """Operator stub that opens/closes OK and reads IDLE (it already
    processed the hardware 'off' event, like the ZFE self-close)."""
    op = MagicMock(state=ValveState.IDLE)
    op.async_turn_on = AsyncMock(return_value=OperationResult(status=OperationStatus.OK))
    op.async_turn_off = AsyncMock(return_value=OperationResult(status=OperationStatus.OK))
    return op


AREA = 20.0
EFF = 0.90
FLOW = 100.0  # huge flow -> 30 s plan; a 2-3 s interruption leaves ~90% residual
VALVE = "switch.pino"


def _zone(hass_mock, di_sensor, deficit):
    zone = IrrigationZoneSensor(
        hass_mock,
        {
            CONF_ZONE_NAME: "Pino",
            CONF_ZONE_VALVE: VALVE,
            CONF_ZONE_AREA: AREA,
            CONF_ZONE_SYSTEM_TYPE: SYSTEM_TYPE_CUSTOM,
            CONF_ZONE_EFFICIENCY: EFF,
            CONF_ZONE_FLOW_RATE: FLOW,
            CONF_ZONE_DELIVERY_MODE: DELIVERY_MODE_ESTIMATED_FLOW,
        },
        di_sensor,
    )
    zone._zone_deficit = deficit
    return zone


def _off_event():
    event = MagicMock()
    event.data = {
        "entity_id": VALVE,
        "old_state": MagicMock(state="on"),
        "new_state": MagicMock(state="off"),
    }
    return event


class _ValveEnv:
    """Mutable valve state served through hass.states.get."""

    def __init__(self, hass_mock):
        self.state = "on"

        def get_state(entity_id):
            if entity_id == VALVE:
                return MagicMock(state=self.state)
            return None

        hass_mock.states.get = MagicMock(side_effect=get_state)


@pytest.mark.asyncio
async def test_hardware_close_mid_session_keeps_residual_deficit(hass_mock, di_sensor):
    """Tonight's exact sequence: the listener event must be suppressed and
    the loop must settle the elapsed fraction, leaving the residual deficit."""
    deficit = 2.25  # 50 L -> 30 s planned at 100 L/min
    zone = _zone(hass_mock, di_sensor, deficit)
    env = _ValveEnv(hass_mock)
    ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)
    # Operator wired and ALREADY back to IDLE: it processed the 'off'
    # event first, exactly like the ZFE self-close in the field.
    ctrl._valve_operators[VALVE] = _idle_operator()
    planned = zone.duration_s
    assert planned == 30

    task = asyncio.create_task(ctrl._irrigate_zones(["Pino"]))
    ctrl._irrigation_task = task
    await asyncio.sleep(2.2)

    # Hardware self-close: state flips off AND the HA state listener runs
    # BEFORE the delivery loop's next 1 s poll.
    env.state = "off"
    ctrl._on_valve_state_change(_off_event())

    await task

    # The manual path must NOT have fired: the deficit keeps the residual
    # of the interrupted plan instead of being zeroed.
    delivered = zone._last_volume_delivered
    planned_volume = deficit * AREA / EFF
    assert 0 < delivered < planned_volume
    expected_residual = deficit - delivered * EFF / AREA
    assert expected_residual > 0
    assert zone._zone_deficit == pytest.approx(expected_residual, abs=0.01)
    # ~90% of the plan must survive: unambiguous against a full reset.
    assert zone._zone_deficit >= deficit * 0.8
    assert zone._last_irrigation_source != "manual"
    # History records the partial, not the full plan.
    elapsed = delivered / planned_volume * planned
    assert 1 <= elapsed <= planned - 1
    assert abs(zone._last_session_duration_s - elapsed) <= 2


@pytest.mark.asyncio
async def test_genuine_manual_close_credits_estimate_when_idle(hass_mock, di_sensor):
    """Control case: with NO commanded session running, a manual close on a
    meterless zone credits flow_rate x elapsed — it does NOT reset the
    deficit (only mark_irrigated does). 100 L/min x 3 s = 5 L on 20 m²
    at η=0.9 → 0.225 mm."""
    deficit = 0.5
    zone = _zone(hass_mock, di_sensor, deficit)
    _ValveEnv(hass_mock)
    ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)
    ctrl._valve_operators[VALVE] = _idle_operator()
    # Manual open tracked 3 s ago.
    ctrl._manual_valve_open[VALVE] = None
    ctrl._manual_session_meta[VALVE] = (datetime.now() - timedelta(seconds=3), deficit)

    ctrl._on_valve_state_change(_off_event())

    assert zone._zone_deficit == pytest.approx(deficit - 0.225, abs=0.01)
    assert zone._last_irrigation_source == "manual"
    assert zone._last_volume_delivered == pytest.approx(5.0, abs=0.1)
