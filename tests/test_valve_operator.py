"""Tests for valve_operator — the HA-aware wrapper around ValveFsm."""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import AsyncMock, MagicMock

import pytest
from never_dry.valve_fsm import FailureKind, FsmConfig, ValveState
from never_dry.valve_operator import OperationResult, OperationStatus, ValveOperator

# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def hass():
    """Mock HomeAssistant instance suitable for ValveOperator tests."""
    hass = MagicMock()
    hass.states = MagicMock()
    hass.states.get = MagicMock(return_value=MagicMock(state="off"))
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()

    def _create_task(coro):
        try:
            return asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            coro.close()
            return MagicMock()

    hass.async_create_task = _create_task
    return hass


def _fast_fsm_config(has_flow_meter: bool) -> FsmConfig:
    """Return an FSM config with tiny timeouts for snappy tests."""
    return FsmConfig(
        has_flow_meter=has_flow_meter,
        open_timeout_s=0.05,
        close_timeout_s=0.05,
        flow_verify_timeout_s=0.05,
        leak_timeout_s=0.05,
        max_consecutive_failures=3,
    )


def _make_operator(
    hass,
    *,
    has_flow_meter: bool = False,
    max_retries: int = 0,
    backoff_s: tuple[float, ...] = (0.01,),
) -> ValveOperator:
    """Build a ValveOperator wired to the mock HA with fast timeouts."""
    return ValveOperator(
        hass=hass,
        switch_entity_id="switch.valve",
        flow_sensor_entity_id="sensor.flow" if has_flow_meter else None,
        zone_name="testzone",
        fsm_config=_fast_fsm_config(has_flow_meter),
        max_retries=max_retries,
        backoff_s=backoff_s,
    )


def _state_event(value: str) -> MagicMock:
    """Build a mock state-change event carrying the given new value."""
    event = MagicMock()
    event.data = {"new_state": MagicMock(state=value)}
    return event


async def _yield_loop(times: int = 3) -> None:
    """Yield to the asyncio loop ``times`` times so scheduled tasks run."""
    for _ in range(times):
        await asyncio.sleep(0)


# ── Initial state ─────────────────────────────────────────────────────


def test_initial_state_is_idle(hass):
    """A fresh operator starts in IDLE, not in maintenance."""
    op = _make_operator(hass)
    assert op.state == ValveState.IDLE
    assert op.is_in_maintenance is False
    assert op.failure_count == 0


# ── Pre-checks ────────────────────────────────────────────────────────


async def test_precheck_switch_entity_not_found(hass):
    """Opening returns PRECHECK_FAILED when the switch entity is missing."""
    hass.states.get.return_value = None
    op = _make_operator(hass)
    result = await op.open()
    assert result.status == OperationStatus.PRECHECK_FAILED
    assert result.error_detail == "switch_entity_not_found"
    hass.services.async_call.assert_not_called()


async def test_precheck_switch_unavailable(hass):
    """Opening returns PRECHECK_FAILED when the switch is unavailable."""
    hass.states.get.return_value = MagicMock(state="unavailable")
    op = _make_operator(hass)
    result = await op.open()
    assert result.status == OperationStatus.PRECHECK_FAILED
    assert result.error_detail == "switch_unavailable"


# ── Happy paths ──────────────────────────────────────────────────────


async def test_open_happy_path_no_flow_meter(hass):
    """Open completes successfully when switch state confirms quickly."""
    op = _make_operator(hass)

    async def simulate():
        await _yield_loop()
        await op._handle_switch_state(_state_event("on"))

    sim = asyncio.create_task(simulate())
    result = await op.open()
    await sim

    assert result.status == OperationStatus.OK
    assert result.retries_used == 0
    assert result.duration_ms > 0
    hass.services.async_call.assert_any_call("switch", "turn_on", {"entity_id": "switch.valve"}, blocking=False)
    assert op.state == ValveState.OPEN


async def test_open_happy_path_with_flow_meter(hass):
    """Open completes when both switch and flow confirm."""
    op = _make_operator(hass, has_flow_meter=True)

    async def simulate():
        await _yield_loop()
        await op._handle_switch_state(_state_event("on"))
        await _yield_loop()
        await op._handle_flow_state(_state_event("0.5"))

    sim = asyncio.create_task(simulate())
    result = await op.open()
    await sim

    assert result.status == OperationStatus.OK
    assert op.state == ValveState.OPEN_VERIFIED


async def test_close_happy_path_no_flow_meter(hass):
    """Close completes when switch reports off."""
    op = _make_operator(hass)

    # Drive the FSM to OPEN first.
    async def open_sim():
        await _yield_loop()
        await op._handle_switch_state(_state_event("on"))

    _bg = asyncio.create_task(open_sim())
    await op.open()
    await _bg

    async def close_sim():
        await _yield_loop()
        await op._handle_switch_state(_state_event("off"))

    sim = asyncio.create_task(close_sim())
    result = await op.close()
    await sim

    assert result.status == OperationStatus.OK
    hass.services.async_call.assert_any_call("switch", "turn_off", {"entity_id": "switch.valve"}, blocking=False)
    assert op.state == ValveState.IDLE


