"""Per-valve finite state machine.

Pure Python, no Home Assistant dependency. Event-driven: all time-based
behavior enters as ``TIMEOUT_*`` events dispatched by the host. The host
(see ``valve_operator.py``, AI-029) is responsible for executing the
actions returned by :meth:`ValveFsm.dispatch` — sending switch commands,
starting/cancelling timers, emitting notifications.

When ``has_flow_meter`` is ``False`` the ``OPEN_VERIFIED`` and ``CLOSED``
states are skipped: the FSM goes directly REQ_OPEN → OPEN → REQ_CLOSE →
IDLE because there is no actuation evidence to verify and no leak window
to honour.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar

# ── States, events, failure kinds ─────────────────────────────────────


class ValveState(StrEnum):
    """Per-valve states tracked by :class:`ValveFsm`."""

    IDLE = "idle"
    REQ_OPEN = "req_open"
    OPEN = "open"
    OPEN_VERIFIED = "open_verified"
    REQ_CLOSE = "req_close"
    CLOSED = "closed"
    UNREACHABLE = "unreachable"
    MAINTENANCE = "maintenance"


class ValveEvent(StrEnum):
    """Input events accepted by :meth:`ValveFsm.dispatch`."""

    CMD_OPEN = "cmd_open"
    CMD_CLOSE = "cmd_close"
    CMD_RESET = "cmd_reset"
    OBS_SWITCH_ON = "obs_switch_on"
    OBS_SWITCH_OFF = "obs_switch_off"
    OBS_FLOW_POSITIVE = "obs_flow_positive"
    OBS_FLOW_ZERO = "obs_flow_zero"
    OBS_UNAVAILABLE = "obs_unavailable"
    OBS_AVAILABLE = "obs_available"
    TIMEOUT_OPEN = "timeout_open"
    TIMEOUT_CLOSE = "timeout_close"
    TIMEOUT_FLOW = "timeout_flow"
    #: The meter cannot testify in time on this installation, so the
    #: verification is declared inapplicable rather than failed.
    FLOW_UNVERIFIABLE = "flow_unverifiable"
    TIMEOUT_LEAK = "timeout_leak"


class FailureKind(StrEnum):
    """Failure modes recognised by the FSM, surfaced via :class:`NotifyFailure`."""

    OPEN_FAILED = "open_failed"
    ACTUATION_FAILED = "actuation_failed"
    CLOSE_VERIFICATION_FAILED = "close_verification_failed"
    CLOSE_LEAK = "close_leak"


class TimerName(StrEnum):
    """Logical names of the timers the host must manage on the FSM's behalf."""

    OPEN = "open"
    CLOSE = "close"
    FLOW = "flow"
    LEAK = "leak"


# ── Actions (pure data, the host executes them) ───────────────────────


@dataclass(frozen=True)
class SendSwitchOn:
    """Action: the host should issue a ``switch.turn_on`` command."""


@dataclass(frozen=True)
class SendSwitchOff:
    """Action: the host should issue a ``switch.turn_off`` command."""


@dataclass(frozen=True)
class StartTimer:
    """Action: the host should start (or restart) a named timer.

    When the timer fires the host must dispatch the matching
    ``TIMEOUT_*`` event back into the FSM.
    """

    name: TimerName
    seconds: float


@dataclass(frozen=True)
class CancelTimer:
    """Action: the host should cancel the named timer if it is running."""

    name: TimerName


@dataclass(frozen=True)
class CancelAllTimers:
    """Action: the host should cancel every active timer for this valve."""


@dataclass(frozen=True)
class NotifyFailure:
    """Action: the host should surface a failure of the given kind."""

    kind: FailureKind


@dataclass(frozen=True)
class EnterMaintenance:
    """Action: marker emitted when the FSM crosses into ``MAINTENANCE``."""


FsmAction = SendSwitchOn | SendSwitchOff | StartTimer | CancelTimer | CancelAllTimers | NotifyFailure | EnterMaintenance


# ── Config & transition result ────────────────────────────────────────


