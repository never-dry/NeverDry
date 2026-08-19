"""Tests for IrrigationController when wired with a ValveOperator + notifier.

These tests cover the behaviours introduced by AI-031:
- An operator-reported FAILED open aborts the delivery cleanly.
- An operator in MAINTENANCE is treated like a failed open.
- Manual valve listener relies on per-valve operator state, not the
  legacy global ``_running`` flag, fixing the AI-003 race.
- Emergency stop closes valves concurrently.
- ``volume_preset`` falls back to ``switch.turn_on`` when the smart
  valve does not auto-open after ``number.set_value``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from never_dry.const import (
    CONF_ZONE_AREA,
    CONF_ZONE_DELIVERY_MODE,
    CONF_ZONE_EFFICIENCY,
    CONF_ZONE_FLOW_RATE,
    CONF_ZONE_NAME,
    CONF_ZONE_SYSTEM_TYPE,
    CONF_ZONE_THRESHOLD,
    CONF_ZONE_VALVE,
    CONF_ZONE_VOLUME_ENTITY,
    SYSTEM_TYPE_CUSTOM,
)
from never_dry.controller import IrrigationController
from never_dry.sensor import IrrigationZoneSensor
from never_dry.valve_fsm import ValveState
from never_dry.valve_operator import OperationResult, OperationStatus

# ── Helpers ───────────────────────────────────────────────────────────


def _fake_operator(state: ValveState = ValveState.IDLE, **kwargs):
    """Build a stand-in driver with AsyncMock on/off commands + a state property."""
    op = MagicMock()
    op.state = state
    op.is_in_maintenance = state == ValveState.MAINTENANCE
    op.async_turn_on = AsyncMock(return_value=kwargs.get("open_result", OperationResult(OperationStatus.OK)))
    op.async_turn_off = AsyncMock(return_value=kwargs.get("close_result", OperationResult(OperationStatus.OK)))
    return op


def _make_zone_orto(hass_mock, di_sensor):
    """Build the standard 'Orto' zone for these tests."""
    cfg = {
        CONF_ZONE_NAME: "Orto",
        CONF_ZONE_VALVE: "switch.valve_orto",
        CONF_ZONE_AREA: 20.0,
        CONF_ZONE_SYSTEM_TYPE: SYSTEM_TYPE_CUSTOM,
        CONF_ZONE_EFFICIENCY: 0.90,
        CONF_ZONE_FLOW_RATE: 8.0,
        CONF_ZONE_THRESHOLD: 15.0,
    }
    return IrrigationZoneSensor(hass_mock, cfg, di_sensor)


# ── Operator integration ─────────────────────────────────────────────


async def test_open_failure_aborts_delivery(hass_mock, di_sensor):
    """A FAILED open from the operator prevents any close, returns 0 delivered."""
    zone = _make_zone_orto(hass_mock, di_sensor)
    zone._zone_deficit = 5.0  # ensure volume_liters > 0

    op = _fake_operator(
        open_result=OperationResult(OperationStatus.FAILED, "OPEN_FAILED"),
    )
    ctrl = IrrigationController(
        hass_mock,
        di_sensor,
        [zone],
        inter_zone_delay=0,
        valve_operators={zone.valve: op},
    )

    delivered = await ctrl._deliver_estimated_flow(zone)
    assert delivered == 0.0
    op.async_turn_on.assert_awaited_once()
    op.async_turn_off.assert_not_called()


async def test_maintenance_open_is_treated_as_failed(hass_mock, di_sensor):
    """An operator in MAINTENANCE returns MAINTENANCE → delivery aborts."""
    zone = _make_zone_orto(hass_mock, di_sensor)
    zone._zone_deficit = 5.0

    op = _fake_operator(
        state=ValveState.MAINTENANCE,
        open_result=OperationResult(OperationStatus.MAINTENANCE, "in_maintenance"),
    )
    ctrl = IrrigationController(
        hass_mock,
        di_sensor,
        [zone],
        inter_zone_delay=0,
        valve_operators={zone.valve: op},
    )

    delivered = await ctrl._deliver_estimated_flow(zone)
    assert delivered == 0.0


async def test_close_uses_operator_when_present(hass_mock, di_sensor):
    """``_close_valve`` routes through the operator when one is registered."""
    zone = _make_zone_orto(hass_mock, di_sensor)
    op = _fake_operator()
    ctrl = IrrigationController(
        hass_mock,
        di_sensor,
        [zone],
        inter_zone_delay=0,
        valve_operators={zone.valve: op},
    )
    ok = await ctrl._close_valve(zone.valve)
    assert ok is True
    op.async_turn_off.assert_awaited_once()
    # No direct switch.turn_off should have been issued.
    turn_off_calls = [c for c in hass_mock.services.async_call.call_args_list if c.args[:2] == ("switch", "turn_off")]
    assert turn_off_calls == []


async def test_emergency_stop_closes_in_parallel(hass_mock, di_sensor):
    """``_handle_stop`` calls close concurrently on every configured valve."""
    zone = _make_zone_orto(hass_mock, di_sensor)
    zone2_cfg = {
        CONF_ZONE_NAME: "Prato",
        CONF_ZONE_VALVE: "switch.valve_prato",
        CONF_ZONE_AREA: 50.0,
        CONF_ZONE_SYSTEM_TYPE: SYSTEM_TYPE_CUSTOM,
        CONF_ZONE_EFFICIENCY: 0.70,
        CONF_ZONE_FLOW_RATE: 15.0,
    }
    zone2 = IrrigationZoneSensor(hass_mock, zone2_cfg, di_sensor)

    op1 = _fake_operator()
    op2 = _fake_operator()
    ctrl = IrrigationController(
        hass_mock,
        di_sensor,
        [zone, zone2],
        inter_zone_delay=0,
        valve_operators={zone.valve: op1, zone2.valve: op2},
    )
    await ctrl._handle_stop(MagicMock())

    op1.async_turn_off.assert_awaited_once()
    op2.async_turn_off.assert_awaited_once()
    assert ctrl.is_running is False


# ── Manual valve listener using operator state ───────────────────────


def test_manual_listener_ignored_when_operator_busy(hass_mock, di_sensor):
    """A state change while the operator is in REQ_OPEN is not a manual irrigation."""
    zone = _make_zone_orto(hass_mock, di_sensor)
    op = _fake_operator(state=ValveState.REQ_OPEN)
    ctrl = IrrigationController(
        hass_mock,
        di_sensor,
        [zone],
        inter_zone_delay=0,
        valve_operators={zone.valve: op},
    )

    event = MagicMock()
    event.data = {
        "entity_id": zone.valve,
        "old_state": MagicMock(state="off"),
        "new_state": MagicMock(state="on"),
    }
    ctrl._on_valve_state_change(event)
    # No manual baseline should have been recorded.
    assert zone.valve not in ctrl._manual_valve_open


def test_manual_listener_records_when_operator_idle(hass_mock, di_sensor):
    """When the operator is IDLE, an off→on state change *is* a manual irrigation."""
    zone = _make_zone_orto(hass_mock, di_sensor)
    op = _fake_operator(state=ValveState.IDLE)
    ctrl = IrrigationController(
        hass_mock,
        di_sensor,
        [zone],
        inter_zone_delay=0,
        valve_operators={zone.valve: op},
    )

    event = MagicMock()
    event.data = {
        "entity_id": zone.valve,
        "old_state": MagicMock(state="off"),
        "new_state": MagicMock(state="on"),
    }
    ctrl._on_valve_state_change(event)
    assert zone.valve in ctrl._manual_valve_open


# ── volume_preset auto-open + fallback ───────────────────────────────


async def test_volume_preset_falls_back_to_turn_on(hass_mock, di_sensor):
    """If the smart valve never auto-opens, controller sends switch.turn_on."""
    cfg = {
        CONF_ZONE_NAME: "Orto",
        CONF_ZONE_VALVE: "switch.valve_orto",
        CONF_ZONE_AREA: 20.0,
        CONF_ZONE_SYSTEM_TYPE: SYSTEM_TYPE_CUSTOM,
        CONF_ZONE_EFFICIENCY: 0.90,
        CONF_ZONE_FLOW_RATE: 8.0,
        CONF_ZONE_THRESHOLD: 15.0,
        CONF_ZONE_DELIVERY_MODE: "volume_preset",
        CONF_ZONE_VOLUME_ENTITY: "number.valve_volume",
    }
    zone = IrrigationZoneSensor(hass_mock, cfg, di_sensor)
    zone._zone_deficit = 5.0

    # Valve never reports "on" → grace expires.
    off_state = MagicMock()
    off_state.state = "off"
    hass_mock.states.get = MagicMock(return_value=off_state)

    ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)
    ctrl.auto_open_grace_s = 0.05

    delivered = await ctrl._deliver_volume_preset(zone)
    assert delivered == zone.volume_liters

    turn_on_calls = [
        c
        for c in hass_mock.services.async_call.call_args_list
        if c.args[:2] == ("switch", "turn_on") and c.args[2].get("entity_id") == "switch.valve_orto"
    ]
    assert len(turn_on_calls) == 1, "fallback turn_on must be issued exactly once"


async def test_volume_preset_no_turn_on_when_auto_opened(hass_mock, di_sensor):
    """If the smart valve reports 'on' within the grace window, no turn_on is sent."""
    cfg = {
        CONF_ZONE_NAME: "Orto",
        CONF_ZONE_VALVE: "switch.valve_orto",
        CONF_ZONE_AREA: 20.0,
        CONF_ZONE_SYSTEM_TYPE: SYSTEM_TYPE_CUSTOM,
        CONF_ZONE_EFFICIENCY: 0.90,
        CONF_ZONE_FLOW_RATE: 8.0,
        CONF_ZONE_THRESHOLD: 15.0,
        CONF_ZONE_DELIVERY_MODE: "volume_preset",
        CONF_ZONE_VOLUME_ENTITY: "number.valve_volume",
    }
    zone = IrrigationZoneSensor(hass_mock, cfg, di_sensor)
    zone._zone_deficit = 5.0

    # First the valve is on (auto-opened) and then it closes itself.
    states = iter(
        [
            MagicMock(state="on"),  # grace poll → auto-open detected
            MagicMock(state="on"),  # main loop poll #1
            MagicMock(state="off"),  # main loop poll #2 → done
        ]
    )

    def _state_for(_entity_id):
        return next(states, MagicMock(state="off"))

    hass_mock.states.get = MagicMock(side_effect=_state_for)

    ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)
    ctrl.auto_open_grace_s = 0.05

    delivered = await ctrl._deliver_volume_preset(zone)
    assert delivered == zone.volume_liters

    turn_on_calls = [c for c in hass_mock.services.async_call.call_args_list if c.args[:2] == ("switch", "turn_on")]
    assert turn_on_calls == [], "no fallback turn_on when the valve auto-opened"


# ── Real-time deficit update (snapshot-based) ────────────────────────


def test_update_deficit_realtime_is_idempotent(hass_mock, di_sensor):
    """Multiple calls with growing delivered always recompute from the snapshot.

    Regression: previously the partial-irrigation settle subtracted
    delivered_mm from the *current* deficit, which double-counted any
    intermediate update. The new path uses an absolute snapshot-based
    formula so intermediate writes converge cleanly.
    """
    zone = _make_zone_orto(hass_mock, di_sensor)
    zone._zone_deficit = 12.0
    zone._deficit_at_irrigation_start = 12.0

    ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)

    # 5 L delivered → 5 * 0.9 / 20 = 0.225 mm consumed → 11.775 mm left.
    ctrl._update_deficit_realtime(zone, 5.0)
    assert zone._zone_deficit == pytest.approx(11.775, rel=1e-3)

    # 50 L delivered → 50 * 0.9 / 20 = 2.25 mm consumed → 9.75 mm left.
    ctrl._update_deficit_realtime(zone, 50.0)
    assert zone._zone_deficit == pytest.approx(9.75, rel=1e-3)

    # Over-shooting target clamps at 0.
    ctrl._update_deficit_realtime(zone, 10_000.0)
    assert zone._zone_deficit == 0.0


def test_update_deficit_realtime_updates_session_water(hass_mock, di_sensor):
    """Session water rises live during a flow-metered cycle (field report:
    Volume/Duration counted down while Session water stayed 0)."""
    zone = _make_zone_orto(hass_mock, di_sensor)
    zone._zone_deficit = 12.0
    zone._deficit_at_irrigation_start = 12.0
    ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)

    ctrl._update_deficit_realtime(zone, 5.0)
    assert zone._session_water_delivered == pytest.approx(5.0)
    ctrl._update_deficit_realtime(zone, 12.5)
    assert zone._session_water_delivered == pytest.approx(12.5)


def test_update_deficit_realtime_skipped_when_no_active_cycle(hass_mock, di_sensor):
    """Without a snapshot the helper must leave the deficit untouched."""
    zone = _make_zone_orto(hass_mock, di_sensor)
    zone._zone_deficit = 7.5
    zone._deficit_at_irrigation_start = None
    ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)
    ctrl._update_deficit_realtime(zone, 100.0)
    assert zone._zone_deficit == 7.5


def test_update_deficit_realtime_skipped_for_zero_area(hass_mock, di_sensor):
    """A misconfigured zone with area=0 must not divide by zero."""
    zone = _make_zone_orto(hass_mock, di_sensor)
    zone._zone_deficit = 5.0
    zone._deficit_at_irrigation_start = 5.0
    zone._area = 0.0
    ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)
    ctrl._update_deficit_realtime(zone, 10.0)
    assert zone._zone_deficit == 5.0


async def test_irrigate_zones_clears_snapshot_after_cycle(hass_mock, di_sensor):
    """The snapshot attribute must be cleared at the end of a clean cycle."""
    zone = _make_zone_orto(hass_mock, di_sensor)
    zone._zone_deficit = 5.0

    ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)

    async def _fake_deliver(z):
        # Intermediate real-time write to exercise the path.
        ctrl._update_deficit_realtime(z, z.volume_liters / 2)
        return z.volume_liters

    ctrl._deliver_water = _fake_deliver  # type: ignore[assignment]
    await ctrl._irrigate_zones(["Orto"])
    assert zone._deficit_at_irrigation_start is None


async def test_irrigate_zones_clears_snapshot_on_abort(hass_mock, di_sensor):
    """Even when the cycle aborts mid-delivery, the snapshot is cleared."""
    zone = _make_zone_orto(hass_mock, di_sensor)
    zone._zone_deficit = 5.0

    ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)

    async def _exploding_deliver(_z):
        raise RuntimeError("simulated crash")

    ctrl._deliver_water = _exploding_deliver  # type: ignore[assignment]
    await ctrl._irrigate_zones(["Orto"])
    assert zone._deficit_at_irrigation_start is None


async def test_settle_is_idempotent_with_realtime_updates(hass_mock, di_sensor):
    """End-of-cycle settle yields the same deficit as the last real-time write."""
    zone = _make_zone_orto(hass_mock, di_sensor)
    zone._zone_deficit = 10.0  # mm

    ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)

    async def _fake_deliver(z):
        # Snapshot was set by _irrigate_zones before this call. Simulate
        # a flow-meter loop writing real-time updates per poll.
        for litres in (5.0, 10.0, 25.0):
            ctrl._update_deficit_realtime(z, litres)
        # Return a partial delivery so the settle exercises the partial branch.
        return 25.0

    ctrl._deliver_water = _fake_deliver  # type: ignore[assignment]
    await ctrl._irrigate_zones(["Orto"])

    # 25 L * 0.9 / 20 m² = 1.125 mm consumed → 10 - 1.125 = 8.875 mm.
    assert zone._zone_deficit == pytest.approx(8.875, rel=1e-3)


# ── _last_irrigated regression coverage ──────────────────────────────


async def test_partial_irrigation_updates_last_irrigated(hass_mock, di_sensor):
    """A partial delivery (delivered < target) must still bump _last_irrigated.

    Regression: before this fix, the partial-irrigation branch in
    ``_irrigate_zones`` updated volume counters but forgot to stamp
    ``_last_irrigated`` and ``_last_irrigation_source``, leaving the UI
    looking like nothing had happened.
    """
    zone = _make_zone_orto(hass_mock, di_sensor)
    # Force a positive deficit and a target volume to deliver.
    zone._zone_deficit = 10.0

    op = _fake_operator()
    ctrl = IrrigationController(
        hass_mock,
        di_sensor,
        [zone],
        inter_zone_delay=0,
        valve_operators={zone.valve: op},
    )

    # Force a partial delivery: deliver half of the requested volume.
    target_volume = zone.volume_liters
    partial = target_volume / 2

    async def _fake_deliver(_zone):
        """Return a partial delivery so the partial-branch runs."""
        return partial

    ctrl._deliver_water = _fake_deliver  # type: ignore[assignment]
    assert zone._last_irrigated is None

    await ctrl._irrigate_zones(["Orto"])

    assert zone._last_irrigated is not None, "partial irrigation must stamp _last_irrigated"
    assert zone._last_irrigation_source in ("automatic", None)  # legacy or new
    assert zone._last_volume_delivered == round(partial, 1)


# ── Unreachable-at-irrigation notification ───────────────────────────


async def test_open_precheck_failed_fires_unreachable_notification(hass_mock, di_sensor):
    """A PRECHECK_FAILED open result fires UNREACHABLE_AT_IRRIGATION."""
    from never_dry.valve_notifier import NotificationKind, ValveNotifier

    zone = _make_zone_orto(hass_mock, di_sensor)
    zone._zone_deficit = 5.0
    notifier = ValveNotifier(hass_mock)
    op = _fake_operator(
        open_result=OperationResult(OperationStatus.PRECHECK_FAILED, "switch_unavailable"),
    )
    ctrl = IrrigationController(
        hass_mock,
        di_sensor,
        [zone],
        inter_zone_delay=0,
        valve_operators={zone.valve: op},
        notifier=notifier,
    )

    delivered = await ctrl._deliver_estimated_flow(zone)
    assert delivered == 0.0
    assert notifier.is_active("Orto", NotificationKind.UNREACHABLE_AT_IRRIGATION)


async def test_open_failed_does_not_fire_unreachable_notification(hass_mock, di_sensor):
    """A FAILED (not PRECHECK_FAILED) open does *not* fire the unreachable kind.

    The operator already raises COMMAND_FAILED in that case; we must not
    double-notify with UNREACHABLE_AT_IRRIGATION.
    """
    from never_dry.valve_notifier import NotificationKind, ValveNotifier

    zone = _make_zone_orto(hass_mock, di_sensor)
    zone._zone_deficit = 5.0
    notifier = ValveNotifier(hass_mock)
    op = _fake_operator(
        open_result=OperationResult(OperationStatus.FAILED, "open_failed"),
    )
    ctrl = IrrigationController(
        hass_mock,
        di_sensor,
        [zone],
        inter_zone_delay=0,
        valve_operators={zone.valve: op},
        notifier=notifier,
    )

    await ctrl._deliver_estimated_flow(zone)
    assert not notifier.is_active("Orto", NotificationKind.UNREACHABLE_AT_IRRIGATION)


async def test_volume_preset_unavailable_valve_notifies_and_aborts(hass_mock, di_sensor):
    """volume_preset pre-checks the switch and notifies if unavailable."""
    from never_dry.valve_notifier import NotificationKind, ValveNotifier

    cfg = {
        CONF_ZONE_NAME: "Orto",
        CONF_ZONE_VALVE: "switch.valve_orto",
        CONF_ZONE_AREA: 20.0,
        CONF_ZONE_SYSTEM_TYPE: SYSTEM_TYPE_CUSTOM,
        CONF_ZONE_EFFICIENCY: 0.90,
        CONF_ZONE_FLOW_RATE: 8.0,
        CONF_ZONE_THRESHOLD: 15.0,
        CONF_ZONE_DELIVERY_MODE: "volume_preset",
        CONF_ZONE_VOLUME_ENTITY: "number.valve_volume",
    }
    zone = IrrigationZoneSensor(hass_mock, cfg, di_sensor)
    zone._zone_deficit = 5.0

    # Switch reports unavailable.
    hass_mock.states.get = MagicMock(return_value=MagicMock(state="unavailable"))

    notifier = ValveNotifier(hass_mock)
    ctrl = IrrigationController(
        hass_mock,
        di_sensor,
        [zone],
        inter_zone_delay=0,
        notifier=notifier,
    )

    delivered = await ctrl._deliver_volume_preset(zone)
    assert delivered == 0.0
    assert notifier.is_active("Orto", NotificationKind.UNREACHABLE_AT_IRRIGATION)
    # No number.set_value should have been sent.
    set_value_calls = [c for c in hass_mock.services.async_call.call_args_list if c.args[:2] == ("number", "set_value")]
    assert set_value_calls == []


async def test_volume_preset_missing_switch_entity_notifies(hass_mock, di_sensor):
    """volume_preset reports switch_entity_not_found when state is None."""
    from never_dry.valve_notifier import NotificationKind, ValveNotifier

    cfg = {
        CONF_ZONE_NAME: "Orto",
        CONF_ZONE_VALVE: "switch.valve_orto",
        CONF_ZONE_AREA: 20.0,
        CONF_ZONE_SYSTEM_TYPE: SYSTEM_TYPE_CUSTOM,
        CONF_ZONE_EFFICIENCY: 0.90,
        CONF_ZONE_FLOW_RATE: 8.0,
        CONF_ZONE_THRESHOLD: 15.0,
        CONF_ZONE_DELIVERY_MODE: "volume_preset",
        CONF_ZONE_VOLUME_ENTITY: "number.valve_volume",
    }
    zone = IrrigationZoneSensor(hass_mock, cfg, di_sensor)
    zone._zone_deficit = 5.0
    hass_mock.states.get = MagicMock(return_value=None)

    notifier = ValveNotifier(hass_mock)
    ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0, notifier=notifier)

    delivered = await ctrl._deliver_volume_preset(zone)
    assert delivered == 0.0
    assert notifier.is_active("Orto", NotificationKind.UNREACHABLE_AT_IRRIGATION)
    active = notifier._active[("Orto", NotificationKind.UNREACHABLE_AT_IRRIGATION)]
    assert active.context["reason"] == "switch_entity_not_found"


async def test_unreachable_notification_dedupes_across_retries(hass_mock, di_sensor):
    """Multiple consecutive presses on an unavailable valve produce one notification."""
    from never_dry.valve_notifier import ValveNotifier

    zone = _make_zone_orto(hass_mock, di_sensor)
    zone._zone_deficit = 5.0
    notifier = ValveNotifier(hass_mock)
    op = _fake_operator(
        open_result=OperationResult(OperationStatus.PRECHECK_FAILED, "switch_unavailable"),
    )
    ctrl = IrrigationController(
        hass_mock,
        di_sensor,
        [zone],
        inter_zone_delay=0,
        valve_operators={zone.valve: op},
        notifier=notifier,
    )

    for _ in range(3):
        await ctrl._deliver_estimated_flow(zone)

    create_calls = [
        c for c in hass_mock.services.async_call.call_args_list if c.args[:2] == ("persistent_notification", "create")
    ]
    assert len(create_calls) == 1


async def test_volume_preset_stop_during_run(hass_mock, di_sensor):
    """A stop signal during volume_preset issues switch.turn_off and returns 0."""
    cfg = {
        CONF_ZONE_NAME: "Orto",
        CONF_ZONE_VALVE: "switch.valve_orto",
        CONF_ZONE_AREA: 20.0,
        CONF_ZONE_SYSTEM_TYPE: SYSTEM_TYPE_CUSTOM,
        CONF_ZONE_EFFICIENCY: 0.90,
        CONF_ZONE_FLOW_RATE: 8.0,
        CONF_ZONE_THRESHOLD: 15.0,
        CONF_ZONE_DELIVERY_MODE: "volume_preset",
        CONF_ZONE_VOLUME_ENTITY: "number.valve_volume",
    }
    zone = IrrigationZoneSensor(hass_mock, cfg, di_sensor)
    zone._zone_deficit = 5.0
    on_state = MagicMock(state="on")
    hass_mock.states.get = MagicMock(return_value=on_state)

    ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)
    ctrl.auto_open_grace_s = 0.05
    ctrl._stop_requested = True

    delivered = await ctrl._deliver_volume_preset(zone)
    assert delivered == 0.0
    turn_off_calls = [c for c in hass_mock.services.async_call.call_args_list if c.args[:2] == ("switch", "turn_off")]
    assert any(c.args[2].get("entity_id") == "switch.valve_orto" for c in turn_off_calls)


# ── Real-operator watchdog integration (AI-161, matrix WDOG cells) ────


async def test_watchdog_real_operator_mid_estimated_flow_settles_once(hass_mock, di_sensor):
    """A REAL ZoneDriver watchdog fires mid estimated_flow delivery.

    The [M] race-matrix tests simulate the watchdog as a bare state flip;
    this test exercises the production seam end to end: FSM-driven open,
    the watchdog task actually firing (max_open_duration_s), the forced
    switch.turn_off, the state echo feeding both the operator FSM and the
    controller listener, the delivery loop perceiving the off state, and
    the idempotent _close_valve on an already-IDLE driver. Contract:
    elapsed-fraction credit, a large residual deficit survives, exactly
    one settle, and the close is never attributed to a manual irrigation.
    """
    import asyncio as _asyncio

    from never_dry.driver import ZoneDriver
    from never_dry.valve_fsm import FsmConfig

    cfg = {
        CONF_ZONE_NAME: "Orto",
        CONF_ZONE_VALVE: "switch.valve_orto",
        CONF_ZONE_AREA: 20.0,
        CONF_ZONE_SYSTEM_TYPE: SYSTEM_TYPE_CUSTOM,
        CONF_ZONE_EFFICIENCY: 0.90,
        CONF_ZONE_FLOW_RATE: 8.0,
        CONF_ZONE_THRESHOLD: 15.0,
        CONF_ZONE_DELIVERY_MODE: "estimated_flow",
    }
    zone = IrrigationZoneSensor(hass_mock, cfg, di_sensor)
    # 0.024 mm on 20 m² at η=0.9 → 0.53 L → 4 s plan at 8 L/min; the
    # watchdog fires at 1.5 s, so a large residual must survive.
    zone._zone_deficit = 0.024
    deficit_pre = zone._zone_deficit

    ctrl = IrrigationController(hass_mock, di_sensor, [zone], inter_zone_delay=0)

    op = ZoneDriver(
        hass_mock,
        "switch.valve_orto",
        flow_meter_sensor=None,
        name="Orto",
        fsm_config=FsmConfig(
            has_flow_meter=False,
            open_timeout_s=0.5,
            close_timeout_s=0.5,
            flow_verify_timeout_s=0.5,
            leak_timeout_s=0.5,
            max_consecutive_failures=3,
        ),
        max_retries=0,
        backoff_s=(0.01,),
        max_open_duration_s=1.5,
    )
    ctrl._valve_operators = {"switch.valve_orto": op}

    valve = {"state": "off"}

    def _controller_event(old, new):
        event = MagicMock()
        event.data = {
            "entity_id": "switch.valve_orto",
            "old_state": MagicMock(state=old),
            "new_state": MagicMock(state=new),
        }
        return event

    async def _dispatch(new_state):
        """Echo the state change to the operator FSM and the controller
        listener — the push event always beats the loop's poll in the field."""
        old = valve["state"]
        valve["state"] = new_state
        await op._handle_switch_state(MagicMock(data={"new_state": MagicMock(state=new_state)}))
        ctrl._on_valve_state_change(_controller_event(old, new_state))

    async def _service_call(domain, service, data=None, **kwargs):
        if domain == "switch" and data and data.get("entity_id") == "switch.valve_orto":
            new = "on" if service == "turn_on" else "off"
            # Echo unconditionally (Z2M republishes state on every command).
            _asyncio.get_running_loop().create_task(_dispatch(new))

    hass_mock.services.async_call = AsyncMock(side_effect=_service_call)
    hass_mock.states.get = lambda eid: MagicMock(state=valve["state"]) if eid == "switch.valve_orto" else None

    await ctrl._irrigate_zones(["Orto"])

    # Watchdog cut the 4 s plan at ~1.5-3 s: partial credit, single settle.
    assert valve["state"] == "off"
    assert zone.is_irrigating is False
    assert 0.0 < zone._zone_deficit < deficit_pre
    assert zone._last_volume_delivered > 0
    assert zone._last_irrigation_source != "manual"
    assert 1 <= zone._last_session_duration_s <= 4
    assert op.state == ValveState.IDLE
