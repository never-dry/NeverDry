"""End-trigger race matrix tests (developer_manual §7.2, marker [M]).

One test per previously-uncovered cell of the irrigation end-trigger
coverage matrix. Every test drives a real delivery loop, fires a second
end trigger while the first is armed, and asserts the same contract:

- the zone deficit is decremented by exactly the credited volume
  (``deficit_start - delivered * efficiency / area``) and — because the
  scenarios interrupt the plan early — a LARGE residual must survive:
  a full reset is unmistakable from a correct partial settle;
- the last-session history is written once and is internally coherent:
  ``last_volume_delivered`` matches the credited volume and
  ``last_session_duration_s`` matches the loop's actual running time
  (wall-clock, +/-2 s tolerance for scheduler overhead);
- the settle happens exactly once and is never attributed to a manual
  irrigation (``last_irrigation_source != "manual"``).

Recipe (field bug of 2026-07-15, Giardino Pino): a huge guard flow
(100 L/min) gives a ~30 s plan; interrupting 2-4 s in must leave >=80%
of the deficit. The tiny-deficit variant used before clipped everything
to zero and could not tell a correct partial from an erroneous full
reset — exactly the bug that slipped through.

Realism upgrades over the first version:
- a ValveOperator stub (IDLE, like after it processed a hardware 'off')
  is wired for the valve, so ``_open_valve``/``_close_valve`` take the
  operator path used in production;
- whenever the valve flips off mid-loop (hardware/watchdog closes), the
  HA valve-state listener ``_on_valve_state_change`` is fired too — the
  push event always beats the loop's poll in the field.

WDOG cells: at controller level the watchdog manifests as the forced
``switch.turn_off`` -> valve state 'off' mid-loop; its own firing logic
is covered in test_valve_operator.py.
"""

import asyncio
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
    DELIVERY_MODE_ESTIMATED_FLOW,
    DELIVERY_MODE_FLOW_METER,
    FLOW_METER_POLL_INTERVAL_S,
    SYSTEM_TYPE_CUSTOM,
)
from never_dry.controller import IrrigationController
from never_dry.sensor import IrrigationZoneSensor
from never_dry.valve_fsm import ValveState
from never_dry.valve_operator import OperationResult, OperationStatus

AREA = 20.0
EFF = 0.90
FLOW = 100.0  # huge guard flow -> short plan, unambiguous residuals
DEFICIT = 2.25  # mm -> 50 L target -> 30 s plan at 100 L/min
PLAN_S = 30
VALVE = "switch.matrix_valve"
METER = "sensor.matrix_meter"
TIMEOUT_S = 2 * FLOW_METER_POLL_INTERVAL_S  # 4 s with the 2 s poll


def _idle_operator():
    """Operator stub: opens/closes OK and reads IDLE — the state it shows
    right after processing a hardware 'off' event (ZFE self-close)."""
    op = MagicMock(state=ValveState.IDLE)
    op.async_turn_on = AsyncMock(return_value=OperationResult(status=OperationStatus.OK))
    op.async_turn_off = AsyncMock(return_value=OperationResult(status=OperationStatus.OK))
    return op


def _off_event():
    event = MagicMock()
    event.data = {
        "entity_id": VALVE,
        "old_state": MagicMock(state="on"),
        "new_state": MagicMock(state="off"),
    }
    return event


class _Env:
    """Scriptable valve/meter state shared with the controller via states.get."""

    def __init__(self, hass_mock, meter_liters=100.0):
        self.valve_state = "on"
        self.meter_liters = meter_liters
        self.meter_step = 0.0  # liters added on every meter read (0 = dead meter)
        self.meter_reads = 0
        self.valve_reads = 0
        self.on_valve_read = None  # callback(reads) fired on each valve poll
        self.ctrl = None  # set by _make_ctrl

        def get_state(entity_id):
            if entity_id == METER:
                self.meter_reads += 1
                self.meter_liters += self.meter_step
                s = MagicMock()
                s.state = str(self.meter_liters)
                s.attributes = {"unit_of_measurement": "L"}
                return s
            if entity_id == VALVE:
                self.valve_reads += 1
                if self.on_valve_read:
                    self.on_valve_read(self.valve_reads)
                s = MagicMock()
                s.state = self.valve_state
                return s
            return None

        hass_mock.states.get = MagicMock(side_effect=get_state)

    def hardware_close(self):
        """The device closes itself: state flips off AND the HA push event
        reaches the state listener before the loop's next poll."""
        self.valve_state = "off"
        if self.ctrl is not None:
            self.ctrl._on_valve_state_change(_off_event())