async def test_close_happy_path_with_flow_meter(hass):
    """Close completes when flow drops to zero after switch off."""
    op = _make_operator(hass, has_flow_meter=True)

    async def open_sim():
        await _yield_loop()
        await op._handle_switch_state(_state_event("on"))
        await _yield_loop()
        await op._handle_flow_state(_state_event("0.5"))

    _bg = asyncio.create_task(open_sim())
    await op.open()
    await _bg

    async def close_sim():
        await _yield_loop()
        await op._handle_switch_state(_state_event("off"))
        await _yield_loop()
        await op._handle_flow_state(_state_event("0.0"))

    sim = asyncio.create_task(close_sim())
    result = await op.close()
    await sim

    assert result.status == OperationStatus.OK
    assert op.state == ValveState.IDLE
    assert op.failure_count == 0


# ── Retries on transient failures ────────────────────────────────────


async def test_open_fails_when_switch_never_confirms(hass):
    """With max_retries=0, an open-timeout returns FAILED immediately."""
    op = _make_operator(hass, max_retries=0)
    result = await op.open()
    assert result.status == OperationStatus.FAILED
    assert result.error_detail == FailureKind.OPEN_FAILED.value
    assert result.retries_used == 0


async def test_open_succeeds_after_one_retry(hass):
    """A transient open failure is retried; the second attempt succeeds."""
    op = _make_operator(hass, max_retries=2, backoff_s=(0.0,))
    attempt = {"count": 0}

    async def watcher():
        # Wait until the SECOND attempt has been dispatched (= retry).
        while attempt["count"] < 2:
            await _yield_loop()
        await op._handle_switch_state(_state_event("on"))

    # Track service calls to count attempts.
    real_call = hass.services.async_call

    async def counting_call(*args, **kwargs):
        if args[:2] == ("switch", "turn_on"):
            attempt["count"] += 1
        return await real_call(*args, **kwargs)

    hass.services.async_call = counting_call

    sim = asyncio.create_task(watcher())
    result = await op.open()
    await sim

    assert result.status == OperationStatus.OK
    assert result.retries_used >= 1


async def test_actuation_failure_not_retried(hass):
    """Switch on but no flow → ACTUATION_FAILED returns immediately."""
    op = _make_operator(hass, has_flow_meter=True, max_retries=5, backoff_s=(0.0,))

    async def simulate():
        await _yield_loop()
        await op._handle_switch_state(_state_event("on"))
        # No flow event → flow timer expires.

    sim = asyncio.create_task(simulate())
    result = await op.open()
    await sim

    assert result.status == OperationStatus.FAILED
    assert result.error_detail == FailureKind.ACTUATION_FAILED.value
    assert result.retries_used == 0


# ── AI-032: leak recovery + escalation ───────────────────────────────


async def _drive_open_then_close_leak(op, hass):
    """Helper: drive the operator to a CLOSE_LEAK failure and return the result."""

    async def open_sim():
        """Take the operator from IDLE to OPEN_VERIFIED."""
        await _yield_loop()
        await op._handle_switch_state(_state_event("on"))
        await _yield_loop()
        await op._handle_flow_state(_state_event("0.5"))

    _bg = asyncio.create_task(open_sim())
    await op.open()
    await _bg

    async def close_sim():
        """Confirm switch off but leave flow positive → leak."""
        await _yield_loop()
        await op._handle_switch_state(_state_event("off"))

    sim = asyncio.create_task(close_sim())
    return await op.close(), sim


async def test_close_leak_recovery_succeeds(hass):
    """If the post-leak ``turn_off`` makes the flow drop, close returns OK."""
    op = _make_operator(hass, has_flow_meter=True, max_retries=0, backoff_s=(0.0,))

    flow_value = {"value": "0.5"}

    def _state_for(entity_id):
        if entity_id == "sensor.flow":
            return MagicMock(state=flow_value["value"])
        return MagicMock(state="off")

    hass.states.get = MagicMock(side_effect=_state_for)

    real_call = hass.services.async_call

    async def call_then_drop_flow(*args, **kwargs):
        if args[:2] == ("switch", "turn_off"):
            flow_value["value"] = "0.0"
        return await real_call(*args, **kwargs)

    hass.services.async_call = call_then_drop_flow

    result, sim = await _drive_open_then_close_leak(op, hass)
    await sim

    assert result.status == OperationStatus.OK
    assert result.error_detail == "leak_recovered"


async def test_close_leak_recovery_fails_triggers_emergency_stop(hass):
    """When recovery cannot clear the leak, ``never_dry.stop`` is invoked."""
    op = _make_operator(hass, has_flow_meter=True, max_retries=0, backoff_s=(0.0,))

    def _state_for(entity_id):
        if entity_id == "sensor.flow":
            return MagicMock(state="0.5")
        return MagicMock(state="off")

    hass.states.get = MagicMock(side_effect=_state_for)

    result, sim = await _drive_open_then_close_leak(op, hass)
    await sim

    assert result.status == OperationStatus.FAILED
    assert result.error_detail == FailureKind.CLOSE_LEAK.value

    stop_calls = [c for c in hass.services.async_call.call_args_list if c.args[:2] == ("never_dry", "stop")]
    assert len(stop_calls) == 1


