"""Driver abstraction — the single home for driving one irrigation actuator.

This module materializes the domain model's ``Driver`` (see
``docs/design_domain_object_model.md``) as a concrete, Home-Assistant-aware
base class — the *attuatore* — together with its two specializations:

* :class:`ZoneDriver` (the model's ``ZoneDriver``) — drives one zone's
  valve/switch, translating a **liters** request into actuation and returning a
  truthful :class:`DeliveryResult`.
* :class:`MasterDriver` (the model's ``MasterDriver``) — drives shared
  hydraulics (a master valve or pump), *following* aggregate zone activity with
  an off-delay linger. It has no notion of liters.

The abstract base :class:`Driver` owns everything the two share, as the domain
model prescribes: the entity adapter (``switch.*`` vs ``valve.*``), confirmed
on/off commands over a pure :class:`~.valve_fsm.ValveFsm`, adaptive
latency/timeout, the safety layers (absolute watchdog, hardware max-duration
timer, ``CLOSE_LEAK`` recovery, stuck-open escalation), an active liveness
probe, and the user-facing notifier seam.

Design intent — this module is deliberately **self-contained**: it consolidates
behaviour today scattered across ``valve_operator.py`` (the FSM host + safety),
the controller ``_deliver_*`` loops (the three delivery strategies) and the
still-missing master-pump logic (GH #95), so a later phase can *replace* those
call sites with this one class hierarchy. It reuses the existing pure/host
building blocks (``valve_fsm``, ``valve_latency``, ``valve_notifier``,
``flow_utils``) by import rather than duplicating them.

References: GH #74 (actuator abstraction), GH #94 (``valve.*`` support),
GH #95 (master valve/pump), ``docs/design/actuator-abstraction.md``,
``docs/design/valve-state-machine.md``, ``docs/design_domain_model_anomalies.md``
(closes the *Driver ``<<abstract>>`` never declared* anomaly §C1 and gives
``volume_preset`` a home behind the uniform ``deliver()`` seam, §A2/§D1).
"""

from __future__ import annotations

import abc
import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import timedelta
from enum import StrEnum
from time import monotonic
from typing import ClassVar

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)

from . import flow_utils
from .const import (
    DEFAULT_DELIVERY_TIMEOUT_S,
    DELIVERY_MODE_ESTIMATED_FLOW,
    DELIVERY_MODE_FLOW_METER,
    DELIVERY_MODE_VOLUME_PRESET,
    FLOW_METER_POLL_INTERVAL_S,
)
from .valve_fsm import (
    CancelAllTimers,
    CancelTimer,
    EnterMaintenance,
    FailureKind,
    FsmAction,
    FsmConfig,
    NotifyFailure,
    SendSwitchOff,
    SendSwitchOn,
    StartTimer,
    TimerName,
    TransitionResult,
    ValveEvent,
    ValveFsm,
    ValveState,
)
from .valve_latency import MIN_SAMPLES, ValveLatencyTracker
from .valve_notifier import NotificationKind, Severity, ValveNotifier

_LOGGER = logging.getLogger(__name__)

# ``volume_preset``: how long to wait for a smart valve to auto-open on a
# ``number.set_value`` dose before we send an explicit open (idempotent).
AUTO_OPEN_GRACE_S: float = 3.0
# Default period of the active liveness probe (a Zigbee valve that drops off the
# mesh often keeps showing a stale ``off``; passive state is not enough).
DEFAULT_LIVENESS_INTERVAL_MIN: float = 30.0
# Default linger before a master valve/pump follows the zones back to OFF.
DEFAULT_MASTER_OFF_DELAY_S: float = 10.0


# ── Entity adapter: switch.* vs valve.* (the shared floor, GH #94) ─────────


class EntityDomain(StrEnum):
    """The two actuator entity domains NeverDry can drive today."""

    SWITCH = "switch"
    VALVE = "valve"


@dataclass(frozen=True)
class ValveCommandAdapter:
    """Map an ``entity_id`` to its command services and normalize its state.

    This is the *floor* of the actuator abstraction (``actuator-abstraction.md``
    §2): the one place that knows a ``switch.*`` opens with ``switch.turn_on``
    and reports ``on``/``off``, while a ``valve.*`` opens with
    ``valve.open_valve`` and reports ``open``/``closed``/``opening``/``closing``.
    The domain is derived from the ``entity_id`` prefix at runtime — additive,
    zero config migration, existing ``switch.*`` setups keep working unchanged.

    Note: a **pump** is not a distinct HA domain — HA has no ``pump.*`` entity or
    ``pump`` device_class. A pump is just a ``switch.*`` entity to HA (a relay
    that turns it on/off), so it falls through the ``switch`` branch here with no
    dedicated handling.
    """

    entity_id: str

    @property
    def domain(self) -> EntityDomain:
        """Return the actuator domain, defaulting to ``switch`` for anything else."""
        prefix = self.entity_id.split(".", 1)[0] if "." in self.entity_id else ""
        return EntityDomain.VALVE if prefix == EntityDomain.VALVE.value else EntityDomain.SWITCH

    def command(self, *, on: bool) -> tuple[str, str]:
        """Return the ``(domain, service)`` pair that opens (``on``) or closes."""
        if self.domain == EntityDomain.VALVE:
            return ("valve", "open_valve" if on else "close_valve")
        return ("switch", "turn_on" if on else "turn_off")

    @staticmethod
    def interpret_state(raw: str | None) -> str:
        """Collapse a raw HA state to ``on`` / ``off`` / ``transitional`` / ``unavailable``.

        ``opening``/``closing`` are *transitional*: the valve is moving and no
        FSM observation should be emitted until it settles.
        """
        if raw is None or raw in ("unavailable", "unknown"):
            return "unavailable" if raw != "unknown" else "unknown"
        if raw in ("on", "open"):
            return "on"
        if raw in ("off", "closed"):
            return "off"
        if raw in ("opening", "closing"):
            return "transitional"
        return "unknown"


# ── Delivery contract: DeliveryResult + qualifiers (domain model) ──────────


class DeliveryQuality(StrEnum):
    """How truthful the delivered-liters figure is (GH #74 review, fpytloun)."""

    MEASURED = "measured"
    ESTIMATED = "estimated"
    PARTIAL = "partial"
    DELAYED = "delayed"
    LOW_CONFIDENCE = "low_confidence"
    # The user hand-watered and pressed "Mark irrigated": the amount is assumed
    # (the recommended liters), declared by a human rather than measured. Used by
    # :class:`ManualActuator` for valve-less, hand-watered plants.
    DECLARED = "declared"


class DeliveryMode(StrEnum):
    """The three delivery strategies, mirroring the ``const`` string values.

    Materializing the mode as an enum turns the controller's ``if mode == ...``
    string dispatch (anomaly §D1) into real polymorphism at the ``deliver()``
    seam.
    """

    ESTIMATED_FLOW = DELIVERY_MODE_ESTIMATED_FLOW
    FLOW_METER = DELIVERY_MODE_FLOW_METER
    VOLUME_PRESET = DELIVERY_MODE_VOLUME_PRESET


