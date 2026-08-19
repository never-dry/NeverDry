"""The *when* — which zone waters, and whether it may start right now.

The fifth object of the model, and the last to get a module. It was left out
of the first round on the grounds that scheduling is deferred, which conflated
two different things:

* **What is deferred** (DA-3, and the GH #74 answer): the queue, time windows,
  calendars, parallel zone runs, and interleaving another zone during a soak
  pause. Those open only on a concrete demand for parallel zones. Nothing here
  builds them.
* **What already runs**: two decision rules, a serial concurrency policy and a
  rate limit — real scheduling behaviour, merely written inline inside two
  Home Assistant callbacks in ``controller.py`` where it cannot be read as a
  policy or tested without a controller.

This module materializes the second. The seam is decision versus execution:
**the scheduler decides, the controller acts**. Registering time listeners,
spawning tasks and driving valves stay on the HA side; what belongs here is the
answer to "may this zone water now, and why not".

Design intent — **pure**: no Home Assistant import, no I/O, no clock. Whether
something is already running and whether a call is rate-limited are passed *in*
as facts, because a decision function that reads the world cannot be tested
against the cases that matter.

**Wiring status — the rules are read from here.** ``IrrigationController``
holds a :class:`Scheduler` and both handlers ask it; the "is something already
running" check that used to be written out twice is now the serial concurrency
policy, stated once and named.

What is still deferred is what was always deferred: the queue, time windows,
calendars, parallel runs. :meth:`Scheduler.next_eligible` is written and
unreached, because nothing yet asks for more than one zone at a time.

References: ``docs/design_domain_object_model.md`` (the Scheduler object, and
"serial vs parallel is a Scheduler policy"), GH #74 (why the queue is deferred),
AI-183 (why scheduled mode ignores the threshold).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .zone import Zone

#: Minimum gap between two service calls with the same key. Mirrors
#: ``const.MIN_SERVICE_INTERVAL_S``, and the mirror is checked by
#: ``tests/test_architecture.py`` — this constant said 5 against the shipped 10
#: until that test was written, which would have halved the throttle the day the
#: scheduler was wired.
DEFAULT_MIN_SERVICE_INTERVAL_S: int = 10


class Trigger(StrEnum):
    """What asked for this irrigation. Becomes the zone's ``last_source``."""

    SCHEDULED = "scheduled"
    REACTIVE = "reactive"
    MANUAL = "manual"
    SERVICE = "service"


class SkipReason(StrEnum):
    """Why a zone that was considered is not going to water.

    Named rather than boolean on purpose: every one of these is a line the user
    may read in the log, and two of them (``ALREADY_RUNNING``, ``THROTTLED``)
    are the ones that make a watering look mysteriously missing.
    """

    NOTHING_TO_REFILL = "nothing_to_refill"
    BELOW_THRESHOLD = "below_threshold"
    ALREADY_RUNNING = "already_running"
    THROTTLED = "throttled"


class ConcurrencyPolicy(StrEnum):
    """Whether zones may run at the same time.

    ``SERIAL`` is what the code does today, though nowhere does it say so: the
    two handlers each check "is something already running" and bail. Naming the
    policy is the point — it turns an emergent behaviour into a stated one, and
    gives ``PARALLEL`` somewhere to be added if the demand ever arrives.
    """

    SERIAL = "serial"
    PARALLEL = "parallel"


@dataclass(frozen=True)
class Decision:
    """The scheduler's answer about one zone: water, or skip with a reason."""

    should_irrigate: bool
    trigger: Trigger | None = None
    reason: SkipReason | None = None

    @classmethod
    def go(cls, trigger: Trigger) -> Decision:
        """Water this zone now, attributed to ``trigger``."""
        return cls(True, trigger=trigger)

    @classmethod
    def skip(cls, reason: SkipReason) -> Decision:
        """Do not water, and say why."""
        return cls(False, reason=reason)


@dataclass
class Scheduler:
    """Decides *when* a zone waters. Holds no state about the world.

    Every method takes the world's facts as arguments — ``is_running``,
    ``is_throttled`` — instead of reaching for them. That is what makes the
    rules testable in isolation, which is the whole reason they are worth
    extracting from the callbacks they live in today.
    """

    concurrency: ConcurrencyPolicy = ConcurrencyPolicy.SERIAL
    min_service_interval_s: int = DEFAULT_MIN_SERVICE_INTERVAL_S

    @property
    def allows_overlap(self) -> bool:
        """``True`` when a zone may start while another is already watering."""
        return self.concurrency is ConcurrencyPolicy.PARALLEL

    def evaluate_scheduled(self, zone: Zone, *, is_running: bool) -> Decision:
        """The daily top-up: water at the scheduled hour, threshold or not.

        The threshold is deliberately **not** consulted. A schedule means "top
        this zone back up at this hour", and gating it on the reactive threshold
        would silently turn every scheduled run into a reactive one (AI-183).
        The only zone with nothing to do is one that is already full.
        """
        if zone.deficit.value_mm <= 0:
            return Decision.skip(SkipReason.NOTHING_TO_REFILL)
        if is_running and not self.allows_overlap:
            return Decision.skip(SkipReason.ALREADY_RUNNING)
        return Decision.go(Trigger.SCHEDULED)

    def evaluate_reactive(self, zone: Zone, *, is_running: bool, is_throttled: bool = False) -> Decision:
        """Mode A: water as soon as the deficit crosses the zone's threshold."""
        if not zone.needs_water:
            return Decision.skip(SkipReason.BELOW_THRESHOLD)
        if is_running and not self.allows_overlap:
            return Decision.skip(SkipReason.ALREADY_RUNNING)
        if is_throttled:
            return Decision.skip(SkipReason.THROTTLED)
        return Decision.go(Trigger.REACTIVE)

    def next_eligible(self, zones: list[Zone], *, is_running: bool) -> Zone | None:
        """The zone to water next under the current policy, or ``None``.

        Today this is only ever asked with one candidate, because the two
        handlers each act on their own zone. It exists so that the ordering
        question has a home the day more than one zone is eligible at once —
        and it deliberately does **not** implement a queue: a queue has memory
        of what is waiting, which is exactly the deferred design (DA-3, GH #74).
        Driest first is the ordering that needs no memory.
        """
        if is_running and not self.allows_overlap:
            return None
        eligible = [z for z in zones if z.needs_water]
        if not eligible:
            return None
        return max(eligible, key=lambda z: z.deficit.value_mm)