async def test_close_leak_recovery_attempted_once(hass):
    """The recovery flag prevents a second attempt within the same close()."""
    op = _make_operator(hass, has_flow_meter=True, max_retries=0, backoff_s=(0.0,))

    def _state_for(entity_id):
        if entity_id == "sensor.flow":
            return MagicMock(state="0.5")
        return MagicMock(state="off")

    hass.states.get = MagicMock(side_effect=_state_for)

    result, sim = await _drive_open_then_close_leak(op, hass)
    await sim

    # Two turn_off calls: one from the FSM during REQ_CLOSE, one from
    # the recovery attempt. Never three.
    turn_off_calls = [c for c in hass.services.async_call.call_args_list if c.args[:2] == ("switch", "turn_off")]
    assert 1 <= len(turn_off_calls) <= 2
    assert result.status == OperationStatus.FAILED


async def test_close_leak_recovery_resets_between_close_calls(hass):
    """A second close() call must be able to retry recovery again."""
    op = _make_operator(hass, has_flow_meter=True, max_retries=0, backoff_s=(0.0,))

    def _state_for(entity_id):
        if entity_id == "sensor.flow":
            return MagicMock(state="0.5")
        return MagicMock(state="off")

    hass.states.get = MagicMock(side_effect=_state_for)

    # First close → leak + recovery attempt
    _, sim1 = await _drive_open_then_close_leak(op, hass)
    await sim1

    # Operator went through MAINTENANCE? Reset it.
    if op.is_in_maintenance:
        await op.reset_maintenance()

    # Re-open and re-leak
    _, sim2 = await _drive_open_then_close_leak(op, hass)
    await sim2

    # Both attempts should have called never_dry.stop independently.
    stop_calls = [c for c in hass.services.async_call.call_args_list if c.args[:2] == ("never_dry", "stop")]
    assert len(stop_calls) >= 2


async def test_close_leak_not_retried(hass):
    """Switch off but flow persists → CLOSE_LEAK returns immediately, no retry."""
    op = _make_operator(hass, has_flow_meter=True, max_retries=5, backoff_s=(0.0,))

    async def open_sim():
        await _yield_loop()
        await op._handle_switch_state(_state_event("on"))
        await _yield_loop()
        await op._handle_flow_state(_state_event("0.5"))

    _bg = asyncio.create_task(open_sim())
    await op.open()
    await _bg

    async def close_sim():
        await _yield_loop()
        await op._handle_switch_state(_state_event("off"))
        # Flow stays > threshold; leak timer expires.

    sim = asyncio.create_task(close_sim())
    result = await op.close()
    await sim

    assert result.status == OperationStatus.FAILED
    assert result.error_detail == FailureKind.CLOSE_LEAK.value
    assert result.retries_used == 0


# ── Maintenance ──────────────────────────────────────────────────────


async def test_three_consecutive_failures_enter_maintenance(hass):
    """Three open timeouts in a row lock the operator in MAINTENANCE."""
    op = _make_operator(hass, max_retries=0)
    for _ in range(3):
        await op.open()
    assert op.is_in_maintenance is True


async def test_open_in_maintenance_returns_maintenance_status(hass):
    """Once locked, open() refuses without touching switch services."""
    op = _make_operator(hass, max_retries=0)
    for _ in range(3):
        await op.open()
    hass.services.async_call.reset_mock()
    result = await op.open()
    assert result.status == OperationStatus.MAINTENANCE
    hass.services.async_call.assert_not_called()


async def test_reset_maintenance_clears_state(hass):
    """``reset_maintenance`` returns the operator to IDLE with a zero counter."""
    op = _make_operator(hass, max_retries=0)
    for _ in range(3):
        await op.open()
    await op.reset_maintenance()
    assert op.is_in_maintenance is False
    assert op.failure_count == 0


# ── Unavailable / available ──────────────────────────────────────────


async def test_switch_unavailable_during_op_moves_to_unreachable(hass):
    """An unavailable observation during an open cycle parks the FSM in UNREACHABLE."""
    op = _make_operator(hass, max_retries=0)

    async def simulate():
        await _yield_loop()
        await op._handle_switch_state(_state_event("unavailable"))

    sim = asyncio.create_task(simulate())
    # The open will not complete OK; we expect FAILED or be parked. Use a
    # short timeout to avoid hanging if the operator misbehaves.
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(op.open(), timeout=0.2)
    await sim
    assert op.state == ValveState.UNREACHABLE


# ── Unload ───────────────────────────────────────────────────────────


def test_async_unload_releases_subscriptions(hass):
    """Unload calls the unsubscribe handles returned by the HA helper."""
    op = _make_operator(hass, has_flow_meter=True)
    op.async_unload()
    # async_track_state_change_event is mocked to MagicMock(); calling its
    # return value should have been requested by async_unload.
    assert op._unsub_switch.called
    assert op._unsub_flow.called


# ── Service exception ────────────────────────────────────────────────


async def test_switch_service_exception_is_caught(hass, caplog):
    """A raising switch service does not crash the operator."""
    hass.services.async_call = AsyncMock(side_effect=RuntimeError("boom"))
    op = _make_operator(hass, max_retries=0)
    result = await op.open()
    # The FSM still drives to OPEN_FAILED via the open timeout.
    assert result.status == OperationStatus.FAILED
    assert "boom" in caplog.text


# ── Coverage of less-traveled branches ───────────────────────────────


async def test_is_retryable_rejects_garbage_error_detail(hass):
    """An unknown error_detail string is treated as non-transient."""
    op = _make_operator(hass)
    outcome = OperationResult(OperationStatus.FAILED, "not_a_real_failure_kind")
    assert op._is_retryable(outcome) is False