def _make_ctrl(hass_mock, di_sensor, zone, env):
    ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)
    ctrl._valve_operators[VALVE] = _idle_operator()
    env.ctrl = ctrl
    return ctrl


def _flow_meter_zone(hass_mock, di_sensor):
    zone = IrrigationZoneSensor(
        hass_mock,
        {
            CONF_ZONE_NAME: "Matrix",
            CONF_ZONE_VALVE: VALVE,
            CONF_ZONE_AREA: AREA,
            CONF_ZONE_SYSTEM_TYPE: SYSTEM_TYPE_CUSTOM,
            CONF_ZONE_EFFICIENCY: EFF,
            CONF_ZONE_FLOW_RATE: FLOW,
            CONF_ZONE_DELIVERY_MODE: DELIVERY_MODE_FLOW_METER,
            CONF_ZONE_FLOW_METER_SENSOR: METER,
            CONF_ZONE_DELIVERY_TIMEOUT: TIMEOUT_S,
        },
        di_sensor,
    )
    zone._zone_deficit = DEFICIT
    return zone


def _estimated_zone(hass_mock, di_sensor):
    zone = IrrigationZoneSensor(
        hass_mock,
        {
            CONF_ZONE_NAME: "Matrix",
            CONF_ZONE_VALVE: VALVE,
            CONF_ZONE_AREA: AREA,
            CONF_ZONE_SYSTEM_TYPE: SYSTEM_TYPE_CUSTOM,
            CONF_ZONE_EFFICIENCY: EFF,
            CONF_ZONE_FLOW_RATE: FLOW,
            CONF_ZONE_DELIVERY_MODE: DELIVERY_MODE_ESTIMATED_FLOW,
        },
        di_sensor,
    )
    zone._zone_deficit = DEFICIT
    return zone


def _assert_partial_with_residual(zone, *, max_running_s):
    """The uniform contract: a plan interrupted early must keep a large
    residual, with history exactly coherent and never marked manual."""
    delivered = zone._last_volume_delivered
    assert delivered > 0, "session must have credited some water"
    planned_volume = DEFICIT * AREA / EFF  # 50 L
    assert delivered < planned_volume * 0.5, "interruption came early: far from full plan"
    # Deficit decremented by exactly the credited volume (mm).
    expected_residual = DEFICIT - delivered * EFF / AREA
    assert zone._zone_deficit == pytest.approx(expected_residual, abs=0.01)
    # The residual is LARGE: a full reset cannot pass this.
    assert zone._zone_deficit >= DEFICIT * 0.8
    # Never attributed to manual irrigation (field bug 2026-07-15).
    assert zone._last_irrigation_source != "manual"
    # History written once: totals match the single credited session.
    assert zone._total_water_delivered == pytest.approx(delivered, abs=0.06)
    assert zone._session_water_delivered == pytest.approx(delivered, abs=0.06)
    assert zone._last_irrigated is not None
    # Running time coherent with the credited volume (guard-flow credit:
    # delivered = FLOW x elapsed / 60 -> elapsed = delivered x 60 / FLOW).
    duration = zone._last_session_duration_s
    elapsed = delivered * 60.0 / FLOW
    assert 1 <= duration <= max_running_s + 2
    assert abs(duration - elapsed) <= 2
    assert zone.is_irrigating is False
    return delivered, duration