@dataclass(frozen=True)
class DeliveryResult:
    """The round-trip return of :meth:`ZoneDriver.deliver`.

    Not a bare float: it carries how much water was delivered *and how truthful*
    that figure is, so the Zone can settle its deficit and diagnostics can show
    the user *how* the number was obtained. A backend that measures late can
    :meth:`revise` an ``estimated``/``delayed`` result once the real figure lands.
    """

    liters_delivered: float
    quality: DeliveryQuality
    elapsed_s: float
    requested_liters: float
    detail: str | None = None

    def revise(self, measured_liters: float, quality: DeliveryQuality = DeliveryQuality.MEASURED) -> DeliveryResult:
        """Return a copy corrected with a later measured figure (see the model's ``revise``)."""
        return replace(self, liters_delivered=measured_liters, quality=quality)


# ── Command outcome types (carried over from valve_operator) ───────────────


class OperationStatus(StrEnum):
    """Outcome category for :meth:`Driver.async_turn_on` / :meth:`async_turn_off`."""

    OK = "ok"
    FAILED = "failed"
    MAINTENANCE = "maintenance"
    PRECHECK_FAILED = "precheck_failed"


@dataclass(frozen=True)
class OperationResult:
    """Typed return of a confirmed on/off cycle.

    ``error_detail`` carries the :class:`~.valve_fsm.FailureKind` name on FAILED,
    the precheck reason on PRECHECK_FAILED, or ``"in_maintenance"`` when locked.
    """

    status: OperationStatus
    error_detail: str | None = None
    retries_used: int = 0
    duration_ms: float = 0.0


# ── Internal mappings (carried over from valve_operator) ───────────────────


_TIMEOUT_EVENT_FOR_TIMER: dict[TimerName, ValveEvent] = {
    TimerName.OPEN: ValveEvent.TIMEOUT_OPEN,
    TimerName.CLOSE: ValveEvent.TIMEOUT_CLOSE,
    TimerName.FLOW: ValveEvent.TIMEOUT_FLOW,
    TimerName.LEAK: ValveEvent.TIMEOUT_LEAK,
}

_TRANSIENT_FAILURES: frozenset[FailureKind] = frozenset(
    {FailureKind.OPEN_FAILED, FailureKind.CLOSE_VERIFICATION_FAILED}
)

_OPEN_STATES: frozenset[ValveState] = frozenset({ValveState.OPEN, ValveState.OPEN_VERIFIED})

_OPERATION_FOR_FAILURE: dict[FailureKind, str] = {
    FailureKind.OPEN_FAILED: "open",
    FailureKind.ACTUATION_FAILED: "open",
    FailureKind.CLOSE_VERIFICATION_FAILED: "close",
    FailureKind.CLOSE_LEAK: "close",
}

# For each command, the normalized entity level that already satisfies it and the
# observation that confirms it. Used to confirm by *level* instead of waiting for
# a state-change *edge* that a switch already in that state will never emit.
_CONFIRM_FOR_CMD: dict[ValveEvent, tuple[str, ValveEvent]] = {
    ValveEvent.CMD_OPEN: ("on", ValveEvent.OBS_SWITCH_ON),
    ValveEvent.CMD_CLOSE: ("off", ValveEvent.OBS_SWITCH_OFF),
}


# ── The abstract driver (the model's ``Driver``) ───────────────────────────