async def test_backoff_for_handles_empty_tuple(hass):
    """An empty backoff tuple yields a zero sleep."""
    op = _make_operator(hass, backoff_s=())
    assert op._backoff_for(0) == 0.0


def test_make_operator_uses_default_backoff_when_none(hass):
    """Passing ``backoff_s=None`` activates the class default."""
    op = ValveOperator(
        hass=hass,
        switch_entity_id="switch.valve",
        zone_name="z",
        fsm_config=_fast_fsm_config(False),
        backoff_s=None,
    )
    assert op._backoff_s == ValveOperator.DEFAULT_BACKOFF_S


async def test_notifier_receives_command_failed_on_open_fail(hass):
    """An OPEN_FAILED with a notifier configured produces a notification."""
    from never_dry.valve_notifier import NotificationKind, Severity, ValveNotifier

    notifier = ValveNotifier(hass)
    op = ValveOperator(
        hass=hass,
        switch_entity_id="switch.valve",
        zone_name="z1",
        fsm_config=_fast_fsm_config(False),
        max_retries=0,
        backoff_s=(0.0,),
        notifier=notifier,
    )
    result = await op.open()
    assert result.status == OperationStatus.FAILED
    assert notifier.is_active("z1", NotificationKind.COMMAND_FAILED)
    active = notifier._active[("z1", NotificationKind.COMMAND_FAILED)]
    assert active.severity == Severity.WARNING


async def test_notifier_receives_stuck_open_on_close_leak(hass):
    """A CLOSE_LEAK with a notifier configured emits STUCK_OPEN CRITICAL."""
    from never_dry.valve_notifier import NotificationKind, Severity, ValveNotifier

    notifier = ValveNotifier(hass)
    op = ValveOperator(
        hass=hass,
        switch_entity_id="switch.valve",
        flow_sensor_entity_id="sensor.flow",
        zone_name="z2",
        fsm_config=_fast_fsm_config(True),
        max_retries=0,
        backoff_s=(0.0,),
        notifier=notifier,
    )

    def _state_for(entity_id):
        if entity_id == "sensor.flow":
            return MagicMock(state="0.5")
        return MagicMock(state="off")

    hass.states.get = MagicMock(side_effect=_state_for)
    result, sim = await _drive_open_then_close_leak(op, hass)
    await sim

    assert result.status == OperationStatus.FAILED
    assert notifier.is_active("z2", NotificationKind.STUCK_OPEN)
    active = notifier._active[("z2", NotificationKind.STUCK_OPEN)]
    assert active.severity == Severity.CRITICAL


async def test_notifier_receives_zone_disabled_on_maintenance(hass):
    """Three consecutive failures notify ZONE_DISABLED with CRITICAL severity."""
    from never_dry.valve_notifier import NotificationKind, ValveNotifier

    notifier = ValveNotifier(hass)
    op = ValveOperator(
        hass=hass,
        switch_entity_id="switch.valve",
        zone_name="z3",
        fsm_config=_fast_fsm_config(False),
        max_retries=0,
        backoff_s=(0.0,),
        notifier=notifier,
    )
    for _ in range(3):
        await op.open()
    assert notifier.is_active("z3", NotificationKind.ZONE_DISABLED)


async def test_leak_recovery_no_flow_meter_treats_as_unrecovered(hass):
    """Without a flow meter, _attempt_leak_recovery returns False."""
    op = _make_operator(hass, has_flow_meter=False)
    op._flow_sensor_entity_id = None
    recovered = await op._attempt_leak_recovery()
    assert recovered is False


async def test_leak_recovery_none_state_treats_as_unrecovered(hass):
    """If hass.states.get returns None during recovery, treat as unrecovered."""
    op = _make_operator(hass, has_flow_meter=True)
    hass.states.get = MagicMock(return_value=None)
    recovered = await op._attempt_leak_recovery()
    assert recovered is False


async def test_leak_recovery_unparseable_flow_treats_as_unrecovered(hass):
    """If the flow sensor reports a non-numeric value, treat as unrecovered."""
    op = _make_operator(hass, has_flow_meter=True)
    hass.states.get = MagicMock(return_value=MagicMock(state="unavailable"))
    recovered = await op._attempt_leak_recovery()
    assert recovered is False


async def test_escalate_stuck_open_handles_service_exception(hass, caplog):
    """If never_dry.stop raises, _escalate_stuck_open logs and continues."""
    op = _make_operator(hass, has_flow_meter=True)
    hass.services.async_call = AsyncMock(side_effect=RuntimeError("boom"))
    await op._escalate_stuck_open()
    assert "Failed to trigger emergency stop" in caplog.text


async def test_escalate_stuck_open_without_notifier(hass):
    """``_escalate_stuck_open`` is safe when no notifier is configured."""
    op = _make_operator(hass, has_flow_meter=True)
    assert op._notifier is None
    await op._escalate_stuck_open()


async def test_sync_callback_schedules_async_handler(hass):
    """The HA-facing sync callbacks must schedule the async handler."""
    op = _make_operator(hass)
    seen = []

    async def fake_handler(event):
        seen.append(event)

    op._handle_switch_state = fake_handler  # type: ignore[assignment]
    op._on_switch_state(_state_event("on"))
    await asyncio.sleep(0)
    assert seen