def _assert_no_further_settle(zone, delivered, deficit):
    """A late second trigger must not re-credit the session."""
    assert zone._last_volume_delivered == pytest.approx(delivered, abs=0.001)
    assert zone._total_water_delivered == pytest.approx(delivered, abs=0.06)
    assert zone._zone_deficit == pytest.approx(deficit, abs=0.001)


async def _run(ctrl, zone_name="Matrix"):
    task = asyncio.create_task(ctrl._irrigate_zones([zone_name]))
    ctrl._irrigation_task = task
    return task


# ── TARGET x UNLOAD (and controller-side UNLOAD diagonal) ─────────────


@pytest.mark.asyncio
async def test_unload_mid_flow_meter_settles_measured_partial(hass_mock, di_sensor):
    """[M] TARGET x UNLOAD: entry unload mid-delivery credits the measured partial."""
    zone = _flow_meter_zone(hass_mock, di_sensor)
    env = _Env(hass_mock)
    env.meter_step = 1.0  # meter progresses: measured credit, not estimated
    ctrl = _make_ctrl(hass_mock, di_sensor, zone, env)

    task = await _run(ctrl)
    await asyncio.sleep(1.0)  # inside the first poll window
    await ctrl.async_stop()  # unload path: stop_requested + await task + settle
    assert task.done()

    delivered = zone._last_volume_delivered
    assert 0 < delivered < 5  # measured liters (1 L/read), not the 100 L/min estimate
    expected_residual = DEFICIT - delivered * EFF / AREA
    assert zone._zone_deficit == pytest.approx(expected_residual, abs=0.01)
    assert zone._zone_deficit >= DEFICIT * 0.8
    assert zone._last_irrigation_source != "manual"


# ── EST row ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stop_zone_mid_estimated_flow_credits_elapsed_fraction(hass_mock, di_sensor):
    """[M] EST x STOPZ: per-zone stop 2-3 s into a 30 s plan -> ~90% residual."""
    zone = _estimated_zone(hass_mock, di_sensor)
    env = _Env(hass_mock)
    ctrl = _make_ctrl(hass_mock, di_sensor, zone, env)
    assert zone.duration_s == PLAN_S

    task = await _run(ctrl)
    await asyncio.sleep(2.2)
    ctrl._stop_zone = "Matrix"  # what _handle_stop_zone sets for the loop
    await task

    _assert_partial_with_residual(zone, max_running_s=PLAN_S)


@pytest.mark.asyncio
async def test_watchdog_forced_close_mid_estimated_flow_credits_elapsed_fraction(hass_mock, di_sensor):
    """[M] EST x WDOG: watchdog force-off 2 s into a 30 s plan; the state
    listener fires first (like the field event) and must not reset."""
    zone = _estimated_zone(hass_mock, di_sensor)
    env = _Env(hass_mock)
    ctrl = _make_ctrl(hass_mock, di_sensor, zone, env)

    def watchdog_fires(reads):
        if reads >= 2:  # second 1 s tick of the wait loop
            env.hardware_close()

    env.on_valve_read = watchdog_fires

    task = await _run(ctrl)
    await task

    _assert_partial_with_residual(zone, max_running_s=PLAN_S)


@pytest.mark.asyncio
async def test_unload_mid_estimated_flow_credits_elapsed_fraction(hass_mock, di_sensor):
    """[M] EST x UNLOAD: entry unload 2-3 s into a 30 s plan -> ~90% residual."""
    zone = _estimated_zone(hass_mock, di_sensor)
    env = _Env(hass_mock)
    ctrl = _make_ctrl(hass_mock, di_sensor, zone, env)

    task = await _run(ctrl)
    await asyncio.sleep(2.2)
    await ctrl.async_stop()
    assert task.done()

    _assert_partial_with_residual(zone, max_running_s=PLAN_S)


# ── TIMEOUT row ───────────────────────────────────────────────────────
# The delivery timeout (4 s) truncates a 30 s plan: the dead-meter credit
# is FLOW x elapsed / 60, so even the pure timeout leaves >=80% residual.