class Driver(abc.ABC):
    """HA-aware driver for a single actuator, sitting on a pure :class:`ValveFsm`.

    Owns the entity adapter, confirmed on/off commands with retry on transient
    (comms) failures, the adaptive latency timeout, the absolute watchdog, the
    optional hardware max-duration timer, ``CLOSE_LEAK`` recovery + stuck-open
    escalation, and an active liveness probe. Subclasses add *what* to do with a
    live actuator: :class:`ZoneDriver` delivers liters; :class:`MasterDriver`
    follows aggregate activity.
    """

    DEFAULT_BACKOFF_S: ClassVar[tuple[float, ...]] = (1.0, 2.0, 4.0, 8.0, 16.0)

    def __init__(
        self,
        hass: HomeAssistant,
        entity_id: str,
        *,
        flow_sensor_entity_id: str | None = None,
        name: str = "",
        fsm_config: FsmConfig | None = None,
        max_retries: int = 5,
        backoff_s: tuple[float, ...] | None = None,
        flow_zero_threshold: float = 0.05,
        notifier: ValveNotifier | None = None,
        max_open_duration_s: float | Callable[[], float] = 3600.0,
        hw_max_duration_entity: str | None = None,
        hw_max_duration_multiplier: float = 1.0,
        hw_max_duration_topic: str | None = None,
        hw_max_duration_payload_template: str = "{value}",
        availability_entity: str | None = None,
    ) -> None:
        """Wire the actuator to HA, subscribe to state changes and build the FSM."""
        self._hass = hass
        self._entity_id = entity_id
        self._adapter = ValveCommandAdapter(entity_id)
        self._flow_sensor_entity_id = flow_sensor_entity_id
        self._name = name or entity_id
        # One command owns 1 + max_retries attempts, so the retry budget can
        # never trip MAINTENANCE mid-command — a fully-failed command reaches
        # it exactly at its definitive failure.
        self._fsm_config = fsm_config or FsmConfig(
            has_flow_meter=flow_sensor_entity_id is not None,
            max_consecutive_failures=max_retries + 1,
        )
        self._fsm = ValveFsm(self._fsm_config)
        self._max_retries = max_retries
        self._backoff_s = backoff_s if backoff_s is not None else self.DEFAULT_BACKOFF_S
        self._flow_zero_threshold = flow_zero_threshold
        self._notifier = notifier
        # Static float or zero-arg callable re-evaluated at every open, so the
        # watchdog and hardware timer track the current deficit, not a snapshot.
        self._max_open_duration_s = max_open_duration_s
        self._hw_max_duration_entity = hw_max_duration_entity
        self._hw_max_duration_multiplier = hw_max_duration_multiplier
        self._hw_max_duration_topic = hw_max_duration_topic
        self._hw_max_duration_payload_template = hw_max_duration_payload_template
        self._availability_entity = availability_entity

        self._lock = asyncio.Lock()
        self._timers: dict[TimerName, asyncio.Task] = {}
        self._completion: asyncio.Future[OperationResult] | None = None
        self._expected_terminal: tuple[ValveState, ...] = ()
        self._leak_recovery_attempted: bool = False
        self._retries_left: int = 0
        self._watchdog_task: asyncio.Task | None = None
        self._hw_duration_set: bool = False

        self._latency = ValveLatencyTracker(hass, entity_id)
        self._cmd_start_time: float | None = None
        hass.async_create_task(self._latency.async_load())

        self._unsub_switch = async_track_state_change_event(hass, [entity_id], self._on_switch_state)
        self._unsub_flow = None
        if flow_sensor_entity_id:
            self._unsub_flow = async_track_state_change_event(hass, [flow_sensor_entity_id], self._on_flow_state)
        self._unsub_liveness = None

    # ── Identity ────────────────────────────────────────────────────────

    @property
    @abc.abstractmethod
    def role(self) -> str:
        """Return the actuator role (``"zone"`` / ``"master"``) — makes the base abstract."""

    @property
    def name(self) -> str:
        """Human-facing name for logs and notifications."""
        return self._name

    @property
    def entity_id(self) -> str:
        """The driven actuator entity id."""
        return self._entity_id

    # ── State properties ─────────────────────────────────────────────────

    @property
    def state(self) -> ValveState:
        """Return the underlying FSM state."""
        return self._fsm.state

    @property
    def is_open(self) -> bool:
        """``True`` while the FSM believes the actuator is open."""
        return self._fsm.state in _OPEN_STATES

    @property
    def is_in_maintenance(self) -> bool:
        """``True`` when the actuator is locked in MAINTENANCE."""
        return self._fsm.state == ValveState.MAINTENANCE

    @property
    def failure_count(self) -> int:
        """Return the FSM's consecutive-failure counter."""
        return self._fsm.failure_count

    @property
    def latency_diagnostics(self) -> dict:
        """Return latency statistics for this actuator (open and close windows)."""
        return self._latency.as_dict()

    def _current_max_open_duration(self) -> float:
        """Resolve the max-open duration, evaluating the provider if callable."""
        if callable(self._max_open_duration_s):
            return float(self._max_open_duration_s())
        return float(self._max_open_duration_s)

    # ── Public command API ───────────────────────────────────────────────

    async def async_turn_on(self) -> OperationResult:
        """Open the actuator, awaiting verification.

        Returns once the FSM reaches ``OPEN_VERIFIED`` (with flow meter) or
        ``OPEN`` (without). Retries transient failures with exponential backoff
        up to ``max_retries``; physical failures return immediately.
        """
        terminals: tuple[ValveState, ...] = (
            (ValveState.OPEN_VERIFIED,) if self._fsm_config.has_flow_meter else (ValveState.OPEN,)
        )
        return await self._run_command(cmd=ValveEvent.CMD_OPEN, terminals=terminals)

    async def async_turn_off(self) -> OperationResult:
        """Close the actuator, awaiting verification, with ``CLOSE_LEAK`` recovery.

        On ``CLOSE_LEAK`` (switch off but flow still positive) the last-resort
        recovery runs *before* declaring the close failed:

        1. Re-issue the close directly (bypassing the FSM, in case the first
           command was lost on the wire).
        2. Wait ``leak_timeout_s`` for the flow to drop.
        3. If the flow dropped → report ``OK`` with ``error_detail="leak_recovered"``.
        4. If the flow is still positive → call the integration-wide emergency
           stop, emit a CRITICAL ``STUCK_OPEN`` and return the ``FAILED`` result.

        Recovery is attempted at most **once** per call.
        """
        self._leak_recovery_attempted = False
        result = await self._run_command(cmd=ValveEvent.CMD_CLOSE, terminals=(ValveState.IDLE,))
        if (
            result.status == OperationStatus.FAILED
            and result.error_detail == FailureKind.CLOSE_LEAK.value
            and not self._leak_recovery_attempted
        ):
            self._leak_recovery_attempted = True
            if await self._attempt_leak_recovery():
                return OperationResult(
                    status=OperationStatus.OK,
                    error_detail="leak_recovered",
                    retries_used=result.retries_used,
                    duration_ms=result.duration_ms,
                )
            await self._escalate_stuck_open()
        return result

    async def async_reset_maintenance(self) -> None:
        """Clear ``MAINTENANCE`` and reset the failure counter."""
        await self._dispatch(ValveEvent.CMD_RESET)

    def async_unload(self) -> None:
        """Detach state listeners, stop the liveness probe and cancel every timer."""
        if self._unsub_switch:
            self._unsub_switch()
        if self._unsub_flow:
            self._unsub_flow()
        if self._unsub_liveness:
            self._unsub_liveness()
            self._unsub_liveness = None
        self._cancel_all_timers()
        self._cancel_watchdog()

    # ── Active liveness probe (Driver.ping in the model) ─────────────────

    def start_liveness_probe(self, interval_min: float = DEFAULT_LIVENESS_INTERVAL_MIN) -> None:
        """Begin polling the actuator's reachability every ``interval_min`` minutes.

        Passive HA state is not enough: a sleepy Zigbee valve that drops off the
        mesh can keep showing a stale ``off`` for hours. The probe drives the
        FSM ``UNREACHABLE`` state and an ``UNREACHABLE_PASSIVE`` notification so
        the user learns about a dead valve *before* the next scheduled run.
        """
        if self._unsub_liveness is not None:
            return
        self._unsub_liveness = async_track_time_interval(
            self._hass, self._on_liveness_tick, timedelta(minutes=interval_min)
        )

    @callback
    def _on_liveness_tick(self, _now) -> None:
        """Time-interval callback: schedule the async reachability probe."""
        self._hass.async_create_task(self.async_ping())

    async def async_ping(self) -> bool:
        """Probe reachability once; return ``True`` when the actuator is live.

        Uses the cheapest backend-appropriate signal: a dedicated availability
        entity when configured (e.g. a Z2M availability sensor), otherwise the
        actuator entity's own state. A failed probe feeds the *existing*
        machinery — the FSM ``OBS_UNAVAILABLE`` event and an
        ``UNREACHABLE_PASSIVE`` notification — rather than inventing a new one.
        """
        source = self._availability_entity or self._entity_id
        state = self._hass.states.get(source)
        raw = state.state if state is not None else None
        reachable = (
            raw not in (None, "unavailable", "unknown", "off")
            if self._availability_entity
            else (state is not None and state.state not in ("unavailable", "unknown"))
        )
        if not reachable:
            if self._fsm.state != ValveState.UNREACHABLE:
                await self._dispatch(ValveEvent.OBS_UNAVAILABLE)
            if self._notifier is not None:
                await self._notifier.notify(
                    self._name,
                    NotificationKind.UNREACHABLE_PASSIVE,
                    Severity.WARNING,
                    context={"duration": "detected by liveness probe"},
                )
            return False
        if self._fsm.state == ValveState.UNREACHABLE:
            await self._dispatch(ValveEvent.OBS_AVAILABLE)
        if self._notifier is not None and self._notifier.is_active(self._name, NotificationKind.UNREACHABLE_PASSIVE):
            await self._notifier.clear(self._name, NotificationKind.UNREACHABLE_PASSIVE)
        return True

    # ── Command driver ───────────────────────────────────────────────────

    async def _run_command(self, cmd: ValveEvent, terminals: tuple[ValveState, ...]) -> OperationResult:
        """Run an open or close cycle with retry on transient failures."""
        start = monotonic()
        precheck = self._precheck()
        if precheck is not None:
            return OperationResult(
                status=precheck[0],
                error_detail=precheck[1],
                retries_used=0,
                duration_ms=self._elapsed_ms(start),
            )
        async with self._lock:
            retries = 0
            while True:
                self._retries_left = self._max_retries - retries
                outcome = await self._run_cycle(cmd, terminals)
                if outcome.status == OperationStatus.OK:
                    return self._finalise(outcome, retries, start)
                if not self._is_retryable(outcome) or retries >= self._max_retries:
                    return self._finalise(outcome, retries, start)
                retries += 1
                await asyncio.sleep(self._backoff_for(retries - 1))

    def _precheck(self) -> tuple[OperationStatus, str] | None:
        """Return a failure tuple if HA state forbids dispatching, else ``None``."""
        if self._fsm.state == ValveState.MAINTENANCE:
            return (OperationStatus.MAINTENANCE, "in_maintenance")
        state = self._hass.states.get(self._entity_id)
        if state is None:
            return (OperationStatus.PRECHECK_FAILED, "switch_entity_not_found")
        if state.state in ("unavailable", "unknown"):
            return (OperationStatus.PRECHECK_FAILED, "switch_unavailable")
        return None

    async def _run_cycle(self, cmd: ValveEvent, terminals: tuple[ValveState, ...]) -> OperationResult:
        """Dispatch ``cmd`` and await the resulting cycle completion.

        After dispatching, if the actuator already reads the *level* the command
        asks for, synthesize the confirming observation instead of waiting for a
        state-change *edge*: a ``switch.*``/``valve.*`` already in the target
        state emits no fresh event, so the FSM would wait on an edge that never
        comes and time out into a spurious ``OPEN_FAILED``/``CLOSE_*`` — which,
        across retries, escalates a *physically working* valve to MAINTENANCE
        (field incident: zone 'Giardino Pino', a Zigbee valve whose confirmation
        lagged the tight adaptive timeout). Confirming by level makes an
        already-open/closed valve report success without forcing actuation. When
        a real edge does arrive it is a harmless noop — the FSM has already left
        the awaited state.
        """
        loop = asyncio.get_event_loop()
        self._completion = loop.create_future()
        self._expected_terminal = terminals
        self._cmd_start_time = monotonic()
        await self._dispatch(cmd)
        if not self._completion.done():
            await self._confirm_if_already_at_level(cmd)
        return await self._completion

    async def _confirm_if_already_at_level(self, cmd: ValveEvent) -> None:
        """Confirm ``cmd`` from the current entity *level* when no edge will come.

        Reads the normalized state (a level, not an edge). If it already matches
        the level the command targets, dispatch the confirming observation so the
        FSM completes at once. The synthetic confirmation goes straight to the FSM
        and is never recorded as a latency sample, so it cannot skew the adaptive
        timeout.
        """
        confirm = _CONFIRM_FOR_CMD.get(cmd)
        if confirm is None:
            return
        target_level, obs_event = confirm
        if self._read_normalized_state() == target_level:
            await self._dispatch(obs_event)

    def _is_retryable(self, outcome: OperationResult) -> bool:
        """``True`` if ``outcome`` describes a transient (comms) failure."""
        if outcome.status != OperationStatus.FAILED or outcome.error_detail is None:
            return False
        try:
            kind = FailureKind(outcome.error_detail)
        except ValueError:
            return False
        return kind in _TRANSIENT_FAILURES

    def _backoff_for(self, retry_index: int) -> float:
        """Return the sleep duration before retry number ``retry_index``."""
        if not self._backoff_s:
            return 0.0
        idx = min(retry_index, len(self._backoff_s) - 1)
        return self._backoff_s[idx]

    def _finalise(self, outcome: OperationResult, retries: int, start: float) -> OperationResult:
        """Stamp timing and retry count on an :class:`OperationResult`."""
        return OperationResult(
            status=outcome.status,
            error_detail=outcome.error_detail,
            retries_used=retries,
            duration_ms=self._elapsed_ms(start),
        )

    @staticmethod
    def _elapsed_ms(start: float) -> float:
        """Return milliseconds elapsed since ``start`` (``monotonic()`` based)."""
        return (monotonic() - start) * 1000.0

    # ── FSM bridging ─────────────────────────────────────────────────────

    async def _dispatch(self, event: ValveEvent) -> None:
        """Push ``event`` into the FSM, execute its actions, settle the future."""
        result = self._fsm.dispatch(event)
        await self._execute_actions(result.actions)
        self._check_terminal(result)
        self._manage_watchdog()

    async def _execute_actions(self, actions: tuple[FsmAction, ...]) -> None:
        """Run every action returned by the FSM, in order."""
        for action in actions:
            await self._execute_action(action)

    async def _execute_action(self, action: FsmAction) -> None:
        """Execute a single FSM action against Home Assistant."""
        if isinstance(action, SendSwitchOn):
            await self._call_actuator(on=True)
        elif isinstance(action, SendSwitchOff):
            await self._call_actuator(on=False)
        elif isinstance(action, StartTimer):
            self._start_timer(action.name, action.seconds)
        elif isinstance(action, CancelTimer):
            self._cancel_timer(action.name)
        elif isinstance(action, CancelAllTimers):
            self._cancel_all_timers()
        elif isinstance(action, NotifyFailure):
            await self._notify_failure(action.kind)
        elif isinstance(action, EnterMaintenance):
            await self._notify_maintenance()

    def _check_terminal(self, result: TransitionResult) -> None:
        """If ``result`` terminates the current cycle, resolve the awaiting future."""
        if self._completion is None or self._completion.done():
            return
        if result.to_state == ValveState.MAINTENANCE:
            detail = result.failure.value if result.failure else "in_maintenance"
            self._completion.set_result(OperationResult(OperationStatus.MAINTENANCE, detail))
            return
        if result.failure is not None:
            self._completion.set_result(OperationResult(OperationStatus.FAILED, result.failure.value))
            return
        if result.to_state in self._expected_terminal:
            self._completion.set_result(OperationResult(OperationStatus.OK))

    # ── Notifier bridging ────────────────────────────────────────────────

    async def _notify_failure(self, kind: FailureKind) -> None:
        """Surface a FailureKind via the notifier (or log if none configured).

        ``CLOSE_LEAK`` is intentionally suppressed here: :meth:`async_turn_off`
        attempts recovery first, and :meth:`_escalate_stuck_open` sends CRITICAL
        only if recovery fails. Transient failures are also suppressed while
        retry attempts remain: on a flaky Zigbee mesh a late confirmation is
        routine, and alerting on every attempt produced spurious CRITICALs.
        """
        if kind in _TRANSIENT_FAILURES and self._retries_left > 0:
            _LOGGER.warning(
                "Driver '%s' transient failure %s — retrying (%d attempt(s) left)",
                self._name,
                kind.name,
                self._retries_left,
            )
            return
        _LOGGER.error("Driver '%s' failure: %s", self._name, kind.name)
        if self._notifier is None:
            return
        if kind == FailureKind.CLOSE_LEAK:
            return
        severity = Severity.CRITICAL if kind == FailureKind.CLOSE_VERIFICATION_FAILED else Severity.WARNING
        await self._notifier.notify(
            self._name,
            NotificationKind.COMMAND_FAILED,
            severity,
            context={"operation": _OPERATION_FOR_FAILURE[kind], "error_detail": kind.value},
        )

    async def _attempt_leak_recovery(self) -> bool:
        """Last-resort recovery from ``CLOSE_LEAK``: re-issue close, re-check flow."""
        _LOGGER.warning(
            "Driver '%s' CLOSE_LEAK detected — attempting recovery (direct close + recheck)",
            self._name,
        )
        await self._call_actuator(on=False)
        await asyncio.sleep(self._fsm_config.leak_timeout_s)

        if self._flow_sensor_entity_id is None:
            return False
        state = self._hass.states.get(self._flow_sensor_entity_id)
        if state is None:
            return False
        try:
            flow = float(state.state)
        except (ValueError, TypeError):
            return False
        recovered = flow <= self._flow_zero_threshold
        if recovered:
            _LOGGER.info("Driver '%s' leak recovery succeeded (flow=%.3f)", self._name, flow)
        else:
            _LOGGER.error("Driver '%s' leak recovery failed (flow=%.3f)", self._name, flow)
        return recovered

    async def _escalate_stuck_open(self) -> None:
        """Trigger integration-wide emergency stop + CRITICAL notification."""
        _LOGGER.error(
            "Driver '%s' stuck-open confirmed after recovery; calling never_dry.stop and escalating",
            self._name,
        )
        try:
            await self._hass.services.async_call("never_dry", "stop", {}, blocking=False)
        except Exception as exc:
            _LOGGER.error("Failed to trigger emergency stop for stuck-open '%s': %s", self._name, exc)
        if self._notifier is None:
            return
        await self._notifier.notify(
            self._name,
            NotificationKind.STUCK_OPEN,
            Severity.CRITICAL,
            context={"flow": "still positive after recovery attempt"},
        )

    async def _notify_maintenance(self) -> None:
        """Notify that the actuator just entered MAINTENANCE."""
        _LOGGER.error(
            "Driver '%s' entered MAINTENANCE (consecutive failures = %d)",
            self._name,
            self._fsm.failure_count,
        )
        if self._notifier is None:
            return
        await self._notifier.notify(
            self._name,
            NotificationKind.ZONE_DISABLED,
            Severity.CRITICAL,
            context={"failures": self._fsm.failure_count},
        )

    # ── HA service helper (adapter-routed: switch.* or valve.*) ──────────

    async def _call_actuator(self, *, on: bool) -> None:
        """Invoke the open/close service on the configured entity, logging errors."""
        domain, service = self._adapter.command(on=on)
        try:
            await self._hass.services.async_call(
                domain,
                service,
                {"entity_id": self._entity_id},
                blocking=False,
            )
        except Exception as exc:
            _LOGGER.error("Driver '%s' %s.%s call raised: %s", self._name, domain, service, exc)

    def _read_normalized_state(self) -> str:
        """Return the actuator's normalized state via the adapter."""
        state = self._hass.states.get(self._entity_id)
        return self._adapter.interpret_state(state.state if state is not None else None)

    def _driver_is_off(self) -> bool:
        """``True`` when the actuator entity currently reads closed/off."""
        return self._read_normalized_state() == "off"

    # ── Watchdog + hardware max-duration timer ───────────────────────────

    def _manage_watchdog(self) -> None:
        """Start the absolute watchdog while open; cancel it otherwise.

        On the first transition into an open state also schedules the hardware
        max-duration write (if configured), so the on-device timer is set even
        if HA later loses communication.
        """
        if self._fsm.state in _OPEN_STATES:
            if self._watchdog_task is None or self._watchdog_task.done():
                self._watchdog_task = self._hass.async_create_task(self._watchdog())
                self._hass.async_create_task(self._set_hw_max_duration())
        else:
            self._cancel_watchdog()

    def _cancel_watchdog(self) -> None:
        """Cancel the watchdog task and reset the hw-duration sentinel."""
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()
        self._watchdog_task = None
        self._hw_duration_set = False

    async def _set_hw_max_duration(self) -> None:
        """Write max_open_duration to the actuator's on-device hardware timer.

        Called once per open cycle (idempotent via ``_hw_duration_set``). Prefers
        a HA ``number`` entity (``hw_max_duration_entity``); falls back to raw
        ``mqtt.publish`` (``hw_max_duration_topic``) with a configurable payload
        template rendered with ``{value}``. Failures are logged, never raised.
        """
        if self._hw_duration_set:
            return
        has_entity = self._hw_max_duration_entity is not None
        has_topic = self._hw_max_duration_topic is not None
        if not has_entity and not has_topic:
            return
        self._hw_duration_set = True
        value = round(self._current_max_open_duration() * self._hw_max_duration_multiplier, 1)

        if has_entity:
            try:
                await self._hass.services.async_call(
                    "number",
                    "set_value",
                    {"entity_id": self._hw_max_duration_entity, "value": value},
                    blocking=False,
                )
                _LOGGER.debug(
                    "Driver '%s' hardware max_duration set to %.1f via entity %s",
                    self._name,
                    value,
                    self._hw_max_duration_entity,
                )
                return
            except Exception as exc:
                _LOGGER.warning(
                    "Driver '%s' failed to set hardware max_duration via entity %s: %s; trying MQTT",
                    self._name,
                    self._hw_max_duration_entity,
                    exc,
                )

        if has_topic:
            try:
                payload = self._hw_max_duration_payload_template.format(value=value)
                await self._hass.services.async_call(
                    "mqtt",
                    "publish",
                    {"topic": self._hw_max_duration_topic, "payload": payload},
                    blocking=False,
                )
                _LOGGER.debug(
                    "Driver '%s' hardware max_duration set to %.1f via MQTT topic %s",
                    self._name,
                    value,
                    self._hw_max_duration_topic,
                )
            except Exception as exc:
                _LOGGER.warning(
                    "Driver '%s' failed to set hardware max_duration via MQTT topic %s: %s",
                    self._name,
                    self._hw_max_duration_topic,
                    exc,
                )

    async def _watchdog(self) -> None:
        """Absolute safety timer: force-close the actuator if it stays open too long."""
        max_open_s = self._current_max_open_duration()
        try:
            await asyncio.sleep(max_open_s)
        except asyncio.CancelledError:
            return
        _LOGGER.error(
            "Driver '%s' watchdog triggered after %.0f s open — forcing close",
            self._name,
            max_open_s,
        )
        await self._call_actuator(on=False)
        if self._notifier is not None:
            await self._notifier.notify(
                self._name,
                NotificationKind.WATCHDOG_TRIGGERED,
                Severity.CRITICAL,
                context={"duration_min": int(max_open_s / 60)},
            )

    # ── Timer plumbing ───────────────────────────────────────────────────

    def _start_timer(self, name: TimerName, seconds: float) -> None:
        """Start (or restart) a named timer that dispatches the matching TIMEOUT."""
        self._cancel_timer(name)
        if name == TimerName.OPEN and len(self._latency.open._samples) >= MIN_SAMPLES:
            seconds = self._latency.open_timeout_s()
        elif name == TimerName.CLOSE and len(self._latency.close._samples) >= MIN_SAMPLES:
            seconds = self._latency.close_timeout_s()
        self._timers[name] = self._hass.async_create_task(self._timer(name, seconds))

    def _cancel_timer(self, name: TimerName) -> None:
        """Cancel ``name`` if it is running; safe to call when absent."""
        task = self._timers.pop(name, None)
        if task and not task.done():
            task.cancel()

    def _cancel_all_timers(self) -> None:
        """Cancel every active timer for this actuator."""
        for name in list(self._timers):
            self._cancel_timer(name)

    async def _timer(self, name: TimerName, seconds: float) -> None:
        """Background task: sleep, then dispatch the matching ``TIMEOUT_*`` event."""
        try:
            await asyncio.sleep(seconds)
            await self._dispatch(_TIMEOUT_EVENT_FOR_TIMER[name])
        except asyncio.CancelledError:
            # Expected, and the normal way a timer ends: `_cancel_timer` cancels
            # this task whenever the event it guards arrives first — a valve that
            # confirms before its timeout, a session that stops early. Swallowing
            # it here keeps cancellation from surfacing as a spurious error; a
            # real timeout still dispatches its event on the line above.
            pass

    # ── HA state listeners ───────────────────────────────────────────────

    @callback
    def _on_switch_state(self, event) -> None:
        """HA callback: schedule the async actuator-state handler."""
        self._hass.async_create_task(self._handle_switch_state(event))

    @callback
    def _on_flow_state(self, event) -> None:
        """HA callback: schedule the async flow-state handler."""
        self._hass.async_create_task(self._handle_flow_state(event))

    async def _handle_switch_state(self, event) -> None:
        """Map an actuator state change to the matching FSM observation."""
        new_state = event.data.get("new_state")
        if new_state is None:
            return
        value = self._adapter.interpret_state(new_state.state)
        if value in ("unavailable", "unknown"):
            await self._dispatch(ValveEvent.OBS_UNAVAILABLE)
            return
        if value == "transitional":
            # Valve is moving (opening/closing); wait for it to settle.
            return
        if self._fsm.state == ValveState.UNREACHABLE:
            await self._dispatch(ValveEvent.OBS_AVAILABLE)
        if value == "on":
            self._record_latency(self._latency.open, ValveState.REQ_OPEN, self._latency.open_timeout_s)
            await self._dispatch(ValveEvent.OBS_SWITCH_ON)
        elif value == "off":
            self._record_latency(self._latency.close, ValveState.REQ_CLOSE, self._latency.close_timeout_s)
            await self._dispatch(ValveEvent.OBS_SWITCH_OFF)

    def _record_latency(self, window, awaited_state: ValveState, timeout_getter: Callable[[], float]) -> None:
        """Record the command→confirmation latency if we were awaiting ``awaited_state``."""
        if self._fsm.state != awaited_state or self._cmd_start_time is None:
            return
        latency_ms = (monotonic() - self._cmd_start_time) * 1000.0
        self._cmd_start_time = None
        window.record(latency_ms)
        self._hass.async_create_task(self._latency.async_save())
        _LOGGER.debug(
            "Driver '%s' confirmation latency %.1f ms → adaptive timeout %.2f s",
            self._name,
            latency_ms,
            timeout_getter(),
        )

    async def _handle_flow_state(self, event) -> None:
        """Map a flow-sensor state change to OBS_FLOW_POSITIVE/OBS_FLOW_ZERO."""
        new_state = event.data.get("new_state")
        if new_state is None:
            return
        try:
            flow = float(new_state.state)
        except (ValueError, TypeError):
            return
        if flow > self._flow_zero_threshold:
            await self._dispatch(ValveEvent.OBS_FLOW_POSITIVE)
        else:
            await self._dispatch(ValveEvent.OBS_FLOW_ZERO)