async def test_flow_sync_callback_schedules_async_handler(hass):
    """The flow sync callback also routes to the async handler."""
    op = _make_operator(hass, has_flow_meter=True)
    seen = []

    async def fake_handler(event):
        seen.append(event)

    op._handle_flow_state = fake_handler  # type: ignore[assignment]
    op._on_flow_state(_state_event("0.5"))
    await asyncio.sleep(0)
    assert seen


async def test_handle_switch_state_ignores_none_new_state(hass):
    """If the event has no new_state, the handler returns silently."""
    op = _make_operator(hass)
    event = MagicMock()
    event.data = {"new_state": None}
    await op._handle_switch_state(event)
    assert op.state == ValveState.IDLE


async def test_handle_switch_state_obs_available_on_recovery(hass):
    """A switch reporting ``on`` after UNREACHABLE dispatches OBS_AVAILABLE first."""
    op = _make_operator(hass)
    await op._handle_switch_state(_state_event("unavailable"))
    assert op.state == ValveState.UNREACHABLE
    await op._handle_switch_state(_state_event("on"))
    assert op.state == ValveState.IDLE


async def test_handle_flow_state_ignores_none_new_state(hass):
    """If the flow event has no new_state, the handler returns silently."""
    op = _make_operator(hass, has_flow_meter=True)
    event = MagicMock()
    event.data = {"new_state": None}
    await op._handle_flow_state(event)
    assert op.state == ValveState.IDLE


async def test_handle_flow_state_ignores_non_numeric(hass):
    """A non-parseable flow reading is silently ignored."""
    op = _make_operator(hass, has_flow_meter=True)
    await op._handle_flow_state(_state_event("unavailable"))
    assert op.state == ValveState.IDLE


# ── Timing ───────────────────────────────────────────────────────────


async def test_duration_ms_is_populated(hass):
    """``duration_ms`` is set to a positive value on every result."""
    op = _make_operator(hass)

    async def simulate():
        await _yield_loop()
        await op._handle_switch_state(_state_event("on"))

    sim = asyncio.create_task(simulate())
    result = await op.open()
    await sim

    assert result.duration_ms > 0.0


# ── AI-033: Absolute watchdog timer ──────────────────────────────────


def _make_operator_with_watchdog(
    hass,
    *,
    max_open_duration_s: float = 0.05,
    has_flow_meter: bool = False,
) -> ValveOperator:
    """Build a ValveOperator with a very short watchdog timeout for testing."""
    from never_dry.valve_notifier import ValveNotifier

    notifier = ValveNotifier(hass)
    return ValveOperator(
        hass=hass,
        switch_entity_id="switch.valve",
        flow_sensor_entity_id="sensor.flow" if has_flow_meter else None,
        zone_name="watchzone",
        fsm_config=_fast_fsm_config(has_flow_meter),
        max_retries=0,
        backoff_s=(0.0,),
        notifier=notifier,
        max_open_duration_s=max_open_duration_s,
    ), notifier


async def test_watchdog_fires_and_calls_turn_off(hass):
    """Watchdog triggers turn_off when valve stays open past max_open_duration_s."""
    op, _ = _make_operator_with_watchdog(hass, max_open_duration_s=0.05)

    async def open_sim():
        await _yield_loop()
        await op._handle_switch_state(_state_event("on"))

    bg = asyncio.create_task(open_sim())
    await op.open()
    await bg

    assert op.state == ValveState.OPEN
    hass.services.async_call.reset_mock()

    await asyncio.sleep(0.1)

    turn_off_calls = [c for c in hass.services.async_call.call_args_list if c.args[:2] == ("switch", "turn_off")]
    assert len(turn_off_calls) >= 1


async def test_watchdog_fires_critical_notification(hass):
    """Watchdog emits a WATCHDOG_TRIGGERED CRITICAL notification."""
    from never_dry.valve_notifier import NotificationKind

    op, notifier = _make_operator_with_watchdog(hass, max_open_duration_s=0.05)

    async def open_sim():
        await _yield_loop()
        await op._handle_switch_state(_state_event("on"))

    bg = asyncio.create_task(open_sim())
    await op.open()
    await bg

    await asyncio.sleep(0.1)

    assert notifier.is_active("watchzone", NotificationKind.WATCHDOG_TRIGGERED)


async def test_max_open_duration_accepts_callable(hass):
    """A callable max_open_duration_s is re-evaluated on each resolve (AI-150)."""
    current = {"value": 0.05}
    op, _ = _make_operator_with_watchdog(hass, max_open_duration_s=lambda: current["value"])

    assert op._current_max_open_duration() == pytest.approx(0.05)
    current["value"] = 7200.0
    assert op._current_max_open_duration() == pytest.approx(7200.0)


async def test_max_open_duration_static_still_works(hass):
    """A plain float keeps the pre-AI-150 behaviour."""
    op, _ = _make_operator_with_watchdog(hass, max_open_duration_s=123.0)
    assert op._current_max_open_duration() == pytest.approx(123.0)