@pytest.mark.asyncio
async def test_stop_just_before_timeout_settles_once(hass_mock, di_sensor):
    """[M] TIMEOUT x STOP: emergency stop racing the delivery timeout on a
    dead meter credits the guard-flow estimate exactly once."""
    zone = _flow_meter_zone(hass_mock, di_sensor)
    env = _Env(hass_mock)  # dead meter: reading frozen
    ctrl = _make_ctrl(hass_mock, di_sensor, zone, env)

    task = await _run(ctrl)
    await asyncio.sleep(FLOW_METER_POLL_INTERVAL_S + 1.0)  # inside the last poll window
    await ctrl._handle_stop(MagicMock(data={}))
    await task

    delivered, _ = _assert_partial_with_residual(zone, max_running_s=TIMEOUT_S)
    _assert_no_further_settle(zone, delivered, zone._zone_deficit)


@pytest.mark.asyncio
async def test_stop_zone_just_before_timeout_settles_once(hass_mock, di_sensor):
    """[M] TIMEOUT x STOPZ: per-zone stop racing the timeout, single settle."""
    zone = _flow_meter_zone(hass_mock, di_sensor)
    env = _Env(hass_mock)
    ctrl = _make_ctrl(hass_mock, di_sensor, zone, env)

    task = await _run(ctrl)
    await asyncio.sleep(FLOW_METER_POLL_INTERVAL_S + 1.0)
    ctrl._stop_zone = "Matrix"
    await task

    delivered, _ = _assert_partial_with_residual(zone, max_running_s=TIMEOUT_S)
    _assert_no_further_settle(zone, delivered, zone._zone_deficit)


@pytest.mark.asyncio
async def test_external_close_before_timeout_settles_estimate(hass_mock, di_sensor):
    """[M] TIMEOUT x EXT: hardware auto-close (push event included) beats the
    timeout; the elapsed open time is credited from the guard flow."""
    zone = _flow_meter_zone(hass_mock, di_sensor)
    env = _Env(hass_mock)

    def hardware_closes(reads):
        if reads >= 2:  # seen by the loop's external-close check
            env.hardware_close()

    env.on_valve_read = hardware_closes
    ctrl = _make_ctrl(hass_mock, di_sensor, zone, env)

    task = await _run(ctrl)
    await task

    _assert_partial_with_residual(zone, max_running_s=TIMEOUT_S)


@pytest.mark.asyncio
async def test_watchdog_close_races_delivery_timeout_single_settle(hass_mock, di_sensor):
    """[M] TIMEOUT x WDOG: watchdog force-off lands in the same window as the
    delivery timeout; the double close is idempotent, the settle single."""
    zone = _flow_meter_zone(hass_mock, di_sensor)
    env = _Env(hass_mock)

    def watchdog_fires(reads):
        if reads >= 3:  # right around the timeout expiry
            env.hardware_close()

    env.on_valve_read = watchdog_fires
    ctrl = _make_ctrl(hass_mock, di_sensor, zone, env)

    task = await _run(ctrl)
    await task

    delivered, _ = _assert_partial_with_residual(zone, max_running_s=TIMEOUT_S)
    _assert_no_further_settle(zone, delivered, zone._zone_deficit)


@pytest.mark.asyncio
async def test_unload_races_delivery_timeout_single_settle(hass_mock, di_sensor):
    """[M] TIMEOUT x UNLOAD: unload arriving near timeout expiry settles once."""
    zone = _flow_meter_zone(hass_mock, di_sensor)
    env = _Env(hass_mock)
    ctrl = _make_ctrl(hass_mock, di_sensor, zone, env)

    task = await _run(ctrl)
    await asyncio.sleep(TIMEOUT_S - 0.5)  # just before the timeout fires
    await ctrl.async_stop()
    assert task.done()

    delivered, _ = _assert_partial_with_residual(zone, max_running_s=TIMEOUT_S)
    deficit_after = zone._zone_deficit
    # async_stop is idempotent: a second call must not re-settle.
    await ctrl.async_stop()
    _assert_no_further_settle(zone, delivered, deficit_after)


