"""Irrigation controller for the NeverDry integration.

Manages valve on/off cycles directly. Given a zone (or all zones),
the controller delivers water using one of three delivery modes:
  - estimated_flow: open valve → wait timer-based duration → close valve
  - flow_meter: open valve → monitor flow sensor → close at target volume
  - volume_preset: send volume to smart valve → wait for self-close

Zones are irrigated sequentially to avoid pressure drops.
An emergency stop service closes all valves immediately.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import datetime, timedelta
from typing import ClassVar

from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_change,
    async_track_time_interval,
)

from . import flow_utils, valve_test
from .const import (
    ANOMALY_DEFICIT_MULTIPLIER,
    ATTR_DEFICIT_MM,
    ATTR_ZONE_NAME,
    DEFAULT_BATTERY_LOW_THRESHOLD,
    DEFAULT_INTER_ZONE_DELAY,
    DEFAULT_THRESHOLD,
    DELIVERY_MODE_ESTIMATED_FLOW,
    DELIVERY_MODE_FLOW_METER,
    DELIVERY_MODE_VOLUME_PRESET,
    DOMAIN,
    EVENT_IRRIGATION_COMPLETE,
    EVENT_VALVE_TEST_COMPLETE,
    FLOW_METER_POLL_INTERVAL_S,
    MIN_SERVICE_INTERVAL_S,
    UNUSUAL_FLOW_MAX_LPM,
    UNUSUAL_FLOW_MIN_LPM,
)
from .driver import Driver, OperationStatus, ValveCommandAdapter
from .environment import Reachability
from .reachability_watch import FleetSilenceWatch
from .scheduler import Decision, Scheduler, SkipReason
from .valve_fsm import ValveState
from .valve_notifier import NotificationKind, Severity, ValveNotifier

#: How often the fleet's silence is measured. Hourly on purpose: the silence
#: that means "off the mesh" is measured in hours (the floor derived from live
#: fleets sits between 9 and 16), so a tighter poll buys nothing.
REACHABILITY_POLL = timedelta(hours=1)
#: Quiet period after warning about one valve. The notification persists until
#: dismissed, so repeating it daily adds noise, not information.
REACHABILITY_ALERT_INTERVAL_S: float = 24 * 3600


def _valve_level(hass, entity_id: str | None) -> str:
    """The valve's position in one vocabulary, whatever domain it lives in.

    A ``switch.*`` says ``on``/``off``; a ``valve.*`` says ``open``/``closed``
    and can also say ``opening``/``closing``. Comparing raw states works for one
    and silently fails for the other — a `valve.*` never equals ``"off"``, so
    every "did it close?" check would answer no for ever.

    Routed through the same adapter the driver uses, so there is one place that
    knows the difference rather than a dozen that assume there is none.
    """
    state = hass.states.get(entity_id) if entity_id else None
    return ValveCommandAdapter.interpret_state(state.state if state else None)


async def _valve_call(hass, entity_id: str, *, on: bool) -> None:
    """Open or close ``entity_id``, using the services its own domain provides.

    ``blocking`` is deliberately not passed: Home Assistant already defaults it
    to false, every call site here relied on that default, and adding it would
    change the call shape for no behavioural gain.
    """
    domain, service = ValveCommandAdapter(entity_id).command(on=on)
    await hass.services.async_call(domain, service, {"entity_id": entity_id})


MONITORING_INTERVAL = 6 * 3600  # 6 hours in seconds
AUTO_OPEN_GRACE_S = 3.0  # volume_preset: wait this long for smart-valve auto-open

_LOGGER = logging.getLogger(__name__)


class IrrigationController:
    """Controls irrigation valves based on deficit calculations.

    Holds references to the DrynessIndexSensor and all IrrigationZoneSensors.
    Exposes HA services to trigger irrigation.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        dryness_sensor,
        zone_sensors: list,
        inter_zone_delay: int = DEFAULT_INTER_ZONE_DELAY,
        valve_operators: dict[str, Driver] | None = None,
        notifier: ValveNotifier | None = None,
    ) -> None:
        """Build the irrigation controller.

        ``valve_operators`` is a mapping from valve switch entity id to the
        :class:`~.driver.Driver` that drives it. When ``None`` the controller
        falls back to direct ``hass.services.async_call`` for switch
        operations (used by the test harness and as a safety net for any
        valve without a dedicated operator).
        """
        self._hass = hass
        self._dryness = dryness_sensor
        self._zones = {zs.zone_name: zs for zs in zone_sensors}
        self._inter_zone_delay = inter_zone_delay
        self._valve_operators: dict[str, Driver] = valve_operators or {}
        self._notifier = notifier
        # Decides *when* a zone waters; this class only acts on the answer.
        # Serial is what the code has always done — two handlers each
        # checking 'is something running' — but nowhere did it say so.
        self._scheduler = Scheduler()
        # Tunable from tests; default matches the production grace window.
        self.auto_open_grace_s: float = AUTO_OPEN_GRACE_S
        self._running = False
        self._stop_requested = False
        # Name of a single zone the user asked to stop mid-delivery (per-zone
        # stop, distinct from the global emergency stop). Consumed by the
        # active delivery loop, reset at the start/end of each cycle.
        self._stop_zone: str | None = None
        self._active_valve: str | None = None
        self._irrigation_task: asyncio.Task | None = None
        self._monitoring_mode = not any(zs.valve for zs in zone_sensors)
        self._unsub_monitor = None
        self._unsubs: list = []
        self._reachability = FleetSilenceWatch(hass)
        self._reachability_alerted: dict[str, float] = {}
        self._last_service_call: dict[str, float] = {}
        self._current_source: str | None = None
        # Manual valve tracking: valve_entity_id → flow meter reading at valve open
        self._manual_valve_open: dict[str, float | None] = {}
        # Per manual session: valve_entity_id → (ts_start_wallclock, deficit_mm_pre).
        # Consumed at manual close to emit the SESSION_RESULT structured log line.
        self._manual_session_meta: dict[str, tuple[datetime, float]] = {}
        # Per-valve safety-close watchdog for external (non-commanded) opens
        self._manual_safety_tasks: dict[str, asyncio.Task] = {}
        # Valves currently being closed by the controller — suppresses spurious
        # "manual irrigation detected" events while the close command is in flight.
        self._controller_closing: set[str] = set()
        # Reverse map: valve_entity_id → zone_name
        self._valve_to_zone: dict[str, str] = {zs.valve: zs.zone_name for zs in zone_sensors if zs.valve}
        # Battery sensor → zone_name map
        self._battery_to_zone: dict[str, str] = {
            zs.battery_sensor: zs.zone_name for zs in zone_sensors if zs.battery_sensor
        }
        # Track which zones have already been alerted for low battery
        self._battery_alerted: set[str] = set()
        # Track which zones have already been alerted for anomalous deficit
        self._deficit_anomaly_alerted: set[str] = set()

    @property
    def is_monitoring_mode(self) -> bool:
        """True if no valves are configured (monitoring only)."""
        return self._monitoring_mode

    @property
    def is_running(self) -> bool:
        """Return True if an irrigation cycle is in progress."""
        return self._running

    @property
    def active_valve(self) -> str | None:
        """Return the entity_id of the currently open valve, or None."""
        return self._active_valve

    @property
    def valve_operators(self) -> dict[str, Driver]:
        """Return the mapping of switch entity_id → :class:`~.driver.Driver`."""
        return self._valve_operators

    @property
    def zone_names(self) -> list[str]:
        """Names of the zones this controller manages."""
        return list(self._zones.keys())

    def has_zone(self, zone_name: str) -> bool:
        """True when this controller manages the given zone."""
        return zone_name in self._zones

    def register_services(self) -> None:
        """Register this controller's state listeners and schedulers.

        HA services are NOT registered here: they are per-domain, so a
        second config entry would silently capture every ``never_dry.*``
        call (GH #105). ``services.async_setup_services`` registers them
        once and dispatches to the controller that owns the target zone.
        """
        # Monitor valve state changes to detect manual irrigation
        valve_entities = [v for v in self._valve_to_zone if v]
        if valve_entities:
            self._unsubs.append(async_track_state_change_event(self._hass, valve_entities, self._on_valve_state_change))

        # Monitor battery sensors for low-battery alerts
        battery_entities = [b for b in self._battery_to_zone if b]
        if battery_entities:
            self._unsubs.append(async_track_state_change_event(self._hass, battery_entities, self._on_battery_change))

        # Periodic deficit anomaly check (all modes, every 6h)
        from datetime import timedelta

        self._unsubs.append(
            async_track_time_interval(
                self._hass,
                self._check_deficit_anomaly,
                timedelta(hours=6),
            )
        )

        # Set up automatic irrigation per zone based on mode
        for zs in self._zones.values():
            if not zs.valve:
                continue
            mode = zs.irrigation_mode

            if mode == "scheduled" and zs.irrigation_time:
                try:
                    parts = zs.irrigation_time.split(":")
                    hour, minute = int(parts[0]), int(parts[1])
                    self._unsubs.append(
                        async_track_time_change(
                            self._hass,
                            self._make_scheduled_handler(zs.zone_name),
                            hour=hour,
                            minute=minute,
                            second=0,
                        )
                    )
                    _LOGGER.info(
                        "Mode B (scheduled): zone='%s' at %s",
                        zs.zone_name,
                        zs.irrigation_time,
                    )
                except (ValueError, IndexError):
                    _LOGGER.error(
                        "Invalid irrigation_time '%s' for zone '%s'",
                        zs.irrigation_time,
                        zs.zone_name,
                    )

            elif mode == "reactive":
                # Register listener on dryness sensor for reactive mode
                zs._dryness.register_zone_listener(
                    self._make_reactive_handler(zs.zone_name),
                )
                _LOGGER.info(
                    "Mode A (reactive): zone='%s', threshold=%.1fmm",
                    zs.zone_name,
                    zs._threshold,
                )

        # Start monitoring mode if no valves are configured
        if self._monitoring_mode:
            _LOGGER.info(
                "No valves configured — running in monitoring mode. "
                "Irrigation alerts will be sent every 6 hours when needed."
            )
            self._unsub_monitor = async_track_time_interval(
                self._hass,
                self._check_and_notify,
                timedelta(hours=6),
            )
            self._unsubs.append(self._unsub_monitor)

        # Reachability watch: notice a valve that has stopped answering *before*
        # the evening it was needed. Hourly because the thing being detected is
        # measured in hours — the floor derived from real fleets sits between 9
        # and 16 — and a tighter poll would only add cost to the same answer.
        if not self._monitoring_mode:
            self._unsubs.append(
                async_track_time_interval(
                    self._hass,
                    self._check_reachability,
                    REACHABILITY_POLL,
                )
            )

    async def _check_reachability(self, _now=None) -> None:
        """Judge every valve's silence against its siblings, and warn once a day.

        The verdict feeds the machinery that already exists: the zone's
        ``valve_reachable`` flag (hence the card's amber chip and its warning
        text) and a single notification. It never touches irrigation — a valve
        judged silent is still commanded when its time comes, because the
        judgement is evidence about the mesh, not proof about the valve, and
        refusing to try on statistical grounds would turn a warning into an
        outage.
        """
        drivers = {
            name: driver
            for name, zone in self._zones.items()
            if zone.valve and (driver := self._valve_operators.get(zone.valve)) is not None
        }
        if len(drivers) < 2:
            return  # nothing to compare against; the criterion needs siblings
        verdicts = self._reachability.observe(drivers)
        now = time.monotonic()
        for zone_name, verdict in verdicts.items():
            zone = self._zones.get(zone_name)
            if zone is None:
                continue
            silent = verdict.status == Reachability.SILENT
            zone.set_silence_verdict(silent if verdict.status != Reachability.UNKNOWN else None)
            zone.async_write_ha_state()
            if not silent:
                self._reachability_alerted.pop(zone_name, None)
                continue
            # One alert per valve per day. The notification persists until it is
            # dismissed, so repeating it adds noise without adding information.
            last = self._reachability_alerted.get(zone_name)
            if last is not None and (now - last) < REACHABILITY_ALERT_INTERVAL_S:
                continue
            self._reachability_alerted[zone_name] = now
            _LOGGER.warning(
                "Zone '%s': valve silent for %.0f min while its siblings are not "
                "(threshold %.0f min) — likely off the mesh",
                zone_name,
                verdict.silence_s / 60,
                (verdict.threshold_s or 0) / 60,
            )
            if self._notifier is not None:
                await self._notifier.notify(
                    zone_name,
                    NotificationKind.UNREACHABLE_PASSIVE,
                    Severity.WARNING,
                    context={"duration": f"{verdict.silence_s / 3600:.1f} h"},
                )

    # ── Scheduled irrigation ────────────────────────────────

    #: Why a zone that was considered is not watering, in words the log can use.
    _SKIP_MESSAGES: ClassVar[dict[SkipReason, str]] = {
        SkipReason.NOTHING_TO_REFILL: "nothing to refill",
        SkipReason.BELOW_THRESHOLD: "deficit below the zone threshold",
        SkipReason.ALREADY_RUNNING: "another irrigation is in progress",
        SkipReason.THROTTLED: "called again too soon",
    }

    def _act_on(self, decision: Decision, zone_name: str, zone) -> None:
        """Carry out the scheduler's answer, or say why there is nothing to do.

        The seam this whole object exists for: **the scheduler decides, the
        controller acts.** Everything above this line is a rule that can be read
        and tested on its own; everything below is Home Assistant mechanics.
        """
        if not decision.should_irrigate:
            _LOGGER.info(
                "Zone '%s' not watering: %s (deficit=%.1fmm, threshold=%.1fmm)",
                zone_name,
                self._SKIP_MESSAGES.get(decision.reason, decision.reason),
                zone._zone_deficit,
                zone._threshold,
            )
            return
        _LOGGER.info(
            "Irrigation triggered by %s: zone='%s', deficit=%.1fmm",
            decision.trigger,
            zone_name,
            zone._zone_deficit,
        )
        self._current_source = str(decision.trigger)
        self._irrigation_task = self._hass.async_create_task(self._irrigate_zones([zone_name]))

    def _make_scheduled_handler(self, zone_name: str):
        """Create a time-triggered handler for a specific zone (Mode B).

        The rule — water at the hour whatever the deficit, skip only a zone with
        nothing to refill — lives in :meth:`Scheduler.evaluate_scheduled`, not
        here. See AI-183 for why the threshold is deliberately not consulted.
        """

        @callback
        def _handler(now) -> None:
            zone = self._zones.get(zone_name)
            if zone is None:
                return
            self._act_on(
                self._scheduler.evaluate_scheduled(zone._zone, is_running=self._running),
                zone_name,
                zone,
            )

        return _handler

    def _make_reactive_handler(self, zone_name: str):
        """Create a deficit-triggered handler for reactive mode (Mode A)."""

        def _handler(dt_h: float, et_h: float, rain: float) -> None:
            zone = self._zones.get(zone_name)
            if zone is None:
                return
            decision = self._scheduler.evaluate_reactive(
                zone._zone,
                is_running=self._running,
                # Asked, not stamped: the throttle is only spent on a call that
                # actually goes ahead, which is what the old ordering achieved
                # by checking it last.
                is_throttled=self._would_throttle("reactive", zone_name),
            )
            if decision.should_irrigate:
                self._record_service_call("reactive", zone_name)
            self._act_on(decision, zone_name, zone)

        return _handler

    # ── Rate limiting ──────────────────────────────────────

    @staticmethod
    def _throttle_key(service_name: str, zone_name: str | None) -> str:
        """Throttling is per service+zone, so two zones do not block each other."""
        return f"{service_name}:{zone_name}" if zone_name else service_name

    def _would_throttle(self, service_name: str, zone_name: str | None = None) -> bool:
        """Whether a call would be rejected right now — asks, records nothing.

        Split from :meth:`_is_throttled` because a predicate that also writes
        makes the *order* of the checks load-bearing: asking "is this throttled"
        before "is something already running" would stamp a call that never
        happened, and throttle the next real one. The scheduler needs the fact,
        not the side effect.
        """
        key = self._throttle_key(service_name, zone_name)
        return (time.monotonic() - self._last_service_call.get(key, 0.0)) < MIN_SERVICE_INTERVAL_S

    def _record_service_call(self, service_name: str, zone_name: str | None = None) -> None:
        """Stamp a call that is actually going ahead."""
        self._last_service_call[self._throttle_key(service_name, zone_name)] = time.monotonic()

    def _is_throttled(self, service_name: str, zone_name: str | None = None) -> bool:
        """Return True if a service call should be rejected (rate limit).

        Ask-and-stamp in one, which is what every service handler wants.
        """
        if self._would_throttle(service_name, zone_name):
            _LOGGER.warning(
                "Service %s throttled — less than %ds since last call",
                self._throttle_key(service_name, zone_name),
                MIN_SERVICE_INTERVAL_S,
            )
            return True
        self._record_service_call(service_name, zone_name)
        return False

    # ── Service handlers ─────────────────────────────────

    async def _handle_reset(self, call: ServiceCall) -> None:
        """Reset reference deficit and all zone deficits to zero."""
        if self._is_throttled("reset"):
            return
        self._dryness.reset()
        self._dryness.async_write_ha_state()
        for zs in self._zones.values():
            zs.reset_deficit("service_reset")
            zs.async_write_ha_state()

    async def _handle_reset_yearly_rain(self, call: ServiceCall) -> None:
        """Clear the year-to-date rain total [mm] — system-wide.

        The total lives on the Dryness Index as a restore attribute and the
        per-zone "Rain Yearly [L]" sensors derive from it, so resetting it
        here re-anchors every zone's yearly-rain display to zero.
        """
        if self._is_throttled("reset_yearly_rain"):
            return
        self._dryness.reset_yearly_rain()
        self._dryness.async_write_ha_state()

    async def _handle_reset_yearly_water(self, call: ServiceCall) -> None:
        """Clear the year-to-date irrigated-water total [L] for every zone.

        The counter is per-zone, so this fans out across all zones of this
        controller; the lifetime total is preserved.
        """
        if self._is_throttled("reset_yearly_water"):
            return
        for zs in self._zones.values():
            zs.reset_yearly_water()
            zs.async_write_ha_state()

    async def _handle_set_deficit(self, call: ServiceCall) -> None:
        """Set deficit to a specific value [mm] — for testing and manual correction."""
        deficit_mm = float(call.data.get(ATTR_DEFICIT_MM, 0.0))
        zone_name = call.data.get(ATTR_ZONE_NAME)
        if zone_name:
            zone = self._zones.get(zone_name)
            if zone is None:
                _LOGGER.error("set_deficit: zone '%s' not found. Available: %s", zone_name, list(self._zones.keys()))
                return
            zone.set_deficit_mm(deficit_mm)
            zone.async_write_ha_state()
        else:
            self._dryness.set_deficit_mm(deficit_mm)
            self._dryness.async_write_ha_state()
            for zs in self._zones.values():
                zs.set_deficit_mm(deficit_mm)
                zs.async_write_ha_state()
        _LOGGER.info("set_deficit: zone=%s deficit=%.2f mm", zone_name or "all", deficit_mm)

    async def _handle_irrigate_zone(self, call: ServiceCall) -> None:
        """Irrigate a single zone by name."""
        zone_name = call.data.get(ATTR_ZONE_NAME)
        if self._is_throttled("irrigate_zone", zone_name):
            return
        if zone_name not in self._zones:
            _LOGGER.error(
                "Zone '%s' not found. Available: %s",
                zone_name,
                list(self._zones.keys()),
            )
            return

        if self._running:
            _LOGGER.warning("Irrigation already in progress, ignoring request")
            return

        self._current_source = "button"
        self._irrigation_task = self._hass.async_create_task(self._irrigate_zones([zone_name]))

    async def _handle_irrigate_all(self, call: ServiceCall) -> None:
        """Irrigate all zones sequentially."""
        if self._is_throttled("irrigate_all"):
            return
        if self._running:
            _LOGGER.warning("Irrigation already in progress, ignoring request")
            return

        self._current_source = "button"
        self._irrigation_task = self._hass.async_create_task(self._irrigate_zones(list(self._zones.keys())))

    async def async_stop(self) -> None:
        """Stop any running irrigation and wait for the task to settle.

        Called during entry unload so the old controller finishes cleanly
        before the new one starts — prevents duplicate deficit updates.

        Sets _stop_requested so the polling loop exits within ~1 s, then
        awaits the task so the finally block can close the valve and settle
        the deficit before the new controller takes over.
        """
        self._stop_requested = True
        task = self._irrigation_task
        if task is not None and not task.done():
            with contextlib.suppress(Exception):
                await task
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        # Settle any manual session still open: the safety monitor dies
        # with this controller, and the successor has no baseline for a
        # valve it never saw open — its close event would be ignored and
        # the delivered water never credited. Close the valve and credit
        # what accrued so far, exactly once. Listeners are already
        # unsubscribed above, so the close cannot re-enter the manual
        # accounting through the state listener.
        for entity_id in list(self._manual_valve_open):
            zone_name = self._valve_to_zone.get(entity_id)
            zone = self._zones.get(zone_name) if zone_name else None
            if zone is None:
                self._manual_valve_open.pop(entity_id, None)
                self._manual_session_meta.pop(entity_id, None)
                mtask = self._manual_safety_tasks.pop(entity_id, None)
                if mtask and not mtask.done():
                    mtask.cancel()
                continue
            if _valve_level(self._hass, entity_id) == "on":
                _LOGGER.info(
                    "Unload with manual session open: zone='%s' — closing valve and settling",
                    zone_name,
                )
                with contextlib.suppress(Exception):
                    await self._close_valve(entity_id)
            self._finalize_manual_session(entity_id, zone_name, zone)

        # Detach the per-valve operators last — the settle loop above may still
        # need one to close a valve through its FSM.
        #
        # Each operator holds HA state listeners on its switch and flow sensor,
        # an absolute watchdog task and the FSM timers, and nothing else ever
        # released them: they were only dropped from hass.data, which frees the
        # reference and not the subscriptions. Every options-flow save reloads
        # the entry, so the count grew with each edit — N operators watching one
        # valve, each with a watchdog able to force it closed under the
        # successor that is legitimately driving it.
        for operator in self._valve_operators.values():
            with contextlib.suppress(Exception):
                operator.async_unload()
        self._valve_operators = {}

    async def _handle_stop(self, call: ServiceCall) -> None:
        """Emergency stop: close every configured valve concurrently."""
        _LOGGER.info("Emergency stop requested")
        self._stop_requested = True

        valves = [zs.valve for zs in self._zones.values() if zs.valve]
        if valves:
            await asyncio.gather(
                *(self._close_valve(v) for v in valves),
                return_exceptions=True,
            )

        self._running = False
        self._active_valve = None

    async def _handle_stop_zone(self, call: ServiceCall) -> None:
        """Stop irrigation for a single zone and close its valve.

        Unlike the global emergency stop this targets one zone, so other
        zones keep irrigating. It works whether the water is being
        delivered by an active controller loop (the loop sees ``_stop_zone``
        on its next poll and finalises the partial delivery) or by a manual
        / hardware-driven open (we close the valve and clear state here).
        """
        zone_name = call.data.get(ATTR_ZONE_NAME)
        zone = self._zones.get(zone_name)
        if zone is None:
            _LOGGER.error("stop_zone: zone '%s' not found", zone_name)
            return
        if not zone.valve:
            _LOGGER.warning("stop_zone: zone '%s' has no valve configured", zone_name)
            return

        _LOGGER.info("Stop requested for zone='%s' — closing valve", zone_name)
        # Signal any active delivery loop for this zone to abort on next poll.
        self._stop_zone = zone_name
        # Cancel a manual safety-close watchdog if one is pending for this valve.
        safety_task = self._manual_safety_tasks.pop(zone.valve, None)
        if safety_task is not None and not safety_task.done():
            safety_task.cancel()
        # Close the valve now (idempotent with the loop's own close).
        await self._close_valve(zone.valve)
        # Clear delivery state and any manual-session tracking for this valve.
        zone.set_irrigating(False)
        zone.async_write_ha_state()
        self._manual_valve_open.pop(zone.valve, None)
        self._manual_session_meta.pop(zone.valve, None)

    async def _handle_mark_irrigated(self, call: ServiceCall) -> None:
        """Mark one or all zones as manually irrigated (reset deficit, no valve)."""
        zone_name = call.data.get(ATTR_ZONE_NAME)
        if self._is_throttled("mark_irrigated", zone_name):
            return
        if zone_name is not None:
            if zone_name not in self._zones:
                _LOGGER.error(
                    "Zone '%s' not found. Available: %s",
                    zone_name,
                    list(self._zones.keys()),
                )
                return
            self._zones[zone_name].reset_deficit("mark_irrigated")
            self._zones[zone_name].async_write_ha_state()
            _LOGGER.info("Zone '%s' marked as irrigated, deficit reset", zone_name)
        else:
            for zs in self._zones.values():
                zs.reset_deficit("mark_irrigated")
                zs.async_write_ha_state()
            _LOGGER.info("All zones marked as irrigated, deficits reset")

    async def _handle_reset_valve(self, call: ServiceCall) -> None:
        """Reset valve FSM from MAINTENANCE to IDLE for a specific zone."""
        zone_name = call.data.get(ATTR_ZONE_NAME)
        zone = self._zones.get(zone_name)
        if zone is None:
            _LOGGER.error("reset_valve: zone '%s' not found", zone_name)
            return
        if not zone.valve:
            _LOGGER.error("reset_valve: zone '%s' has no valve configured", zone_name)
            return
        operator = self._valve_operators.get(zone.valve)
        if operator is None:
            _LOGGER.warning("reset_valve: zone '%s' has no operator (volume_preset mode?)", zone_name)
            return
        await operator.async_reset_maintenance()
        zone.async_write_ha_state()
        _LOGGER.info("Valve maintenance reset: zone='%s'", zone_name)

    async def _handle_test_valve(self, call: ServiceCall) -> None:
        """Run the supervised one-minute test on one zone and publish the result.

        Refuses while anything is irrigating: the point is to attribute what moves
        to this valve and nothing else. The three valve operations are handed to
        ``valve_test`` as callables so that module names no service and no entity
        domain — the adapter stays the only place that knows (GH #94).
        """
        zone_name = call.data.get(ATTR_ZONE_NAME)
        if self._is_throttled("test_valve", zone_name):
            return
        zone = self._zones.get(zone_name)
        if zone is None:
            _LOGGER.error("test_valve: zone '%s' not found. Available: %s", zone_name, list(self._zones))
            return
        if not zone.valve:
            _LOGGER.error("test_valve: zone '%s' has no valve configured", zone_name)
            return
        if self._running:
            _LOGGER.warning("test_valve: irrigation in progress, refusing to test zone '%s'", zone_name)
            return

        minutes = call.data.get("duration_min")
        duration = float(minutes) * 60.0 if minutes else valve_test.DEFAULT_TEST_DURATION_S
        valve = zone.valve
        _LOGGER.info("Zone '%s': supervised valve test starting, %ss", zone_name, duration)

        self._running = True
        self._active_valve = valve
        try:
            result = await valve_test.run_valve_test(
                self._hass,
                zone_name=zone_name,
                valve_entity=valve,
                meter_entity=zone.flow_meter_sensor,
                duration_s=duration,
                open_valve=lambda: _valve_call(self._hass, valve, on=True),
                close_valve=lambda: _valve_call(self._hass, valve, on=False),
                read_level=lambda: _valve_level(self._hass, valve),
            )
        finally:
            self._running = False
            self._active_valve = None

        zone.record_valve_test(result.as_dict())
        self._record_test_as_flow_sample(zone, result)
        self._hass.bus.async_fire(EVENT_VALVE_TEST_COMPLETE, result.as_dict())
        _LOGGER.info(
            "Zone '%s': valve test done — volume=%s L, measured=%s L/h, smallest step=%s, updates=%d%s",
            zone_name,
            result.volume_l,
            result.measured_lph,
            result.smallest_step,
            result.updates,
            "" if not result.notes else " | " + "; ".join(result.notes),
        )

    def _record_test_as_flow_sample(self, zone, result) -> None:
        """File a supervised test where it belongs: as the first historical sample.

        A test measures water that actually came out, so it is a measurement of
        the same kind the sessions produce — not a correction to the design
        figure. Writing it over the configured rate, as an earlier version did,
        conflated two different quantities: what the zone was built to deliver
        and what it delivers today. Kept apart, the pair is a diagnosis; merged,
        both numbers are lost.

        Its practical value is being *first*: a new zone has no session history,
        and a single supervised minute gives the median something to start from.
        """
        lpm = result.measured_lpm
        if not lpm or lpm <= 0:
            return
        if not (UNUSUAL_FLOW_MIN_LPM <= lpm <= UNUSUAL_FLOW_MAX_LPM):
            _LOGGER.warning(
                "Zone '%s': test measured %.2f L/min (%.0f L/h), outside the plausible range"
                " — not recorded. On a coarse counter a short run reads high; re-run it longer",
                zone.zone_name,
                lpm,
                lpm * 60,
            )
            return
        driver = self._valve_operators.get(zone.valve)
        if driver is None or not hasattr(driver, "record_flow_sample"):
            return
        driver.record_flow_sample(lpm)
        # The step the test observed is the meter's limit of detection, and it
        # is what makes the flow-verification window derivable for this zone.
        if result.smallest_step:
            driver.record_meter_resolution(result.smallest_step)
        _LOGGER.info(
            "Zone '%s': supervised test filed as a historical flow sample — %.0f L/h",
            zone.zone_name,
            lpm * 60,
        )

    # ── Core irrigation logic ────────────────────────────

    def _log_session_result(
        self,
        *,
        zone_name: str,
        zone,
        source: str,
        ts_start: datetime,
        ts_end: datetime,
        volume_target_L: float | None,
        volume_delivered_L: float,
        deficit_mm_pre: float,
        deficit_mm_post: float,
    ) -> None:
        # Single-line INFO log marker for post-hoc field-test analysis.
        # Format is intentionally stable: a leading SESSION_RESULT token
        # followed by space-separated key=value pairs. External tools grep
        # for the marker and parse the pairs; do not reorder casually.
        duration_s = max(0.0, (ts_end - ts_start).total_seconds())
        vol_target = "null" if volume_target_L is None else f"{volume_target_L:.1f}"
        _LOGGER.info(
            "SESSION_RESULT zone=%s source=%s delivery_mode=%s "
            "duration_s=%.1f volume_target_L=%s volume_delivered_L=%.1f "
            "deficit_mm_pre=%.2f deficit_mm_post=%.2f "
            "ts_start=%s ts_end=%s",
            zone_name,
            source,
            zone.delivery_mode,
            duration_s,
            vol_target,
            volume_delivered_L,
            deficit_mm_pre,
            deficit_mm_post,
            ts_start.isoformat(),
            ts_end.isoformat(),
        )

    async def _irrigate_zones(self, zone_names: list[str]) -> None:
        """Run irrigation cycle for the given zones sequentially."""
        self._running = True
        self._stop_requested = False
        self._stop_zone = None
        irrigated_zones: list = []

        try:
            for i, zone_name in enumerate(zone_names):
                if self._stop_requested:
                    _LOGGER.info("Irrigation stopped by user after %d zones", i)
                    break

                zone = self._zones[zone_name]

                if not zone.valve:
                    _LOGGER.warning("Zone '%s' has no valve configured, skipping", zone_name)
                    continue

                if zone.volume_liters <= 0:
                    _LOGGER.info(
                        "Zone '%s' needs 0L irrigation — skipping (deficit=%.1fmm, area=%.1fm², efficiency=%.2f)",
                        zone_name,
                        zone._zone_deficit,
                        zone._area,
                        zone._efficiency,
                    )
                    continue

                _LOGGER.info(
                    "Starting irrigation: zone='%s', mode='%s', volume=%.1fL,"
                    " est_duration=%ds, deficit=%.1fmm, timeout=%ds",
                    zone_name,
                    zone.delivery_mode,
                    zone.volume_liters,
                    zone.duration_s,
                    zone._zone_deficit,
                    zone.delivery_timeout,
                )

                volume_target = zone.volume_liters
                # Snapshot the deficit BEFORE delivery starts. Flow-metered
                # delivery modes use this snapshot for real-time deficit
                # updates so that partial progress is preserved on crashes
                # mid-cycle (a network glitch in the flow sensor used to
                # lose every mm we had already delivered).
                deficit_at_start = zone._zone_deficit
                zone.begin_cycle()
                ts_start = datetime.now()
                delivered = await self._deliver_water(zone)
                ts_end = datetime.now()

                # Credit partial delivery BEFORE checking stop so that any
                # water already delivered is always settled in the finally block.
                if delivered > 0:
                    irrigated_zones.append(
                        (zone_name, delivered, volume_target, deficit_at_start, ts_start, ts_end),
                    )
                    self._hass.bus.async_fire(
                        EVENT_IRRIGATION_COMPLETE,
                        {
                            "zone": zone_name,
                            "source": self._current_source or "automatic",
                            "volume_liters": round(delivered, 1),
                            "volume_target": round(volume_target, 1),
                            "deficit_mm": round(zone._zone_deficit, 2),
                        },
                    )
                    _LOGGER.info(
                        "Completed irrigation: zone='%s', delivered=%.1fL of %.1fL target",
                        zone_name,
                        delivered,
                        volume_target,
                    )

                if self._stop_requested:
                    break

                # Inter-zone delay (pressure stabilization)
                if i < len(zone_names) - 1 and not self._stop_requested:
                    _LOGGER.debug("Inter-zone delay: %ds", self._inter_zone_delay)
                    await asyncio.sleep(self._inter_zone_delay)

        except Exception:
            _LOGGER.exception("Error during irrigation cycle")
            # Safety: close all valves on error
            for zs in self._zones.values():
                if zs.valve:
                    await self._close_valve(zs.valve)
                zs.set_irrigating(False)
        finally:
            self._running = False
            self._active_valve = None
            self._stop_zone = None
            # Settle deficits unconditionally — covers normal completion, emergency
            # stop, exceptions, and task cancellation (HA reload mid-cycle).
            try:
                self._settle_irrigated_zones(irrigated_zones)
            except Exception:
                _LOGGER.exception("Error during deficit settle — partial deliveries may not be recorded")
            for zs in self._zones.values():
                zs._deficit_at_irrigation_start = None

    def _settle_irrigated_zones(self, irrigated_zones: list) -> None:
        """Apply deficit adjustments and log results for all zones that received water.

        Idempotent with real-time updates from flow-metered modes: both write
        ``max(0, deficit_at_start - delivered_mm)``.  Called unconditionally
        from the finally block so partial deliveries are always credited.
        """
        if not irrigated_zones:
            return
        all_complete = True
        for zone_name, delivered, target, deficit_at_start, ts_start, ts_end in irrigated_zones:
            zone = self._zones[zone_name]
            if delivered >= target:
                # Full irrigation — reset deficit to zero. Credit the measured
                # ``delivered`` volume because flow-metered modes deplete the
                # deficit in real time, so volume_liters would read ~0 here.
                # The outcome is known — the zone is full — so the deficit goes
                # to exactly zero rather than wherever the arithmetic lands. The
                # measured volume is credited because a flow-metered cycle has
                # already depleted the deficit in real time, so the demand would
                # read ~0 by now.
                zone.reset_deficit(
                    self._current_source or "automatic",
                    delivered_liters=delivered,
                    duration_s=round((ts_end - ts_start).total_seconds()),
                )
            else:
                # Partial irrigation — the zone settles itself. It credits the
                # delivery against its own cycle snapshot (the same value as
                # ``deficit_at_start``, see _irrigate_zones), moves every counter
                # and stamps the session in one call. Writing those seven fields
                # from here is what let the yearly total miss a new year: this
                # branch never rolled it, only the full-delivery one did.
                all_complete = False
                zone.settle_cycle(
                    delivered,
                    source=self._current_source or "automatic",
                    at=datetime.now(),
                    duration_s=round((ts_end - ts_start).total_seconds()),
                )
                _LOGGER.info(
                    "Partial irrigation: zone='%s', delivered=%.1fL/%.1fL, deficit reduced to %.2fmm",
                    zone_name,
                    delivered,
                    target,
                    zone._zone_deficit,
                )
            zone._deficit_at_irrigation_start = None
            zone.async_write_ha_state()
            self._log_session_result(
                zone_name=zone_name,
                zone=zone,
                source=self._current_source or "automatic",
                ts_start=ts_start,
                ts_end=ts_end,
                volume_target_L=target,
                volume_delivered_L=delivered,
                deficit_mm_pre=deficit_at_start,
                deficit_mm_post=zone._zone_deficit,
            )
        # Reset reference sensor only if ALL zones fully irrigated
        zone_names_irrigated = {z[0] for z in irrigated_zones}
        if all_complete and zone_names_irrigated == set(self._zones.keys()):
            self._dryness.reset()
        self._dryness.async_write_ha_state()
        _LOGGER.info(
            "Irrigation cycle complete. %d zone(s) irrigated",
            len(irrigated_zones),
        )

    # ── Delivery mode dispatch ────────────────────────────

    async def _deliver_water(self, zone) -> float:
        """Deliver water to a zone using its configured delivery mode.

        Returns the volume actually delivered in liters (0.0 on failure).
        """
        mode = zone.delivery_mode
        if mode == DELIVERY_MODE_ESTIMATED_FLOW:
            return await self._deliver_estimated_flow(zone)
        if mode == DELIVERY_MODE_FLOW_METER:
            return await self._deliver_flow_meter(zone)
        if mode == DELIVERY_MODE_VOLUME_PRESET:
            return await self._deliver_volume_preset(zone)
        _LOGGER.error("Unknown delivery mode '%s' for zone '%s'", mode, zone.zone_name)
        return 0.0

    def _should_abort(self, zone) -> bool:
        """True if the active delivery loop for ``zone`` should exit early.

        Covers both the global emergency stop and a per-zone stop request
        (``never_dry.stop_zone``) targeting this zone.
        """
        return self._stop_requested or self._stop_zone == zone.zone_name

    def _valve_closed_externally(self, zone) -> bool:
        """True if the zone's valve switch reads 'off' mid-delivery.

        A smart/hardware valve (e.g. Sonoff SWV) can auto-close on its own
        on-time before our software target or ``delivery_timeout`` is
        reached. When that happens the flow drops to zero; continuing to
        poll for the full timeout would pin the zone 'irrigating' for up to
        an hour. A confirmed 'off' is treated as end-of-session.
        """
        return _valve_level(self._hass, zone.valve) == "off"

    def _fallback_volume_estimate(self, zone, elapsed_s: float, measured: float) -> float:
        """Estimate delivered volume when the flow sensor measured nothing.

        A session that ends (delivery timeout, hardware auto-close, stop)
        with the valve open for ``elapsed_s`` but zero measured flow is far
        more likely a dead/stale flow sensor than a dry pipe. Returning the
        raw 0.0 would skip the deficit settle entirely: the deficit would
        survive up to an hour of real watering and the scheduler would
        immediately re-irrigate on the next cycle. Fall back to the zone's
        configured nominal flow rate so the water is still credited.
        """
        if measured > 0 or elapsed_s <= 0:
            return measured
        if zone._flow_rate <= 0:
            _LOGGER.warning(
                "Zone '%s': flow sensor measured 0 L after %.0fs with the valve open"
                " and no flow_rate configured — cannot estimate delivered volume,"
                " deficit will NOT be adjusted",
                zone.zone_name,
                elapsed_s,
            )
            return measured
        estimate = zone._flow_rate * elapsed_s / 60.0
        _LOGGER.warning(
            "Zone '%s': flow sensor measured 0 L after %.0fs with the valve open —"
            " crediting estimated %.1fL from configured flow_rate (%.2f L/min) so"
            " the deficit is settled",
            zone.zone_name,
            elapsed_s,
            estimate,
            zone._flow_rate,
        )
        return estimate

    async def _deliver_estimated_flow(self, zone) -> float:
        """Open the valve, wait the calculated duration, close it.

        The duration is the dose here: the run always stops on the clock. What
        the *meter* decides, when the zone has one, is how much water is then
        credited — and the two are separate questions that used to be answered
        by the same setting.

        Crediting the planned volume back was tautological: it returned the
        question as its own answer, so the deficit settled to zero whatever came
        out of the ground. On a zone delivering 57% of its design rate — the gap
        measured on this installation — the model believed the debt was paid
        while more than a third of the water had never arrived. It also kept the
        error alive, because without a measurement the flow rate can never be
        learned, and the unlearned rate produces the same wrong volume next time.

        So: if a meter is configured, the delta across the run is the answer;
        otherwise the estimate stands, and a zone without a meter behaves
        exactly as before.
        """
        duration = zone.duration_s
        if duration <= 0:
            return 0.0

        meter = zone.flow_meter_sensor
        baseline = self._read_volume_liters(meter) if meter else None

        if not await self._open_valve(zone.valve):
            return 0.0
        zone.set_irrigating(True)
        zone.async_write_ha_state()

        session_start = time.monotonic()
        elapsed = await self._wait_with_stop_check(duration, valve_entity=zone.valve, zone=zone)
        session_s = time.monotonic() - session_start

        await self._close_valve(zone.valve)
        zone.set_irrigating(False)
        zone.async_write_ha_state()

        # Proportional to elapsed time, so an emergency stop still credits what
        # ran. This is the fallback, not the answer, whenever a meter exists.
        estimated = zone.volume_liters * elapsed / duration
        if baseline is None:
            return estimated

        # The run also becomes a flow-rate sample, which is what eventually makes
        # the estimate above unnecessary. Deferred inside the driver so the late
        # ticks land first; the figure returned here cannot wait for them, and may
        # therefore understate the volume by at most one counter step.
        self._schedule_flow_sample(zone, meter, baseline, session_s)

        current = self._read_volume_liters(meter)
        if current is None:
            _LOGGER.warning(
                "Zone '%s': meter '%s' unreadable at close — crediting the estimate (%.1fL)",
                zone.zone_name,
                meter,
                estimated,
            )
            return estimated
        measured = current - baseline
        if measured <= 0:
            # Either nothing moved or the counter reset. A zone that ran with the
            # valve open and measured nothing is far more likely a stalled meter
            # than a dry pipe, so the estimate keeps the deficit settling.
            _LOGGER.warning(
                "Zone '%s': meter moved %.1fL over %.0fs with the valve open — crediting the estimate (%.1fL)",
                zone.zone_name,
                measured,
                session_s,
                estimated,
            )
            return estimated

        _LOGGER.info(
            "Zone '%s': delivered %.1fL measured by the meter (the time-based estimate was %.1fL)",
            zone.zone_name,
            measured,
            estimated,
        )
        return measured

    async def _deliver_volume_preset(self, zone) -> float:
        """Arm the smart-valve dose, ensure it opens, wait for self-close.

        Sequence:
          1. ``number.set_value`` arms the volume target on the smart valve.
          2. Wait ``AUTO_OPEN_GRACE_S`` to see if the valve auto-opens.
          3. If still closed after the grace window, send the open command
             (idempotent if the valve has just auto-opened in the gap).
          4. Poll for self-close (existing behaviour); on stop or timeout
             we force the close command.

        Note: this delivery mode bypasses the driver on purpose.
        Smart valves with auto-close behaviour drive their own state and
        do not fit the operator's "I command, you obey" semantics.
        """
        volume = zone.volume_liters
        if volume <= 0:
            return 0.0

        volume_entity = zone.volume_entity
        if not volume_entity:
            _LOGGER.error("Zone '%s' has no volume_entity configured", zone.zone_name)
            return 0.0

        # Pre-check the switch entity. volume_preset bypasses the driver,
        # so it also bypasses the operator's pre-check. Do our own here so
        # the user gets the same "unreachable at irrigation time"
        # notification when the smart valve is offline.
        switch_state = self._hass.states.get(zone.valve)
        if switch_state is None or switch_state.state in ("unavailable", "unknown"):
            reason = "switch_entity_not_found" if switch_state is None else "switch_unavailable"
            _LOGGER.error(
                "Zone '%s' valve '%s' %s, skipping volume_preset",
                zone.zone_name,
                zone.valve,
                reason,
            )
            await self._notify_unreachable_at_irrigation(zone.valve, reason)
            return 0.0

        # 1) Arm the dose
        await self._hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": volume_entity, "value": round(volume, 1)},
        )
        zone.set_irrigating(True)
        zone.async_write_ha_state()

        # 2-3) Grace window for auto-open; explicit turn_on as fallback
        grace_s = self.auto_open_grace_s
        auto_opened = await self._wait_for_auto_open(zone.valve, grace_s)
        if not auto_opened:
            _LOGGER.info(
                "Zone '%s': smart valve did not auto-open within %.1fs, sending the open command",
                zone.zone_name,
                grace_s,
            )
            await _valve_call(self._hass, zone.valve, on=True)

        # 4) Wait for the smart valve to finish (monitor switch state)
        timeout = zone.delivery_timeout
        elapsed = 0
        while elapsed < timeout:
            if self._should_abort(zone):
                await _valve_call(self._hass, zone.valve, on=False)
                zone.set_irrigating(False)
                zone.async_write_ha_state()
                # Credit the water dispensed before the stop — the smart valve
                # was dosing at its nominal rate, capped at the armed volume.
                return min(volume, self._fallback_volume_estimate(zone, elapsed, 0.0))
            await asyncio.sleep(FLOW_METER_POLL_INTERVAL_S)
            elapsed += FLOW_METER_POLL_INTERVAL_S

            # Check if the valve switch has turned off (valve closed itself)
            if _valve_level(self._hass, zone.valve) == "off":
                break
        else:
            _LOGGER.warning(
                "Zone '%s' volume_preset timeout (%ds). Forcing valve close.",
                zone.zone_name,
                timeout,
            )
            await _valve_call(self._hass, zone.valve, on=False)

        zone.set_irrigating(False)
        zone.async_write_ha_state()
        # Smart valve reports it completed, assume full delivery
        return volume

    async def _deliver_flow_meter(self, zone) -> float:
        """Open valve, monitor flow sensor, close when target volume reached.

        Supports two sensor types:
        - Cumulative volume (L): reads difference between start and current
        - Flow rate (L/h, L/min, m³/h): integrates rate over time

        Returns volume actually delivered in liters.
        """
        volume_target = zone.volume_liters
        if volume_target <= 0:
            return 0.0

        meter_entity = zone.flow_meter_sensor
        if not meter_entity:
            _LOGGER.error("Zone '%s' has no flow_meter_sensor configured", zone.zone_name)
            return 0.0

        # Detect sensor type from unit of measurement
        is_rate_sensor = self._is_flow_rate_sensor(meter_entity)

        if is_rate_sensor:
            # Flow rate mode: accumulate volume from rate readings
            return await self._deliver_flow_rate(zone, meter_entity, volume_target)

        # Cumulative volume mode: read difference (normalized to liters)
        initial_reading = self._read_volume_liters(meter_entity)
        if initial_reading is None:
            _LOGGER.error(
                "Flow meter '%s' unavailable for zone '%s', skipping",
                meter_entity,
                zone.zone_name,
            )
            return 0.0

        if not await self._open_valve(zone.valve):
            return 0.0
        zone.set_irrigating(True)
        zone.async_write_ha_state()

        # Kept apart from ``initial_reading``, which the loop rebases on a meter
        # reset: a rebased baseline would file the reset as delivered water.
        flow_baseline = initial_reading
        last_reading = initial_reading
        session_start = time.monotonic()

        timeout = zone.delivery_timeout
        elapsed = 0
        delivered = 0.0
        while elapsed < timeout:
            if self._should_abort(zone):
                await self._close_valve(zone.valve)
                zone.set_irrigating(False)
                zone.async_write_ha_state()
                return self._fallback_volume_estimate(zone, elapsed, delivered)

            await asyncio.sleep(FLOW_METER_POLL_INTERVAL_S)
            elapsed += FLOW_METER_POLL_INTERVAL_S

            # Valve closed itself (e.g. hardware auto-close) — end the session
            # instead of polling a dead flow meter for the full timeout.
            if self._valve_closed_externally(zone):
                _LOGGER.info(
                    "Zone '%s' valve closed externally after %ds (hardware auto-close?) —"
                    " ending session, delivered %.1fL of %.1fL",
                    zone.zone_name,
                    elapsed,
                    delivered,
                    volume_target,
                )
                break

            current_reading = self._read_volume_liters(meter_entity)
            if current_reading is None:
                _LOGGER.warning(
                    "Flow meter '%s' became unavailable during irrigation of zone '%s'",
                    meter_entity,
                    zone.zone_name,
                )
                continue

            if current_reading > last_reading:
                # Every delivery teaches the meter's step for free; the smallest
                # increment ever seen is its limit of detection, and that is what
                # makes the flow-verification window derivable (GH #173).
                self._observe_meter_step(zone, current_reading - last_reading)
            last_reading = current_reading

            delivered = current_reading - initial_reading
            if delivered < 0:
                _LOGGER.warning("Flow meter reset detected, adjusting baseline")
                initial_reading = 0.0
                delivered = current_reading

            # Real-time deficit update (snapshot-based — idempotent with settle).
            self._update_deficit_realtime(zone, delivered)

            if delivered >= volume_target:
                break
        else:
            _LOGGER.warning(
                "Zone '%s' flow_meter timeout (%ds). Delivered %.1fL of %.1fL target. Closing valve.",
                zone.zone_name,
                timeout,
                delivered,
                volume_target,
            )

        session_s = time.monotonic() - session_start
        await self._close_valve(zone.valve)
        zone.set_irrigating(False)
        zone.async_write_ha_state()
        self._schedule_flow_sample(zone, meter_entity, flow_baseline, session_s)
        return self._fallback_volume_estimate(zone, elapsed, delivered)

    def _driver_for(self, zone):
        """The driver that owns this zone's valve, if the zone has one."""
        return self._valve_operators.get(zone.valve) if zone.valve else None

    def _observe_meter_step(self, zone, step: float) -> None:
        """Teach the zone's driver the smallest counter increment seen."""
        driver = self._driver_for(zone)
        if driver is not None and hasattr(driver, "record_meter_resolution"):
            driver.record_meter_resolution(step)

    def _schedule_flow_sample(self, zone, meter_entity: str, baseline: float | None, session_s: float) -> None:
        """Ask the driver to file this session's real flow rate, once it settles.

        Deferred inside the driver by design: the meter's last tick routinely
        lands after the valve is already shut, so the reading is taken a little
        later. The delivery has to return now regardless.
        """
        driver = self._driver_for(zone)
        if driver is None or baseline is None or not hasattr(driver, "schedule_flow_sample"):
            return
        driver.schedule_flow_sample(meter_entity, baseline, session_s)

    async def _deliver_flow_rate(
        self,
        zone,
        meter_entity: str,
        volume_target: float,
    ) -> float:
        """Deliver water by integrating a flow rate sensor over time.

        Returns volume actually delivered in liters.
        """
        if not await self._open_valve(zone.valve):
            return 0.0
        zone.set_irrigating(True)
        zone.async_write_ha_state()

        delivered = 0.0
        timeout = zone.delivery_timeout
        elapsed = 0

        _LOGGER.info(
            "Zone '%s' using flow rate sensor '%s', target=%.1fL",
            zone.zone_name,
            meter_entity,
            volume_target,
        )

        while elapsed < timeout:
            if self._should_abort(zone):
                await self._close_valve(zone.valve)
                zone.set_irrigating(False)
                zone.async_write_ha_state()
                return self._fallback_volume_estimate(zone, elapsed, delivered)

            await asyncio.sleep(FLOW_METER_POLL_INTERVAL_S)
            elapsed += FLOW_METER_POLL_INTERVAL_S

            # Valve closed itself (e.g. hardware auto-close) — end the session
            # instead of integrating a dead flow sensor for the full timeout.
            if self._valve_closed_externally(zone):
                _LOGGER.info(
                    "Zone '%s' valve closed externally after %ds (hardware auto-close?) —"
                    " ending session, delivered %.1fL of %.1fL",
                    zone.zone_name,
                    elapsed,
                    delivered,
                    volume_target,
                )
                break

            rate = self._read_flow_meter(meter_entity)
            if rate is None or rate < 0:
                continue

            # Normalize rate to L/min (handles metric + imperial), then
            # integrate over the poll interval.
            unit = self._get_flow_meter_unit(meter_entity)
            lpm = self._rate_to_lpm(rate, unit)
            delivered += lpm / 60 * FLOW_METER_POLL_INTERVAL_S

            # Real-time deficit update (snapshot-based — idempotent with settle).
            self._update_deficit_realtime(zone, delivered)

            if delivered >= volume_target:
                _LOGGER.info(
                    "Zone '%s' target reached: delivered=%.1fL",
                    zone.zone_name,
                    delivered,
                )
                break
        else:
            _LOGGER.warning(
                "Zone '%s' flow_rate timeout (%ds). Delivered %.1fL of %.1fL target. Closing valve.",
                zone.zone_name,
                timeout,
                delivered,
                volume_target,
            )

        await self._close_valve(zone.valve)
        zone.set_irrigating(False)
        zone.async_write_ha_state()
        return self._fallback_volume_estimate(zone, elapsed, delivered)

    # Thin wrappers around the shared helpers in ``flow_utils`` — kept as
    # methods so existing call sites and tests keep working unchanged.

    def _is_flow_rate_sensor(self, entity_id: str) -> bool:
        """Check if the sensor reports a flow rate (not cumulative volume)."""
        return flow_utils.is_flow_rate_sensor(self._hass, entity_id)

    def _get_flow_meter_unit(self, entity_id: str) -> str | None:
        """Get the unit of measurement of a flow meter sensor."""
        return flow_utils.get_flow_meter_unit(self._hass, entity_id)

    def _read_flow_meter(self, entity_id: str) -> float | None:
        """Read the current value of a flow meter sensor."""
        return flow_utils.read_flow_meter(self._hass, entity_id)

    def _read_volume_liters(self, entity_id: str) -> float | None:
        """Read a cumulative-volume sensor and normalize to liters in one fetch."""
        return flow_utils.read_volume_liters(self._hass, entity_id)

    @staticmethod
    def _rate_to_lpm(rate: float, unit: str | None) -> float:
        """Normalize a flow-rate reading to liters per minute."""
        return flow_utils.rate_to_lpm(rate, unit)

    @staticmethod
    def _volume_to_liters(value: float, unit: str | None) -> float:
        """Normalize a cumulative-volume reading to liters."""
        return flow_utils.volume_to_liters(value, unit)

    # ── Deficit live-update helper ────────────────────────

    def _update_deficit_realtime(self, zone, delivered_liters: float) -> None:
        """Apply a snapshot-based incremental deficit update.

        Writes ``zone._zone_deficit = max(0, deficit_at_start -
        delivered_liters * efficiency / area)`` and pushes the new state
        to Home Assistant. Skipped when ``_deficit_at_irrigation_start``
        is ``None`` (no active cycle) or the zone has no area.

        Idempotency: every call writes an absolute value derived from the
        snapshot, so multiple intermediate calls plus the end-of-cycle
        settle never double-count.
        """
        snapshot = zone._deficit_at_irrigation_start
        if snapshot is None or zone._area <= 0:
            return
        zone.credit_delivery(delivered_liters)
        # Session water must rise live during a flow-metered cycle, not only at
        # completion — otherwise the card shows Volume/Duration counting down
        # while Session water stays 0 (field report, flow_meter zone).
        zone._session_water_delivered = round(delivered_liters, 1)
        zone.async_write_ha_state()
        zone.notify_session_listeners()

    # ── Valve helpers ─────────────────────────────────────

    async def _notify_unreachable_at_irrigation(self, entity_id: str, reason: str) -> None:
        """Surface a UNREACHABLE_AT_IRRIGATION notification.

        Fired when the user (or scheduler) asked the integration to open a
        valve but the pre-check failed: the switch entity is missing,
        ``unavailable`` or ``unknown``. The notifier dedups on
        ``(zone, kind, context)`` so repeated presses do not stack
        identical notifications.
        """
        if self._notifier is None:
            return
        zone_name = self._valve_to_zone.get(entity_id, entity_id)
        await self._notifier.notify(
            zone_name,
            NotificationKind.UNREACHABLE_AT_IRRIGATION,
            Severity.WARNING,
            context={"entity_id": entity_id, "reason": reason},
        )

    async def _wait_with_stop_check(self, duration_s: int, valve_entity: str | None = None, zone=None) -> int:
        """Wait for duration, checking for stop requests every second.

        Returns the number of seconds actually elapsed, which may be less
        than ``duration_s`` when a stop is requested (global or per-zone) or
        the valve is closed externally.
        """
        for elapsed in range(duration_s):
            if self._stop_requested or (zone is not None and self._stop_zone == zone.zone_name):
                return elapsed
            if valve_entity is not None and _valve_level(self._hass, valve_entity) == "off":
                _LOGGER.info(
                    "Valve '%s' closed externally after %ds — aborting estimated_flow wait",
                    valve_entity,
                    elapsed,
                )
                return elapsed
            await asyncio.sleep(1)
        return duration_s

    async def _open_valve(self, entity_id: str) -> bool:
        """Open a valve switch. Returns True on success, False on failure.

        Uses the :class:`~.driver.Driver` when one is registered for the
        entity; otherwise falls back to a direct open command
        (used for valves without an operator, including the test harness).
        """
        self._active_valve = entity_id
        _LOGGER.info("Attempting valve open: '%s'", entity_id)
        # Announce the attempt before it can fail. Opening a valve is not
        # instantaneous and does not always succeed: a sleeping Zigbee valve
        # spends the better part of a minute in retries, and until now the card
        # said nothing for that whole time — indistinguishable from a button
        # that ignored the press.
        zone = self._zone_for_valve(entity_id)
        if zone is not None:
            zone.set_awaiting_valve(True)
            zone.async_write_ha_state()
        try:
            return await self._open_valve_inner(entity_id)
        finally:
            if zone is not None:
                zone.set_awaiting_valve(False)
                zone.async_write_ha_state()

    def _zone_for_valve(self, entity_id: str):
        """The zone driving this valve entity, if any."""
        return next((z for z in self._zones.values() if z.valve == entity_id), None)

    async def _open_valve_inner(self, entity_id: str) -> bool:
        """Issue the open command and report whether the valve confirmed."""
        operator = self._valve_operators.get(entity_id)
        if operator is None:
            await _valve_call(self._hass, entity_id, on=True)
            return True
        result = await operator.async_turn_on()
        if result.status != OperationStatus.OK:
            _LOGGER.error(
                "Valve open failed for '%s': status=%s detail=%s",
                entity_id,
                result.status.value,
                result.error_detail,
            )
            if result.status == OperationStatus.PRECHECK_FAILED:
                await self._notify_unreachable_at_irrigation(entity_id, result.error_detail or "unavailable")
            return False
        return True

    async def _close_valve(self, entity_id: str) -> bool:
        """Close a valve switch. Returns True on success, False on failure."""
        operator = self._valve_operators.get(entity_id)
        if operator is None:
            await _valve_call(self._hass, entity_id, on=False)
            if self._active_valve == entity_id:
                self._active_valve = None
            return True
        self._controller_closing.add(entity_id)
        try:
            result = await operator.async_turn_off()
        finally:
            self._controller_closing.discard(entity_id)
        if self._active_valve == entity_id:
            self._active_valve = None
        if result.status != OperationStatus.OK:
            _LOGGER.error(
                "Valve close failed for '%s': status=%s detail=%s",
                entity_id,
                result.status.value,
                result.error_detail,
            )
            return False
        return True

    async def _wait_for_auto_open(self, entity_id: str, grace_s: float) -> bool:
        """Poll the valve state for up to ``grace_s`` seconds, return True on auto-open.

        Used by ``volume_preset`` to detect smart-valves that open
        themselves after receiving the volume target. If the grace
        window elapses without the switch reporting ``"on"``, the caller
        is expected to send the open command explicitly.
        """
        waited = 0.0
        step = min(0.5, max(0.01, grace_s / 6))
        while waited < grace_s:
            await asyncio.sleep(step)
            waited += step
            if _valve_level(self._hass, entity_id) == "on":
                return True
        return False

    # ── Manual valve detection ───────────────────────────

    @callback
    def _on_valve_state_change(self, event) -> None:
        """Detect manual valve operation (not initiated by controller).

        When an operator is registered for the valve we trust its FSM
        state: anything other than ``IDLE`` means the controller is
        actively driving the valve. Otherwise we fall back to the legacy
        global ``_running`` flag for valves without an operator.
        """
        entity_id = event.data.get("entity_id")
        if self._running and self._active_valve == entity_id:
            # Commanded delivery in progress for THIS valve: any state
            # change belongs to that session and its loop settles it.
            # Do NOT trust the operator FSM here — on a hardware
            # self-close (e.g. Sonoff ZFE on-device dose) the operator
            # processes the 'off' event first and is already back to
            # IDLE by the time this listener runs, which used to make
            # the close look manual and fully reset the deficit while
            # the delivery loop was about to credit the partial
            # (field bug, 2026-07-15: 640 s delivered of 1055 s planned,
            # deficit zeroed instead of keeping the remaining third).
            return
        operator = self._valve_operators.get(entity_id) if entity_id else None
        if operator is not None:
            if operator.state != ValveState.IDLE:
                return  # operator is driving this valve
        elif self._running:
            return  # legacy gate: controller is driving some valve

        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")

        if new_state is None or old_state is None:
            return
        if entity_id not in self._valve_to_zone:
            return

        zone_name = self._valve_to_zone[entity_id]
        zone = self._zones.get(zone_name)
        if zone is None:
            return

        _level = ValveCommandAdapter.interpret_state
        if _level(old_state.state) == "off" and _level(new_state.state) == "on":
            # Valve opened manually — record baseline
            if zone.flow_meter_sensor:
                is_rate = self._is_flow_rate_sensor(zone.flow_meter_sensor)
                if is_rate:
                    # For rate sensors, record open timestamp
                    self._manual_valve_open[entity_id] = time.monotonic()
                else:
                    # For cumulative sensors, record current reading (in liters)
                    self._manual_valve_open[entity_id] = self._read_volume_liters(zone.flow_meter_sensor)
            else:
                self._manual_valve_open[entity_id] = None
            # Snapshot for the SESSION_RESULT log emitted at manual close.
            self._manual_session_meta[entity_id] = (datetime.now(), zone._zone_deficit)
            # Reflect "currently irrigating" in zone state so UI/automations
            # see the same flag they would during a commanded cycle.
            zone.set_irrigating(True)
            zone.async_write_ha_state()
            # Active monitor: closes the valve at the minimum between
            # volume-needed (if we can measure or estimate it) and the
            # safety delivery_timeout.
            self._manual_safety_tasks[entity_id] = self._hass.async_create_task(
                self._external_session_monitor(entity_id, zone_name),
            )
            _LOGGER.info(
                "Manual valve open detected: zone='%s' (target=%.1fL, timeout=%ds)",
                zone_name,
                zone.volume_liters,
                zone.delivery_timeout,
            )

        elif _level(old_state.state) == "on" and _level(new_state.state) == "off":
            if entity_id in self._controller_closing:
                return  # NeverDry-initiated close — not a manual event
            if entity_id not in self._manual_valve_open:
                # No manual session was ever tracked for this valve: the
                # close belongs to a commanded delivery (its loop settles
                # the session — even in the window where an emergency stop
                # has already cleared _running/_active_valve) or it is a
                # stray event. Treating it as manual used to fully reset
                # the deficit (field bug, 2026-07-15).
                return
            self._finalize_manual_session(entity_id, zone_name, zone)

    def _finalize_manual_session(self, entity_id: str, zone_name: str, zone) -> None:
        """Account a tracked manual session and settle the zone exactly once.

        Cancels the safety monitor, credits the delivered water — measured
        by the flow meter when possible, estimated from flow_rate x elapsed
        otherwise — and writes the session history. A close NEVER fully
        resets the deficit: that is mark_irrigated's exclusive semantics.

        Called from the valve state listener on the manual on→off
        transition, and from :meth:`async_stop` for sessions still open at
        entry unload — the monitor dies with this controller and the
        successor has no baseline for a valve it never saw open, so the
        water must be credited here or never.
        """
        task = self._manual_safety_tasks.pop(entity_id, None)
        if task and not task.done():
            task.cancel()
        baseline = self._manual_valve_open.pop(entity_id)
        zone.set_irrigating(False)

        session_meta = self._manual_session_meta.pop(entity_id, None)
        ts_end = datetime.now()
        elapsed_s = (ts_end - session_meta[0]).total_seconds() if session_meta else 0.0

        delivered_liters = 0.0
        if zone.flow_meter_sensor and baseline is not None:
            is_rate = self._is_flow_rate_sensor(zone.flow_meter_sensor)
            if is_rate:
                # Estimate volume from average flow rate x duration
                duration_s = time.monotonic() - baseline
                current_rate = self._read_flow_meter(zone.flow_meter_sensor)
                if current_rate is not None and current_rate > 0:
                    unit = self._get_flow_meter_unit(zone.flow_meter_sensor)
                    lpm = self._rate_to_lpm(current_rate, unit)
                    delivered_liters = lpm / 60 * duration_s
            else:
                # Cumulative: simple difference (baseline already in liters)
                flow_end = self._read_volume_liters(zone.flow_meter_sensor)
                delivered_liters = max(0.0, flow_end - baseline) if flow_end is not None else 0.0
            # Meter measured nothing → credit the guard-flow estimate.
            delivered_liters = self._fallback_volume_estimate(zone, elapsed_s, delivered_liters)
        elif zone._flow_rate > 0 and elapsed_s > 0:
            delivered_liters = zone._flow_rate * elapsed_s / 60.0
            _LOGGER.info(
                "Manual irrigation estimated: zone='%s', %.1fL from flow_rate %.2f L/min x %.0fs",
                zone_name,
                delivered_liters,
                zone._flow_rate,
                elapsed_s,
            )
        else:
            _LOGGER.warning(
                "Manual irrigation on zone '%s' cannot be quantified (no flow meter,"
                " no flow_rate) — deficit left unchanged; use mark_irrigated if the"
                " zone was fully watered",
                zone_name,
            )

        if delivered_liters > 0 and zone._area > 0:
            # Same call as the commanded path: no cycle was opened here, so the
            # zone credits against its current deficit rather than a snapshot —
            # that distinction lives inside credit_delivery, which is the whole
            # reason the arithmetic belongs to the zone and not to two callers
            # that had drifted apart.
            zone.settle_cycle(delivered_liters, source="manual", at=ts_end, duration_s=round(elapsed_s))
            _LOGGER.info(
                "Manual irrigation accounted: zone='%s', delivered=%.1fL, new deficit=%.2fmm",
                zone_name,
                delivered_liters,
                zone._zone_deficit,
            )

        zone.async_write_ha_state()
        self._hass.bus.async_fire(
            EVENT_IRRIGATION_COMPLETE,
            {
                "zone": zone_name,
                "source": "manual",
                "deficit_mm": round(zone._zone_deficit, 2),
            },
        )
        if session_meta is not None:
            ts_start, deficit_pre = session_meta
            # settle_cycle already wrote this when there was water to credit;
            # this covers the session that delivered nothing measurable and so
            # never reached it, and still has a duration worth reporting.
            zone._last_session_duration_s = round(elapsed_s)
            self._log_session_result(
                zone_name=zone_name,
                zone=zone,
                source="manual",
                ts_start=ts_start,
                ts_end=ts_end,
                volume_target_L=None,
                volume_delivered_L=delivered_liters,
                deficit_mm_pre=deficit_pre,
                deficit_mm_post=zone._zone_deficit,
            )

    async def _external_session_monitor(self, entity_id: str, zone_name: str) -> None:
        """Auto-close a manually-opened valve at min(volume_needed, timeout).

        Started when the user opens the valve from the physical button on
        the device, the Zigbee app, or the HA switch UI (i.e. anything
        outside NeverDry's commanded path). Three exit conditions:

        1. **Volume target reached** — if the zone has a flow meter we
           poll it and close as soon as the delivered amount covers the
           current ``volume_liters`` (deficit-driven target).
        2. **Estimated duration elapsed** — without a flow meter but
           with a configured ``flow_rate``, we sleep for
           ``volume_liters / flow_rate`` and close.
        3. **Safety timeout** — ``delivery_timeout`` is always honoured
           as the upper bound, so a forgotten-open valve cannot run
           indefinitely.

        The final close command triggers the OFF transition that
        :meth:`_on_valve_state_change` uses to finalise the session
        (deficit, ``last_irrigated``, ``is_irrigating``). If the user
        closes the valve first, the OFF transition cancels this task
        before it gets a chance to send the service call.
        """
        zone = self._zones.get(zone_name)
        if zone is None:
            return
        timeout_s = max(1, zone.delivery_timeout)
        volume_target = zone.volume_liters

        try:
            if volume_target > 0 and zone.flow_meter_sensor:
                await self._monitor_via_flow_meter(
                    entity_id,
                    zone_name,
                    zone,
                    volume_target,
                    timeout_s,
                )
            elif volume_target > 0 and zone._flow_rate > 0:
                # Estimated duration: volume / flow_rate (L/min) → seconds
                estimated_s = int(volume_target / zone._flow_rate * 60)
                wait_s = min(estimated_s, timeout_s)
                _LOGGER.info(
                    "Manual valve '%s': estimated %ds for %.1fL (timeout=%ds, waiting %ds)",
                    zone_name,
                    estimated_s,
                    volume_target,
                    timeout_s,
                    wait_s,
                )
                await asyncio.sleep(wait_s)
            else:
                # No way to measure or estimate — fall back to safety
                # timeout only. The valve will be force-closed when the
                # timeout expires.
                _LOGGER.info(
                    "Manual valve '%s': no measurable target, safety timeout=%ds",
                    zone_name,
                    timeout_s,
                )
                await asyncio.sleep(timeout_s)
        except asyncio.CancelledError:
            return

        state = self._hass.states.get(entity_id)
        if state is None or state.state != "on":
            return
        _LOGGER.info(
            "Manual valve '%s' auto-close: target reached or timeout — sending the close command",
            zone_name,
        )
        await _valve_call(self._hass, entity_id, on=False)

    async def _monitor_via_flow_meter(
        self,
        entity_id: str,
        zone_name: str,
        zone,
        volume_target: float,
        timeout_s: int,
    ) -> None:
        """Poll the flow meter until ``volume_target`` is delivered or timeout.

        Supports both cumulative-volume sensors (L) and rate sensors
        (L/h, L/min, m³/h) — mirroring the logic used by the commanded
        delivery paths in :meth:`_deliver_flow_meter` /
        :meth:`_deliver_flow_rate`.
        """
        meter = zone.flow_meter_sensor
        is_rate = self._is_flow_rate_sensor(meter)
        elapsed = 0
        delivered = 0.0

        if is_rate:
            unit = self._get_flow_meter_unit(meter)
            while elapsed < timeout_s:
                await asyncio.sleep(FLOW_METER_POLL_INTERVAL_S)
                elapsed += FLOW_METER_POLL_INTERVAL_S
                rate = self._read_flow_meter(meter)
                if rate is None or rate < 0:
                    continue
                lpm = self._rate_to_lpm(rate, unit)
                delivered += lpm / 60 * FLOW_METER_POLL_INTERVAL_S
                if delivered >= volume_target:
                    _LOGGER.info(
                        "Manual valve '%s' reached target (%.1fL of %.1fL) via flow rate",
                        zone_name,
                        delivered,
                        volume_target,
                    )
                    return
            _LOGGER.warning(
                "Manual valve '%s' rate-monitor timeout (%ds): %.1fL of %.1fL target",
                zone_name,
                timeout_s,
                delivered,
                volume_target,
            )
            return

        initial = self._read_volume_liters(meter)
        if initial is None:
            _LOGGER.warning(
                "Manual valve '%s': flow meter unavailable at open, falling back to timeout=%ds",
                zone_name,
                timeout_s,
            )
            await asyncio.sleep(timeout_s)
            return
        while elapsed < timeout_s:
            await asyncio.sleep(FLOW_METER_POLL_INTERVAL_S)
            elapsed += FLOW_METER_POLL_INTERVAL_S
            current = self._read_volume_liters(meter)
            if current is None:
                continue
            delivered = current - initial
            if delivered < 0:
                initial = 0.0
                delivered = current
            if delivered >= volume_target:
                _LOGGER.info(
                    "Manual valve '%s' reached target (%.1fL of %.1fL) via cumulative meter",
                    zone_name,
                    delivered,
                    volume_target,
                )
                return
        _LOGGER.warning(
            "Manual valve '%s' meter-monitor timeout (%ds): %.1fL of %.1fL target",
            zone_name,
            timeout_s,
            delivered,
            volume_target,
        )

    # ── Battery monitoring ────────────────────────────────

    @callback
    def _on_battery_change(self, event) -> None:
        """Alert when a valve battery drops below threshold."""
        new_state = event.data.get("new_state")
        if new_state is None:
            return

        entity_id = event.data.get("entity_id")
        if entity_id not in self._battery_to_zone:
            return

        try:
            level = float(new_state.state)
        except (ValueError, TypeError):
            return

        zone_name = self._battery_to_zone[entity_id]

        if level <= DEFAULT_BATTERY_LOW_THRESHOLD:
            if zone_name not in self._battery_alerted:
                self._battery_alerted.add(zone_name)
                self._hass.async_create_task(
                    self._hass.services.async_call(
                        "persistent_notification",
                        "create",
                        {
                            "title": "Low battery — irrigation valve",
                            "message": (
                                f"Zone **{zone_name}**: valve battery at {level:.0f}%. "
                                f"Replace batteries soon to avoid irrigation failures."
                            ),
                            "notification_id": f"{DOMAIN}_battery_{zone_name}",
                        },
                    )
                )
                _LOGGER.warning(
                    "Low battery alert: zone='%s', level=%.0f%%",
                    zone_name,
                    level,
                )
        else:
            # Battery recovered (e.g. replaced) — reset alert
            self._battery_alerted.discard(zone_name)

    # ── Deficit anomaly detection ─────────────────────────

    async def _check_deficit_anomaly(self, now=None) -> None:
        """Alert when a zone's deficit is anomalously high (possible malfunction).

        Called every 6 hours in all modes. Alerts once per zone until
        the deficit drops back below the anomaly threshold.
        """
        for zs in self._zones.values():
            threshold = zs.extra_state_attributes.get("threshold_mm", DEFAULT_THRESHOLD)
            anomaly_limit = threshold * ANOMALY_DEFICIT_MULTIPLIER
            zone_deficit = zs._zone_deficit

            if zone_deficit >= anomaly_limit:
                if zs.zone_name not in self._deficit_anomaly_alerted:
                    self._deficit_anomaly_alerted.add(zs.zone_name)
                    await self._hass.services.async_call(
                        "persistent_notification",
                        "create",
                        {
                            "title": "Anomalous deficit — possible malfunction",
                            "message": (
                                f"Zone **{zs.zone_name}**: deficit {zone_deficit:.1f} mm "
                                f"exceeds {anomaly_limit:.0f} mm "
                                f"({ANOMALY_DEFICIT_MULTIPLIER}\u00d7 threshold). "
                                f"Irrigation may not be working correctly. "
                                f"Check valve, schedule, and HA logs."
                            ),
                            "notification_id": f"{DOMAIN}_anomaly_{zs.zone_name}",
                        },
                    )
                    _LOGGER.warning(
                        "Deficit anomaly: zone='%s', deficit=%.1fmm, limit=%.0fmm",
                        zs.zone_name,
                        zone_deficit,
                        anomaly_limit,
                    )
            else:
                self._deficit_anomaly_alerted.discard(zs.zone_name)

    # ── Monitoring mode (no valves) ──────────────────────

    async def _check_and_notify(self, now=None) -> None:
        """Check per-zone deficits and send notification if irrigation needed.

        Called every 6 hours when no valves are configured (monitoring mode).
        """
        zone_lines = []
        needs_irrigation = False
        for zs in self._zones.values():
            zone_deficit = zs._zone_deficit
            threshold = zs.extra_state_attributes.get("threshold_mm", DEFAULT_THRESHOLD)
            if zone_deficit >= threshold:
                needs_irrigation = True
                zone_lines.append(
                    f"- **{zs.zone_name}**: deficit {zone_deficit:.1f} mm, "
                    f"{zs.volume_liters:.0f} L ({zs.duration_s // 60} min)"
                )

        if not needs_irrigation:
            return

        message = (
            "Your garden needs watering:\n\n" + "\n".join(zone_lines) + "\n\nNo irrigation valves are configured — "
            "please water manually or configure valves in the integration settings."
        )

        await self._hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "title": "🌱 Irrigation needed",
                "message": message,
                "notification_id": f"{DOMAIN}_irrigation_alert",
            },
        )