async def test_watchdog_fires_with_callable_duration(hass):
    """The watchdog honours the provider value read at open time."""
    op, _ = _make_operator_with_watchdog(hass, max_open_duration_s=lambda: 0.05)

    async def open_sim():
        await _yield_loop()
        await op._handle_switch_state(_state_event("on"))

    bg = asyncio.create_task(open_sim())
    await op.open()
    await bg

    assert op.state == ValveState.OPEN
    hass.services.async_call.reset_mock()

    await asyncio.sleep(0.1)

    turn_off_calls = [c for c in hass.services.async_call.call_args_list if c.args[:2] == ("switch", "turn_off")]
    assert len(turn_off_calls) >= 1


async def test_watchdog_cancelled_on_normal_close(hass):
    """After a normal close, the watchdog task is cancelled and not pending."""
    op, _ = _make_operator_with_watchdog(hass, max_open_duration_s=5.0)

    async def open_sim():
        await _yield_loop()
        await op._handle_switch_state(_state_event("on"))

    bg = asyncio.create_task(open_sim())
    await op.open()
    await bg

    assert op._watchdog_task is not None

    async def close_sim():
        await _yield_loop()
        await op._handle_switch_state(_state_event("off"))

    sim = asyncio.create_task(close_sim())
    result = await op.close()
    await sim

    assert result.status == OperationStatus.OK
    assert op._watchdog_task is None


async def test_watchdog_not_restarted_on_open_to_open_verified(hass):
    """OPEN → OPEN_VERIFIED transition does not create a second watchdog task."""
    op, _ = _make_operator_with_watchdog(hass, max_open_duration_s=5.0, has_flow_meter=True)

    async def open_sim():
        await _yield_loop()
        await op._handle_switch_state(_state_event("on"))
        await _yield_loop()
        await op._handle_flow_state(_state_event("0.5"))

    bg = asyncio.create_task(open_sim())
    await op.open()
    await bg

    assert op.state == ValveState.OPEN_VERIFIED
    task_after_verified = op._watchdog_task
    assert task_after_verified is not None
    assert not task_after_verified.done()


async def test_watchdog_cancelled_on_unload(hass):
    """async_unload cancels a pending watchdog task."""
    op, _ = _make_operator_with_watchdog(hass, max_open_duration_s=5.0)

    async def open_sim():
        await _yield_loop()
        await op._handle_switch_state(_state_event("on"))

    bg = asyncio.create_task(open_sim())
    await op.open()
    await bg

    assert op._watchdog_task is not None
    op.async_unload()
    assert op._watchdog_task is None


async def test_watchdog_not_started_when_valve_not_open(hass):
    """No watchdog is started if the open attempt fails."""
    op, _ = _make_operator_with_watchdog(hass, max_open_duration_s=5.0)
    result = await op.open()
    assert result.status == OperationStatus.FAILED
    assert op._watchdog_task is None


# ── Hardware max-duration interlock ──────────────────────────────────


def _make_operator_with_hw_interlock(hass, *, max_open_duration_s: float = 5.0) -> ValveOperator:
    """Build an operator with both software watchdog and hw_max_duration_entity."""
    from never_dry.valve_notifier import ValveNotifier

    notifier = ValveNotifier(hass)
    return ValveOperator(
        hass=hass,
        switch_entity_id="switch.valve",
        zone_name="hwzone",
        fsm_config=_fast_fsm_config(False),
        max_retries=0,
        backoff_s=(0.0,),
        notifier=notifier,
        max_open_duration_s=max_open_duration_s,
        hw_max_duration_entity="number.hw_timer",
        hw_max_duration_multiplier=1.0,
    )


async def test_hw_max_duration_called_on_open(hass):
    """number.set_value is called on the hw entity when the valve opens."""
    op = _make_operator_with_hw_interlock(hass, max_open_duration_s=120.0)

    async def open_sim():
        await _yield_loop()
        await op._handle_switch_state(_state_event("on"))

    bg = asyncio.create_task(open_sim())
    await op.open()
    await bg
    await _yield_loop(5)

    set_value_calls = [c for c in hass.services.async_call.call_args_list if c.args[:2] == ("number", "set_value")]
    assert len(set_value_calls) == 1
    assert set_value_calls[0].args[2]["entity_id"] == "number.hw_timer"
    # No hw provider passed: the on-device timer mirrors the watchdog value.
    # The ladder itself is the zone's business, not the operator's.
    assert set_value_calls[0].args[2]["value"] == pytest.approx(120.0)


async def test_hw_max_duration_not_called_on_failed_open(hass):
    """number.set_value is NOT called when the open attempt fails."""
    op = _make_operator_with_hw_interlock(hass, max_open_duration_s=120.0)
    result = await op.open()
    assert result.status == OperationStatus.FAILED
    await _yield_loop(5)

    set_value_calls = [c for c in hass.services.async_call.call_args_list if c.args[:2] == ("number", "set_value")]
    assert len(set_value_calls) == 0


async def test_hw_max_duration_called_again_after_close_reopen(hass):
    """number.set_value is called again after a close+reopen cycle."""
    op = _make_operator_with_hw_interlock(hass, max_open_duration_s=60.0)

    async def open_sim():
        await _yield_loop()
        await op._handle_switch_state(_state_event("on"))

    bg = asyncio.create_task(open_sim())
    await op.open()
    await bg
    await _yield_loop(5)

    async def close_sim():
        await _yield_loop()
        await op._handle_switch_state(_state_event("off"))

    sim = asyncio.create_task(close_sim())
    await op.close()
    await sim

    hass.services.async_call.reset_mock()

    bg2 = asyncio.create_task(open_sim())
    await op.open()
    await bg2
    await _yield_loop(5)

    set_value_calls = [c for c in hass.services.async_call.call_args_list if c.args[:2] == ("number", "set_value")]
    assert len(set_value_calls) == 1