# ── STOP row ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_global_stop_and_stop_zone_together_settle_once(hass_mock, di_sensor):
    """[M] STOP x STOPZ: zone stop followed by panic global stop, one settle."""
    zone = _flow_meter_zone(hass_mock, di_sensor)
    env = _Env(hass_mock)
    ctrl = _make_ctrl(hass_mock, di_sensor, zone, env)

    task = await _run(ctrl)
    await asyncio.sleep(FLOW_METER_POLL_INTERVAL_S + 0.5)
    ctrl._stop_zone = "Matrix"
    await ctrl._handle_stop(MagicMock(data={}))
    await task

    delivered, _ = _assert_partial_with_residual(zone, max_running_s=TIMEOUT_S)
    _assert_no_further_settle(zone, delivered, zone._zone_deficit)
    assert ctrl.is_running is False


@pytest.mark.asyncio
async def test_global_stop_with_valve_already_closed_externally(hass_mock, di_sensor):
    """[M] STOP x EXT: hardware close (push event included) an instant before
    the emergency stop; the session settles exactly once, never as manual."""
    zone = _flow_meter_zone(hass_mock, di_sensor)
    env = _Env(hass_mock)

    def hardware_closes(reads):
        if reads >= 2:
            env.hardware_close()

    env.on_valve_read = hardware_closes
    ctrl = _make_ctrl(hass_mock, di_sensor, zone, env)

    task = await _run(ctrl)
    await asyncio.sleep(FLOW_METER_POLL_INTERVAL_S + 0.5)
    await ctrl._handle_stop(MagicMock(data={}))
    await task

    delivered, _ = _assert_partial_with_residual(zone, max_running_s=TIMEOUT_S)
    _assert_no_further_settle(zone, delivered, zone._zone_deficit)


@pytest.mark.asyncio
async def test_global_stop_races_watchdog_force_close(hass_mock, di_sensor):
    """[M] STOP x WDOG: emergency stop and watchdog force-off (push event
    included) in the same poll window; single coherent settle."""
    zone = _flow_meter_zone(hass_mock, di_sensor)
    env = _Env(hass_mock)
    ctrl = _make_ctrl(hass_mock, di_sensor, zone, env)

    task = await _run(ctrl)
    await asyncio.sleep(FLOW_METER_POLL_INTERVAL_S + 0.5)
    env.hardware_close()  # watchdog turn_off takes effect...
    await ctrl._handle_stop(MagicMock(data={}))  # ...as the user hits stop
    await task

    delivered, _ = _assert_partial_with_residual(zone, max_running_s=TIMEOUT_S)
    _assert_no_further_settle(zone, delivered, zone._zone_deficit)


@pytest.mark.asyncio
async def test_global_stop_then_unload_settles_once(hass_mock, di_sensor):
    """[M] STOP x UNLOAD: user stop followed by entry unload (same reload
    sequence HA runs); the partial session settles exactly once."""
    zone = _flow_meter_zone(hass_mock, di_sensor)
    env = _Env(hass_mock)
    ctrl = _make_ctrl(hass_mock, di_sensor, zone, env)

    task = await _run(ctrl)
    await asyncio.sleep(FLOW_METER_POLL_INTERVAL_S + 0.5)
    await ctrl._handle_stop(MagicMock(data={}))
    await ctrl.async_stop()
    assert task.done()

    delivered, _ = _assert_partial_with_residual(zone, max_running_s=TIMEOUT_S)
    _assert_no_further_settle(zone, delivered, zone._zone_deficit)