@dataclass(frozen=True)
class FsmConfig:
    """Configuration knobs for a single :class:`ValveFsm` instance.

    ``has_flow_meter=False`` collapses the verification states: with no
    flow sensor we have no L3 evidence to confirm actuation or close.
    """

    has_flow_meter: bool
    open_timeout_s: float = 10.0
    close_timeout_s: float = 10.0
    flow_verify_timeout_s: float = 10.0
    leak_timeout_s: float = 10.0
    max_consecutive_failures: int = 3


@dataclass(frozen=True)
class TransitionResult:
    """Outcome of a single :meth:`ValveFsm.dispatch` call.

    ``actions`` is the list of side-effects the host must execute, in
    order. ``failure`` is set only on transitions that record a failure;
    ``failure_count`` is the current counter value *after* the transition.
    """

    from_state: ValveState
    to_state: ValveState
    event: ValveEvent
    actions: tuple[FsmAction, ...] = ()
    failure: FailureKind | None = None
    failure_count: int = 0


# ── The FSM ───────────────────────────────────────────────────────────


class ValveFsm:
    """Per-valve finite state machine.

    The FSM never reads a clock and never schedules anything. All
    time-driven transitions enter via ``TIMEOUT_*`` events from the host.
    """

    def __init__(self, config: FsmConfig):
        """Build a fresh FSM in :attr:`ValveState.IDLE` with a zero counter."""
        self._config = config
        self._state: ValveState = ValveState.IDLE
        self._failure_count: int = 0
        self._last_failure: FailureKind | None = None

    # ── Properties ───────────────────────────────────────────────────

    @property
    def state(self) -> ValveState:
        """Return the current state."""
        return self._state

    @property
    def failure_count(self) -> int:
        """Return the number of consecutive failures since the last clean cycle."""
        return self._failure_count

    @property
    def last_failure(self) -> FailureKind | None:
        """Return the kind of the most recent failure, or ``None`` if none."""
        return self._last_failure

    # ── Dispatch ─────────────────────────────────────────────────────

    def dispatch(self, event: ValveEvent) -> TransitionResult:
        """Process one event and return the resulting transition.

        Maintenance swallows every command except ``CMD_RESET``;
        ``OBS_UNAVAILABLE`` wins from any active state and routes the
        FSM to :attr:`ValveState.UNREACHABLE`. All other events are
        forwarded to the per-state handler.
        """
        if self._state == ValveState.MAINTENANCE:
            return self._handle_maintenance(event)

        if event == ValveEvent.OBS_UNAVAILABLE:
            if self._state == ValveState.UNREACHABLE:
                return self._noop(event)
            return self._transition(
                ValveState.UNREACHABLE,
                event,
                actions=(CancelAllTimers(),),
            )

        if self._state == ValveState.UNREACHABLE:
            if event == ValveEvent.OBS_AVAILABLE:
                return self._transition(ValveState.IDLE, event)
            return self._noop(event)

        handler = self._HANDLERS.get(self._state)
        if handler is None:  # pragma: no cover — defensive; every active state has a handler
            return self._noop(event)
        return handler(self, event)

    # ── Per-state handlers ───────────────────────────────────────────

    def _on_idle(self, event: ValveEvent) -> TransitionResult:
        """Handle events in :attr:`ValveState.IDLE` (only ``CMD_OPEN`` does anything)."""
        if event == ValveEvent.CMD_OPEN:
            return self._transition(
                ValveState.REQ_OPEN,
                event,
                actions=(
                    SendSwitchOn(),
                    StartTimer(TimerName.OPEN, self._config.open_timeout_s),
                ),
            )
        return self._noop(event)

    def _on_req_open(self, event: ValveEvent) -> TransitionResult:
        """Handle events while waiting for ``switch.turn_on`` to be confirmed."""
        if event == ValveEvent.OBS_SWITCH_ON:
            if self._config.has_flow_meter:
                return self._transition(
                    ValveState.OPEN,
                    event,
                    actions=(
                        CancelTimer(TimerName.OPEN),
                        StartTimer(TimerName.FLOW, self._config.flow_verify_timeout_s),
                    ),
                )
            # No flow meter: OPEN is the steady on-state.
            return self._transition(
                ValveState.OPEN,
                event,
                actions=(CancelTimer(TimerName.OPEN),),
            )
        if event == ValveEvent.TIMEOUT_OPEN:
            return self._fail(event, FailureKind.OPEN_FAILED)
        if event == ValveEvent.CMD_CLOSE:
            # Emergency cancel of a pending open.
            return self._transition(
                ValveState.REQ_CLOSE,
                event,
                actions=(
                    CancelTimer(TimerName.OPEN),
                    SendSwitchOff(),
                    StartTimer(TimerName.CLOSE, self._config.close_timeout_s),
                ),
            )
        return self._noop(event)

    def _on_open(self, event: ValveEvent) -> TransitionResult:
        """Handle events in :attr:`ValveState.OPEN`.

        With a flow meter this is the L3 verification window: we wait
        for flow or for the flow timeout. Without one, OPEN is the
        steady-state and only ``CMD_CLOSE`` is meaningful.
        """
        if self._config.has_flow_meter:
            if event == ValveEvent.OBS_FLOW_POSITIVE:
                return self._transition(
                    ValveState.OPEN_VERIFIED,
                    event,
                    actions=(CancelTimer(TimerName.FLOW),),
                )
            if event == ValveEvent.TIMEOUT_FLOW:
                return self._fail(
                    event,
                    FailureKind.ACTUATION_FAILED,
                    extra_actions=(SendSwitchOff(),),
                )
            if event == ValveEvent.FLOW_UNVERIFIABLE:
                # A still meter is not evidence of a dry pipe. When the counter's
                # resolution over this zone's flow means the first increment
                # cannot arrive inside any usable window, the honest verdict is
                # "not verifiable here" — the run proceeds and flow stays an
                # observer. Closing a healthy valve because a coarse counter had
                # nothing to say yet is the failure this avoids (GH #173).
                return self._transition(
                    ValveState.OPEN_VERIFIED,
                    event,
                    actions=(CancelTimer(TimerName.FLOW),),
                )
            if event == ValveEvent.CMD_CLOSE:
                return self._transition(
                    ValveState.REQ_CLOSE,
                    event,
                    actions=(
                        CancelTimer(TimerName.FLOW),
                        SendSwitchOff(),
                        StartTimer(TimerName.CLOSE, self._config.close_timeout_s),
                    ),
                )
            if event == ValveEvent.OBS_SWITCH_OFF:
                return self._external_close(event)
            return self._noop(event)

        # No flow meter: OPEN is the steady state, only CMD_CLOSE moves.
        if event == ValveEvent.CMD_CLOSE:
            return self._transition(
                ValveState.REQ_CLOSE,
                event,
                actions=(
                    SendSwitchOff(),
                    StartTimer(TimerName.CLOSE, self._config.close_timeout_s),
                ),
            )
        if event == ValveEvent.OBS_SWITCH_OFF:
            return self._external_close(event)
        return self._noop(event)

    def _on_open_verified(self, event: ValveEvent) -> TransitionResult:
        """Handle events in :attr:`ValveState.OPEN_VERIFIED` (flow meter only)."""
        if event == ValveEvent.CMD_CLOSE:
            return self._transition(
                ValveState.REQ_CLOSE,
                event,
                actions=(
                    SendSwitchOff(),
                    StartTimer(TimerName.CLOSE, self._config.close_timeout_s),
                ),
            )
        if event == ValveEvent.OBS_SWITCH_OFF:
            return self._external_close(event)
        return self._noop(event)

    def _on_req_close(self, event: ValveEvent) -> TransitionResult:
        """Handle events while waiting for ``switch.turn_off`` to be confirmed."""
        if event == ValveEvent.OBS_SWITCH_OFF:
            if self._config.has_flow_meter:
                return self._transition(
                    ValveState.CLOSED,
                    event,
                    actions=(
                        CancelTimer(TimerName.CLOSE),
                        StartTimer(TimerName.LEAK, self._config.leak_timeout_s),
                    ),
                )
            # No flow meter: close confirmed, cycle is clean.
            self._failure_count = 0
            self._last_failure = None
            return self._transition(
                ValveState.IDLE,
                event,
                actions=(CancelTimer(TimerName.CLOSE),),
            )
        if event == ValveEvent.TIMEOUT_CLOSE:
            return self._fail(event, FailureKind.CLOSE_VERIFICATION_FAILED)
        return self._noop(event)

    def _on_closed(self, event: ValveEvent) -> TransitionResult:
        """Handle events in :attr:`ValveState.CLOSED`, the post-close leak window."""
        if event == ValveEvent.OBS_FLOW_ZERO:
            self._failure_count = 0
            self._last_failure = None
            return self._transition(
                ValveState.IDLE,
                event,
                actions=(CancelTimer(TimerName.LEAK),),
            )
        if event == ValveEvent.TIMEOUT_LEAK:
            return self._fail(event, FailureKind.CLOSE_LEAK)
        return self._noop(event)

    def _external_close(self, event: ValveEvent) -> TransitionResult:
        """Valve observed OFF while open (e.g. hardware/smart-valve auto-close).

        The switch went off without us commanding it. Treat the cycle as
        cleanly closed: cancel every timer, clear the failure counter and
        return to IDLE so the FSM reflects reality. This prevents a later
        commanded ``CMD_CLOSE`` from waiting on an off-transition that will
        never arrive (the switch is already off) and timing out as a
        spurious ``CLOSE_VERIFICATION_FAILED``.
        """
        self._failure_count = 0
        self._last_failure = None
        return self._transition(
            ValveState.IDLE,
            event,
            actions=(CancelAllTimers(),),
        )

    def _handle_maintenance(self, event: ValveEvent) -> TransitionResult:
        """Handle events while the FSM is locked in maintenance."""
        if event == ValveEvent.CMD_RESET:
            self._failure_count = 0
            self._last_failure = None
            return self._transition(ValveState.IDLE, event)
        return self._noop(event)

    _HANDLERS: ClassVar[dict] = {
        ValveState.IDLE: _on_idle,
        ValveState.REQ_OPEN: _on_req_open,
        ValveState.OPEN: _on_open,
        ValveState.OPEN_VERIFIED: _on_open_verified,
        ValveState.REQ_CLOSE: _on_req_close,
        ValveState.CLOSED: _on_closed,
    }

    # ── Internal helpers ─────────────────────────────────────────────

    def _transition(
        self,
        to_state: ValveState,
        event: ValveEvent,
        actions: tuple[FsmAction, ...] = (),
        failure: FailureKind | None = None,
    ) -> TransitionResult:
        """Move to ``to_state`` and build a :class:`TransitionResult`."""
        from_state = self._state
        self._state = to_state
        return TransitionResult(
            from_state=from_state,
            to_state=to_state,
            event=event,
            actions=actions,
            failure=failure,
            failure_count=self._failure_count,
        )

    def _noop(self, event: ValveEvent) -> TransitionResult:
        """Return a result that leaves the state unchanged and emits no actions."""
        return TransitionResult(
            from_state=self._state,
            to_state=self._state,
            event=event,
            failure_count=self._failure_count,
        )

    def _fail(
        self,
        event: ValveEvent,
        kind: FailureKind,
        extra_actions: tuple[FsmAction, ...] = (),
    ) -> TransitionResult:
        """Record a failure of ``kind`` and route to IDLE (or MAINTENANCE if threshold hit)."""
        self._failure_count += 1
        self._last_failure = kind
        hit_threshold = self._failure_count >= self._config.max_consecutive_failures
        actions: tuple[FsmAction, ...] = (
            *extra_actions,
            NotifyFailure(kind),
        )
        if hit_threshold:
            actions = (*actions, EnterMaintenance())
        target = ValveState.MAINTENANCE if hit_threshold else ValveState.IDLE
        return self._transition(target, event, actions=actions, failure=kind)