async def test_hw_max_duration_called_once_per_open(hass):
    """OPEN → OPEN_VERIFIED transition does not call number.set_value a second time."""
    from never_dry.valve_notifier import ValveNotifier

    notifier = ValveNotifier(hass)
    op = ValveOperator(
        hass=hass,
        switch_entity_id="switch.valve",
        flow_sensor_entity_id="sensor.flow",
        zone_name="hwzone2",
        fsm_config=_fast_fsm_config(True),
        max_retries=0,
        backoff_s=(0.0,),
        notifier=notifier,
        max_open_duration_s=60.0,
        hw_max_duration_entity="number.hw_timer",
        hw_max_duration_multiplier=1.0,
    )

    async def open_sim():
        await _yield_loop()
        await op._handle_switch_state(_state_event("on"))
        await _yield_loop()
        await op._handle_flow_state(_state_event("0.5"))

    bg = asyncio.create_task(open_sim())
    await op.open()
    await bg
    await _yield_loop(5)

    assert op.state == ValveState.OPEN_VERIFIED
    set_value_calls = [c for c in hass.services.async_call.call_args_list if c.args[:2] == ("number", "set_value")]
    assert len(set_value_calls) == 1


async def test_hw_max_duration_with_minute_multiplier(hass):
    """Multiplier is applied: 120s * (1/60) = 2.0 minutes written to entity."""
    op = ValveOperator(
        hass=hass,
        switch_entity_id="switch.valve",
        zone_name="hwzone3",
        fsm_config=_fast_fsm_config(False),
        max_retries=0,
        backoff_s=(0.0,),
        max_open_duration_s=120.0,
        hw_max_duration_entity="number.hw_timer_min",
        hw_max_duration_multiplier=1.0 / 60.0,
    )

    async def open_sim():
        await _yield_loop()
        await op._handle_switch_state(_state_event("on"))

    bg = asyncio.create_task(open_sim())
    await op.open()
    await bg
    await _yield_loop(5)

    set_value_calls = [c for c in hass.services.async_call.call_args_list if c.args[:2] == ("number", "set_value")]
    assert len(set_value_calls) == 1
    assert set_value_calls[0].args[2]["value"] == pytest.approx(2.0, rel=1e-3)


async def test_hw_max_duration_mqtt_fallback_when_no_entity(hass):
    """When no entity is configured but a topic is, mqtt.publish is called."""
    op = ValveOperator(
        hass=hass,
        switch_entity_id="switch.valve",
        zone_name="mqttzone",
        fsm_config=_fast_fsm_config(False),
        max_retries=0,
        backoff_s=(0.0,),
        max_open_duration_s=60.0,
        hw_max_duration_entity=None,
        hw_max_duration_topic="zigbee2mqtt/valve/set",
        hw_max_duration_payload_template='{{"irrigation_duration": {value}}}',
    )

    async def open_sim():
        await _yield_loop()
        await op._handle_switch_state(_state_event("on"))

    bg = asyncio.create_task(open_sim())
    await op.open()
    await bg
    await _yield_loop(5)

    mqtt_calls = [c for c in hass.services.async_call.call_args_list if c.args[:2] == ("mqtt", "publish")]
    assert len(mqtt_calls) == 1
    assert mqtt_calls[0].args[2]["topic"] == "zigbee2mqtt/valve/set"
    assert mqtt_calls[0].args[2]["payload"] == '{"irrigation_duration": 60.0}'


async def test_hw_max_duration_entity_tried_before_mqtt(hass):
    """Entity path is tried first; MQTT is only used if entity call fails."""
    original_call = hass.services.async_call
    entity_attempted = []
    mqtt_calls = []

    async def tracking_call(*args, **kwargs):
        if args[:2] == ("number", "set_value"):
            entity_attempted.append(True)
            raise RuntimeError("entity unavailable")
        if args[:2] == ("mqtt", "publish"):
            mqtt_calls.append(args[2])
        return await original_call(*args, **kwargs)

    hass.services.async_call = tracking_call

    op = ValveOperator(
        hass=hass,
        switch_entity_id="switch.valve",
        zone_name="fallbackzone",
        fsm_config=_fast_fsm_config(False),
        max_retries=0,
        backoff_s=(0.0,),
        max_open_duration_s=30.0,
        hw_max_duration_entity="number.hw_timer",
        hw_max_duration_topic="mqtt/valve/set",
        hw_max_duration_payload_template="{value}",
    )

    async def open_sim():
        await _yield_loop()
        await op._handle_switch_state(_state_event("on"))

    bg = asyncio.create_task(open_sim())
    await op.open()
    await bg
    await _yield_loop(5)

    assert entity_attempted, "entity path was never tried"
    assert len(mqtt_calls) == 1
    assert mqtt_calls[0]["payload"] == "30.0"