# ── Zone actuator (the model's ``ZoneDriver``) ─────────────────────────────


class ZoneDriver(Driver):
    """Drive one zone's actuator, translating a **liters** request into water.

    ``deliver(liters)`` is the contract with the Zone: the zone always asks for
    liters and only the driver knows whether to deliver them by native volume,
    by measured flow, or by time x flow rate. It returns a :class:`DeliveryResult`
    stating the delivered liters *as truthfully as the backend allows*.

    All three strategies live behind this one seam — including ``volume_preset``,
    whose smart-valve self-close is a *strategy detail* here rather than a bypass
    of the whole abstraction (closes anomalies §A2/§D1).
    """

    role = "zone"

    def __init__(
        self,
        hass: HomeAssistant,
        entity_id: str,
        *,
        delivery_mode: str | DeliveryMode = DeliveryMode.ESTIMATED_FLOW,
        flow_rate_lpm: float = 0.0,
        volume_entity: str | None = None,
        flow_meter_sensor: str | None = None,
        delivery_timeout_s: float = DEFAULT_DELIVERY_TIMEOUT_S,
        auto_open_grace_s: float = AUTO_OPEN_GRACE_S,
        **base_kwargs,
    ) -> None:
        """Configure a zone actuator; ``base_kwargs`` flow to :class:`Driver`.

        ``delivery_timeout_s`` is a **ceiling on the job**, not an absolute
        constant: the caller derives it from the expected duration (volume over
        the declared flow rate) times a margin, capped by the user's configured
        safety timeout — see ``IrrigationZoneSensor.delivery_timeout``. Passing
        a bare constant here is what let a stalled meter keep a valve open for
        an hour on a five-minute job (GH #173), because the only exit from the
        loops below is the meter reaching target or this timeout expiring. Take
        it once, before opening: the deficit shrinks as water arrives, so a
        bound recomputed mid-session follows the session it should bound.
        """
        super().__init__(hass, entity_id, flow_sensor_entity_id=flow_meter_sensor, **base_kwargs)
        self._delivery_mode = DeliveryMode(delivery_mode)
        self._flow_rate_lpm = flow_rate_lpm
        self._volume_entity = volume_entity
        self._flow_meter_sensor = flow_meter_sensor
        self._delivery_timeout_s = delivery_timeout_s
        self._auto_open_grace_s = auto_open_grace_s

    @property
    def delivery_mode(self) -> DeliveryMode:
        """The configured delivery strategy."""
        return self._delivery_mode

    # ── The liters → actuation contract ──────────────────────────────────

    async def deliver(
        self,
        liters: float,
        *,
        should_abort: Callable[[], bool] | None = None,
        on_progress: Callable[[float], None] | None = None,
    ) -> DeliveryResult:
        """Deliver ``liters`` of water and return a truthful :class:`DeliveryResult`.

        ``should_abort`` lets the caller stop delivery early (global/zone stop);
        ``on_progress`` receives the running delivered-liters figure so the caller
        can update a live deficit. Polymorphic dispatch over :class:`DeliveryMode`
        replaces the controller's ``if mode == ...`` chain.
        """
        if liters <= 0:
            return DeliveryResult(0.0, DeliveryQuality.MEASURED, 0.0, liters)
        if self._delivery_mode == DeliveryMode.ESTIMATED_FLOW:
            return await self._deliver_estimated_flow(liters, should_abort)
        if self._delivery_mode == DeliveryMode.FLOW_METER:
            return await self._deliver_flow_meter(liters, should_abort, on_progress)
        if self._delivery_mode == DeliveryMode.VOLUME_PRESET:
            return await self._deliver_volume_preset(liters, should_abort)
        _LOGGER.error("Unknown delivery mode '%s' for zone '%s'", self._delivery_mode, self._name)
        return DeliveryResult(0.0, DeliveryQuality.LOW_CONFIDENCE, 0.0, liters, detail="unknown_delivery_mode")

    # ── Strategy 1: estimated flow (open, wait, close) ───────────────────

    async def _deliver_estimated_flow(self, liters: float, should_abort: Callable[[], bool] | None) -> DeliveryResult:
        """Open, wait the duration implied by the nominal flow rate, close.

        No measurement: the delivered figure is time-based, so it is ``estimated``
        (``partial`` when interrupted or auto-closed early).
        """
        if self._flow_rate_lpm <= 0:
            _LOGGER.warning("Zone '%s' has no flow_rate configured; cannot estimate duration", self._name)
            return DeliveryResult(0.0, DeliveryQuality.LOW_CONFIDENCE, 0.0, liters, detail="no_flow_rate")
        duration = liters / self._flow_rate_lpm * 60.0
        if duration <= 0:
            return DeliveryResult(0.0, DeliveryQuality.MEASURED, 0.0, liters)

        if (result := await self.async_turn_on()).status != OperationStatus.OK:
            return DeliveryResult(0.0, DeliveryQuality.LOW_CONFIDENCE, 0.0, liters, detail=result.error_detail)

        elapsed = await self._wait_with_abort(duration, should_abort)
        await self.async_turn_off()
        delivered = liters * min(elapsed, duration) / duration
        quality = DeliveryQuality.ESTIMATED if elapsed >= duration else DeliveryQuality.PARTIAL
        return DeliveryResult(delivered, quality, elapsed, liters)

    # ── Strategy 2: flow meter (cumulative volume or integrated rate) ────

    async def _deliver_flow_meter(
        self,
        liters: float,
        should_abort: Callable[[], bool] | None,
        on_progress: Callable[[float], None] | None,
    ) -> DeliveryResult:
        """Open, monitor the flow sensor, close when the target volume is reached."""
        meter = self._flow_meter_sensor
        if not meter:
            _LOGGER.error("Zone '%s' has no flow_meter_sensor configured", self._name)
            return DeliveryResult(0.0, DeliveryQuality.LOW_CONFIDENCE, 0.0, liters, detail="no_flow_meter")
        if flow_utils.is_flow_rate_sensor(self._hass, meter):
            return await self._deliver_by_rate(liters, meter, should_abort, on_progress)
        return await self._deliver_by_volume(liters, meter, should_abort, on_progress)

    async def _deliver_by_volume(
        self,
        liters: float,
        meter: str,
        should_abort: Callable[[], bool] | None,
        on_progress: Callable[[float], None] | None,
    ) -> DeliveryResult:
        """Cumulative-volume meter: deliver by the delta between start and current reading."""
        initial = flow_utils.read_volume_liters(self._hass, meter)
        if initial is None:
            _LOGGER.error("Flow meter '%s' unavailable for zone '%s'", meter, self._name)
            return DeliveryResult(0.0, DeliveryQuality.LOW_CONFIDENCE, 0.0, liters, detail="meter_unavailable")

        if (result := await self.async_turn_on()).status != OperationStatus.OK:
            return DeliveryResult(0.0, DeliveryQuality.LOW_CONFIDENCE, 0.0, liters, detail=result.error_detail)

        delivered = 0.0
        elapsed = 0.0
        timeout = self._delivery_timeout_s
        reached_target = False
        while elapsed < timeout:
            if should_abort and should_abort():
                break
            await asyncio.sleep(FLOW_METER_POLL_INTERVAL_S)
            elapsed += FLOW_METER_POLL_INTERVAL_S
            if self._driver_is_off():
                break  # hardware auto-close — stop polling a dead meter
            current = flow_utils.read_volume_liters(self._hass, meter)
            if current is None:
                continue
            delivered = current - initial
            if delivered < 0:
                initial = 0.0
                delivered = current
            if on_progress:
                on_progress(delivered)
            if delivered >= liters:
                reached_target = True
                break

        await self.async_turn_off()
        return self._settle(liters, delivered, elapsed, reached_target)

    async def _deliver_by_rate(
        self,
        liters: float,
        meter: str,
        should_abort: Callable[[], bool] | None,
        on_progress: Callable[[float], None] | None,
    ) -> DeliveryResult:
        """Flow-rate meter: integrate the rate (normalized to L/min) over time."""
        if (result := await self.async_turn_on()).status != OperationStatus.OK:
            return DeliveryResult(0.0, DeliveryQuality.LOW_CONFIDENCE, 0.0, liters, detail=result.error_detail)

        delivered = 0.0
        elapsed = 0.0
        timeout = self._delivery_timeout_s
        reached_target = False
        while elapsed < timeout:
            if should_abort and should_abort():
                break
            await asyncio.sleep(FLOW_METER_POLL_INTERVAL_S)
            elapsed += FLOW_METER_POLL_INTERVAL_S
            if self._driver_is_off():
                break
            rate = flow_utils.read_flow_meter(self._hass, meter)
            if rate is None or rate < 0:
                continue
            unit = flow_utils.get_flow_meter_unit(self._hass, meter)
            delivered += flow_utils.rate_to_lpm(rate, unit) / 60.0 * FLOW_METER_POLL_INTERVAL_S
            if on_progress:
                on_progress(delivered)
            if delivered >= liters:
                reached_target = True
                break

        await self.async_turn_off()
        return self._settle(liters, delivered, elapsed, reached_target)

    def _settle(self, liters: float, delivered: float, elapsed: float, reached_target: bool) -> DeliveryResult:
        """Build the flow-meter :class:`DeliveryResult`, falling back when nothing measured.

        A session that ran with the valve open but measured zero flow is far more
        likely a dead/stale sensor than a dry pipe: credit the nominal flow rate
        so the deficit still settles (the field bug behind the zero-measured-flow
        retry loop), labeled ``estimated``.
        """
        if delivered > 0:
            quality = DeliveryQuality.MEASURED if reached_target else DeliveryQuality.PARTIAL
            return DeliveryResult(delivered, quality, elapsed, liters)
        if elapsed > 0 and self._flow_rate_lpm > 0:
            estimate = self._flow_rate_lpm * elapsed / 60.0
            _LOGGER.warning(
                "Zone '%s': flow sensor measured 0 L after %.0fs open — crediting estimated %.1fL",
                self._name,
                elapsed,
                estimate,
            )
            return DeliveryResult(estimate, DeliveryQuality.ESTIMATED, elapsed, liters, detail="fallback_estimate")
        _LOGGER.warning(
            "Zone '%s': flow sensor measured 0 L and no flow_rate to estimate from; deficit unsettled",
            self._name,
        )
        return DeliveryResult(0.0, DeliveryQuality.LOW_CONFIDENCE, elapsed, liters, detail="no_measurement")

    # ── Strategy 3: volume preset (smart self-piloting valve) ────────────

    async def _deliver_volume_preset(self, liters: float, should_abort: Callable[[], bool] | None) -> DeliveryResult:
        """Arm the smart-valve dose, ensure it opens, wait for its self-close.

        Smart valves accept a dose on a ``number`` entity and close themselves;
        they drive their own state and do not fit the FSM's "I command, you obey"
        semantics, so this strategy talks to the actuator directly. It still lives
        behind the uniform ``deliver()`` seam and reuses the base precheck.
        """
        if not self._volume_entity:
            _LOGGER.error("Zone '%s' has no volume_entity configured", self._name)
            return DeliveryResult(0.0, DeliveryQuality.LOW_CONFIDENCE, 0.0, liters, detail="no_volume_entity")
        precheck = self._precheck()
        if precheck is not None:
            if self._notifier is not None:
                await self._notifier.notify(
                    self._name,
                    NotificationKind.UNREACHABLE_AT_IRRIGATION,
                    Severity.WARNING,
                    context={"reason": precheck[1]},
                )
            return DeliveryResult(0.0, DeliveryQuality.LOW_CONFIDENCE, 0.0, liters, detail=precheck[1])

        # 1) Arm the dose.
        await self._hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": self._volume_entity, "value": round(liters, 1)},
            blocking=False,
        )

        # 2-3) Grace window for auto-open; explicit open as fallback.
        if not await self._wait_for_auto_open(self._auto_open_grace_s):
            _LOGGER.info(
                "Zone '%s': smart valve did not auto-open within %.1fs, sending open",
                self._name,
                self._auto_open_grace_s,
            )
            await self._call_actuator(on=True)

        # 4) Wait for the smart valve to finish (self-close), stop or timeout.
        elapsed = 0.0
        timeout = self._delivery_timeout_s
        while elapsed < timeout:
            if should_abort and should_abort():
                await self._call_actuator(on=False)
                estimate = self._flow_rate_lpm * elapsed / 60.0 if self._flow_rate_lpm > 0 else 0.0
                return DeliveryResult(min(liters, estimate), DeliveryQuality.PARTIAL, elapsed, liters, detail="aborted")
            await asyncio.sleep(FLOW_METER_POLL_INTERVAL_S)
            elapsed += FLOW_METER_POLL_INTERVAL_S
            if self._driver_is_off():
                return DeliveryResult(liters, DeliveryQuality.MEASURED, elapsed, liters)

        _LOGGER.warning("Zone '%s' volume_preset timeout (%.0fs). Forcing close.", self._name, timeout)
        await self._call_actuator(on=False)
        return DeliveryResult(liters, DeliveryQuality.LOW_CONFIDENCE, elapsed, liters, detail="timeout")

    async def _wait_for_auto_open(self, grace_s: float) -> bool:
        """Poll the actuator state up to ``grace_s`` for a smart-valve auto-open."""
        elapsed = 0.0
        step = min(0.5, grace_s)
        while elapsed < grace_s:
            if self._read_normalized_state() == "on":
                return True
            await asyncio.sleep(step)
            elapsed += step
        return self._read_normalized_state() == "on"

    # ── Shared wait helper ───────────────────────────────────────────────

    async def _wait_with_abort(self, duration_s: float, should_abort: Callable[[], bool] | None) -> float:
        """Sleep up to ``duration_s``, breaking early on abort or external close."""
        elapsed = 0.0
        while elapsed < duration_s:
            if should_abort and should_abort():
                break
            step = min(FLOW_METER_POLL_INTERVAL_S, duration_s - elapsed)
            await asyncio.sleep(step)
            elapsed += step
            if self._driver_is_off():
                break
        return elapsed