# ── STOPZ row ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stop_zone_with_valve_already_closed_externally(hass_mock, di_sensor):
    """[M] STOPZ x EXT: per-zone stop on a valve that just closed on its own
    (push event included); partial settle, never manual."""
    zone = _flow_meter_zone(hass_mock, di_sensor)
    env = _Env(hass_mock)

    def hardware_closes(reads):
        if reads >= 2:
            env.hardware_close()

    env.on_valve_read = hardware_closes
    ctrl = _make_ctrl(hass_mock, di_sensor, zone, env)

    task = await _run(ctrl)
    await asyncio.sleep(FLOW_METER_POLL_INTERVAL_S + 0.5)
    ctrl._stop_zone = "Matrix"
    await task

    delivered, _ = _assert_partial_with_residual(zone, max_running_s=TIMEOUT_S)
    _assert_no_further_settle(zone, delivered, zone._zone_deficit)


@pytest.mark.asyncio
async def test_stop_zone_races_watchdog_force_close(hass_mock, di_sensor):
    """[M] STOPZ x WDOG: per-zone stop and watchdog force-off together."""
    zone = _flow_meter_zone(hass_mock, di_sensor)
    env = _Env(hass_mock)
    ctrl = _make_ctrl(hass_mock, di_sensor, zone, env)

    task = await _run(ctrl)
    await asyncio.sleep(FLOW_METER_POLL_INTERVAL_S + 0.5)
    env.hardware_close()
    ctrl._stop_zone = "Matrix"
    await task

    delivered, _ = _assert_partial_with_residual(zone, max_running_s=TIMEOUT_S)
    _assert_no_further_settle(zone, delivered, zone._zone_deficit)


@pytest.mark.asyncio
async def test_stop_zone_then_unload_settles_once(hass_mock, di_sensor):
    """[M] STOPZ x UNLOAD: per-zone stop immediately followed by unload."""
    zone = _flow_meter_zone(hass_mock, di_sensor)
    env = _Env(hass_mock)
    ctrl = _make_ctrl(hass_mock, di_sensor, zone, env)

    task = await _run(ctrl)
    await asyncio.sleep(FLOW_METER_POLL_INTERVAL_S + 0.5)
    ctrl._stop_zone = "Matrix"
    await ctrl.async_stop()
    assert task.done()

    delivered, _ = _assert_partial_with_residual(zone, max_running_s=TIMEOUT_S)
    _assert_no_further_settle(zone, delivered, zone._zone_deficit)


# ── EXT row ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_external_close_then_late_watchdog_off_no_double_settle(hass_mock, di_sensor):
    """[M] EXT x WDOG: hardware auto-close ends the session; a late watchdog
    turn_off must not open a second session nor re-credit the first."""
    zone = _flow_meter_zone(hass_mock, di_sensor)
    env = _Env(hass_mock)

    def hardware_closes(reads):
        if reads >= 2:
            env.hardware_close()

    env.on_valve_read = hardware_closes
    ctrl = _make_ctrl(hass_mock, di_sensor, zone, env)

    task = await _run(ctrl)
    await task
    delivered, _ = _assert_partial_with_residual(zone, max_running_s=TIMEOUT_S)
    deficit_after = zone._zone_deficit

    # Late watchdog force-off after the session already ended (redundant
    # switch call; HA state is already off, so no on->off event follows).
    await hass_mock.services.async_call("switch", "turn_off", {"entity_id": VALVE})
    await asyncio.sleep(0)
    _assert_no_further_settle(zone, delivered, deficit_after)


@pytest.mark.asyncio
async def test_external_close_then_unload_no_double_settle(hass_mock, di_sensor):
    """[M] EXT x UNLOAD: hardware auto-close ends the session; the entry
    unload that follows must not settle it again."""
    zone = _flow_meter_zone(hass_mock, di_sensor)
    env = _Env(hass_mock)

    def hardware_closes(reads):
        if reads >= 2:
            env.hardware_close()

    env.on_valve_read = hardware_closes
    ctrl = _make_ctrl(hass_mock, di_sensor, zone, env)

    task = await _run(ctrl)
    await task
    delivered, _ = _assert_partial_with_residual(zone, max_running_s=TIMEOUT_S)
    deficit_after = zone._zone_deficit

    await ctrl.async_stop()
    _assert_no_further_settle(zone, delivered, deficit_after)