async def test_hw_max_duration_exception_is_swallowed(hass, caplog):
    """A failing number.set_value call is logged as WARNING but never raises."""
    original_call = hass.services.async_call

    async def side_effect(*args, **kwargs):
        if args[:2] == ("number", "set_value"):
            raise RuntimeError("entity not found")
        return await original_call(*args, **kwargs)

    hass.services.async_call = side_effect

    op = _make_operator_with_hw_interlock(hass, max_open_duration_s=60.0)

    async def open_sim():
        await _yield_loop()
        await op._handle_switch_state(_state_event("on"))

    bg = asyncio.create_task(open_sim())
    result = await op.open()
    await bg
    await _yield_loop(5)

    assert result.status == OperationStatus.OK
    assert "failed to set hardware max_duration" in caplog.text


async def test_hw_max_duration_idempotency_guard_direct(hass):
    """Calling _set_hw_max_duration twice only runs the entity call once (line 546)."""
    op = _make_operator_with_hw_interlock(hass, max_open_duration_s=60.0)

    async def open_sim():
        await _yield_loop()
        await op._handle_switch_state(_state_event("on"))

    bg = asyncio.create_task(open_sim())
    await op.open()
    await bg
    await _yield_loop(5)

    hass.services.async_call.reset_mock()
    # Call directly a second time — must hit the _hw_duration_set guard (line 546)
    await op._set_hw_max_duration()

    set_value_calls = [c for c in hass.services.async_call.call_args_list if c.args[:2] == ("number", "set_value")]
    assert len(set_value_calls) == 0


async def test_hw_max_duration_mqtt_exception_is_logged(hass, caplog):
    """MQTT publish failure is logged as WARNING but does not raise (lines 593-594)."""
    original_call = hass.services.async_call
    mqtt_calls = []

    async def side_effect(*args, **kwargs):
        if args[:2] == ("mqtt", "publish"):
            mqtt_calls.append(True)
            raise RuntimeError("mqtt broker unavailable")
        return await original_call(*args, **kwargs)

    hass.services.async_call = side_effect

    op = ValveOperator(
        hass=hass,
        switch_entity_id="switch.valve",
        zone_name="mqttfailzone",
        fsm_config=_fast_fsm_config(False),
        max_retries=0,
        backoff_s=(0.0,),
        max_open_duration_s=30.0,
        hw_max_duration_entity=None,
        hw_max_duration_topic="mqtt/fail/set",
        hw_max_duration_payload_template="{value}",
    )

    async def open_sim():
        await _yield_loop()
        await op._handle_switch_state(_state_event("on"))

    bg = asyncio.create_task(open_sim())
    result = await op.open()
    await bg
    await _yield_loop(5)

    assert result.status == OperationStatus.OK
    assert mqtt_calls, "MQTT publish was never attempted"
    assert "failed to set hardware max_duration via MQTT" in caplog.text


# ── Retry cap and transient-notification suppression (AI-157) ─────────


async def test_default_retry_budget_and_derived_maintenance_threshold(hass):
    """Defaults: 5 retries, doubling backoff, MAINTENANCE at 1 + max_retries.

    The derived threshold guarantees one command's retry budget can never
    trip MAINTENANCE mid-command: a fully-failed command reaches it exactly
    at its definitive failure.
    """
    op = ValveOperator(hass=hass, switch_entity_id="switch.valve")
    assert op._max_retries == 5
    assert op.DEFAULT_BACKOFF_S == (1.0, 2.0, 4.0, 8.0, 16.0)
    assert op._fsm_config.max_consecutive_failures == 6


async def test_transient_notification_only_after_retry_budget_exhausted(hass):
    """OPEN_FAILED on every attempt: intermediate failures stay silent,
    the notifier fires exactly once when the command is definitively failed."""
    from never_dry.valve_fsm import FsmConfig
    from never_dry.valve_notifier import NotificationKind

    notifier = MagicMock()
    notifier.notify = AsyncMock()
    op = ValveOperator(
        hass=hass,
        switch_entity_id="switch.valve",
        zone_name="capzone",
        # High maintenance threshold: keep the test on the FAILED path.
        fsm_config=FsmConfig(
            has_flow_meter=False,
            open_timeout_s=0.05,
            close_timeout_s=0.05,
            flow_verify_timeout_s=0.05,
            leak_timeout_s=0.05,
            max_consecutive_failures=10,
        ),
        max_retries=2,
        backoff_s=(0.0,),
        notifier=notifier,
    )

    result = await op.open()

    assert result.status == OperationStatus.FAILED
    assert result.retries_used == 2
    assert notifier.notify.await_count == 1
    kind = notifier.notify.await_args.args[1]
    assert kind == NotificationKind.COMMAND_FAILED


async def test_fully_failed_command_enters_maintenance_at_budget_end(hass):
    """With the derived threshold (1 + max_retries) a command that fails
    every attempt lands in MAINTENANCE exactly at its definitive failure."""
    from never_dry.valve_fsm import FsmConfig

    op = ValveOperator(
        hass=hass,
        switch_entity_id="switch.valve",
        zone_name="maintzone",
        fsm_config=FsmConfig(
            has_flow_meter=False,
            open_timeout_s=0.05,
            close_timeout_s=0.05,
            flow_verify_timeout_s=0.05,
            leak_timeout_s=0.05,
            max_consecutive_failures=3,  # = 1 + max_retries below
        ),
        max_retries=2,
        backoff_s=(0.0,),
    )

    result = await op.open()

    assert result.status == OperationStatus.MAINTENANCE
    assert op.is_in_maintenance