# ── Master actuator (the model's ``MasterDriver``) ─────────────────────────


class MasterDriver(Driver):
    """Drive shared hydraulics (a master valve or pump) by following zone activity.

    It takes no irrigation decisions and has no notion of liters: it is ON while
    any zone actuator is active and OFF once none are, after a configurable
    off-delay linger that avoids pump cycling during sequential zone runs (GH #95).
    Modeling it as an :class:`Driver` means the safety layers — never leave the
    pump running on error/stop/restart — are inherited from the base, not
    duplicated.

    Note: "pump" is a *role*, not a HA device type — HA has no ``pump.*`` domain
    or ``pump`` device_class. A pump is driven as a ``switch.*`` entity (a relay),
    which the :class:`ValveCommandAdapter` handles transparently; ``MasterDriver``
    adds only the pump-specific behaviour (``off_delay_s`` linger). This is why the
    domain model folds pump and master valve into the single ``MasterDriver``.
    """

    role = "master"

    def __init__(
        self,
        hass: HomeAssistant,
        entity_id: str,
        *,
        off_delay_s: float = DEFAULT_MASTER_OFF_DELAY_S,
        **base_kwargs,
    ) -> None:
        """Configure a master actuator; ``base_kwargs`` flow to :class:`Driver`."""
        super().__init__(hass, entity_id, **base_kwargs)
        self._off_delay_s = off_delay_s
        self._linger_task: asyncio.Task | None = None

    @property
    def off_delay_s(self) -> float:
        """The linger before following the zones back to OFF."""
        return self._off_delay_s

    async def follow(self, any_zone_active: bool) -> None:
        """React to aggregate zone activity: ON when any is active, OFF (delayed) when none.

        Called by the orchestrator on every change in aggregate activity. Turning
        ON cancels a pending off-linger; turning inactive schedules the off after
        ``off_delay_s`` so a brief gap between two sequential zones does not cycle
        the pump.
        """
        if any_zone_active:
            self._cancel_linger()
            if not self.is_open:
                await self.async_turn_on()
            return
        if self._linger_task is None or self._linger_task.done():
            self._linger_task = self._hass.async_create_task(self._linger_off())

    async def _linger_off(self) -> None:
        """Wait the off-delay, then close the master actuator."""
        try:
            await asyncio.sleep(self._off_delay_s)
        except asyncio.CancelledError:
            return
        await self.async_turn_off()

    def _cancel_linger(self) -> None:
        """Cancel a pending off-linger (a new zone became active)."""
        if self._linger_task and not self._linger_task.done():
            self._linger_task.cancel()
        self._linger_task = None

    def async_unload(self) -> None:
        """Cancel the off-linger, then unload the base actuator."""
        self._cancel_linger()
        super().async_unload()


# ── Manual actuator (a valve-less "how": a human with a watering can) ───────


class ManualActuator:
    """A valve-less actuator for hand-watered plants (house plants).

    The third materialization of the domain's "how": there is no hardware to
    drive. Instead of opening a valve it raises an **alert** when a zone's deficit
    says water is due, and the delivery completes when the user presses **Mark
    irrigated**. It deliberately does *not* extend :class:`Driver` — that base
    is all valve machinery (FSM, switch commands, watchdog, liveness), none of
    which applies to a human with a watering can. What it shares is the delivery
    *contract*: it returns a :class:`DeliveryResult`, so the Zone settles its
    deficit exactly as it does for a valve, unaware the "backend" was a person.

    It leans on things that already exist rather than inventing them: the alert is
    a notification (any ``on_alert`` sink — a persistent notification, a mobile
    push, the notifier seam), and **Mark irrigated** is the existing
    ``reset_deficit`` action, reused here as the delivery *confirmation*.

    The asynchronous, human-paced nature is already modelled by
    :class:`DeliveryResult`: :meth:`request_irrigation` returns a ``delayed``
    pending result, :meth:`mark_irrigated` the final ``declared`` one — a human is
    the extreme case of "a backend that measures late".

    Pairing note: a house plant is watered manually (this actuator) *and* its
    demand is not weather-driven, so it pairs with a VWC / indoor water-balance
    model, not ``ETModel``. Actuation and model are orthogonal axes: a house plant
    picks the manual "how" and the indoor "how much".
    """

    role = "manual"

    def __init__(
        self,
        name: str,
        *,
        on_alert: Callable[[str, float, float | None], None] | None = None,
        on_clear: Callable[[str], None] | None = None,
    ) -> None:
        """Configure a manual actuator; ``on_alert``/``on_clear`` raise/clear the user alert."""
        self._name = name
        self._on_alert = on_alert
        self._on_clear = on_clear
        self._pending_liters: float = 0.0
        self._pending: bool = False

    @property
    def name(self) -> str:
        """Human-facing name for the alert."""
        return self._name

    @property
    def is_pending(self) -> bool:
        """``True`` while an alert is open awaiting the user's confirmation."""
        return self._pending

    @property
    def pending_liters(self) -> float:
        """The recommended liters the open alert asks the user to pour."""
        return self._pending_liters

    def request_irrigation(self, liters: float, *, deficit_mm: float | None = None) -> DeliveryResult:
        """Raise the "water this plant" alert; return a pending :class:`DeliveryResult`.

        Nothing is actuated: the result is ``delayed`` with zero delivered so far,
        settled only when the user confirms via :meth:`mark_irrigated`. A repeated
        request while one is pending just refreshes the recommended amount.
        """
        self._pending_liters = liters
        self._pending = True
        if self._on_alert is not None:
            self._on_alert(self._name, liters, deficit_mm)
        return DeliveryResult(0.0, DeliveryQuality.DELAYED, 0.0, liters, detail="awaiting_manual")

    def mark_irrigated(self, liters: float | None = None) -> DeliveryResult:
        """Confirm the plant was hand-watered; return the ``declared`` result and clear the alert.

        With no explicit ``liters`` the recommended amount is assumed (the user
        followed the advice); an explicit figure lets a careful user declare how
        much they actually poured. Either way the amount is *declared*, not
        measured. The Zone settles its deficit with this result.
        """
        requested = self._pending_liters
        delivered = requested if liters is None else max(0.0, liters)
        self._pending = False
        self._pending_liters = 0.0
        if self._on_clear is not None:
            self._on_clear(self._name)
        return DeliveryResult(delivered, DeliveryQuality.DECLARED, 0.0, requested, detail="user_marked")
