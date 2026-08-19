"""The zone — the irrigation unit that owns its deficit and turns it into water.

This module writes out the class the domain model has specified since the
beginning and the code never typed: :class:`Zone`. It has a row in the
five-object table, a box in the class diagram, and a named owner for every
behaviour listed in anomaly A1 — but until now no module. What exists instead is
``IrrigationZoneSensor`` plus eighteen ``Zone*Sensor`` numeric projections of it,
with the accounting living in ``IrrigationController``.

Why that matters more than tidiness. Look at where the other two scaffolds point:
``driver.py`` returns a ``DeliveryResult`` — to *whom*? To the zone that settles
it. ``water_balance_model.py`` produces a ``Deficit`` — for *whom*? For the zone
that owns it. Both seams face this class. They are inert not because wiring is
hard but because the object they attach to had not been written.

**The one crediting formula.** Today ``max(0, baseline - delivered*efficiency/area)``
is duplicated across four sites — ``_settle_irrigated_zones``,
``_update_deficit_realtime`` and ``_finalize_manual_session`` in ``controller.py``,
and ``reset_deficit`` in ``sensor.py`` — and the four are *not* identical: two
subtract from a snapshot taken at cycle start, one subtracts from the live value.
:meth:`Zone.credit_delivery` is the single home for that arithmetic. It resolves
the divergence deliberately, on the snapshot when a cycle is open and on the
current value otherwise, so an intermediate write stays idempotent and settling
never double-counts.

**Placement, not exposure, not environment.** ``Zone.placement`` says where the
zone sits — outdoors, on a covered patio, under glass, indoors — and gates
whether rain reaches it at all. It is deliberately *not* named ``environment``
(that is the site-level sensor inventory, see ``environment.py``) nor
``exposure`` (that is the sun/wind microclimate factor of GH #146). Three
overlapping words at two levels; the collision is resolved here rather than
after wiring.

Design intent — this module is **pure**: no Home Assistant import, no I/O, only
arithmetic and rules. It reads a delivery through the structural
:class:`Delivery` protocol rather than importing ``driver.DeliveryResult``,
because ``driver.py`` is HA-coupled and this module must not become so;
``DeliveryResult`` satisfies the protocol without either module knowing about the
other. That is also the first declared interface in the package, which anomaly C1
notes is otherwise entirely absent.

**Wiring status — wired.** ``IrrigationZoneSensor`` holds a :class:`Zone` and
every attribute it used to own is a property onto it; every path that changes
zone state goes through a method here. The commanded partial delivery and the
manual session call :meth:`settle`; the completed delivery and the hand-watered
zone call :meth:`mark_irrigated`; the litres a zone needs are
:attr:`water_demand_l`, once.

Two divergences closed on the way, both of which had grown quietly between
copies of the same bookkeeping: only one settle path rolled the yearly total on
a new year, and the roll-over itself existed four times.

The distinction between the two closing methods took a decision rather than a
move, and it is the reason this was the last object wired. :meth:`settle` credits
an amount and lets the arithmetic land where it lands — right for a delivery that
stopped short. :meth:`mark_irrigated` knows the *outcome* instead: the zone is
full, so the deficit is cleared to exactly zero. The target was computed from the
deficit in the first place, so delivering it clears it by construction and any
residue is rounding.

One thing deliberately unchanged: the entity's deficit **setter does not clamp**,
while :meth:`credit_delivery` does. Callers that clamp for themselves rely on it,
and a persisted value restored at startup goes through the setter. Worth knowing
before assuming every write is bounded.

The wiring status is asserted in ``tests/test_architecture.py``, not only stated
here: this paragraph claimed the module was inert for two releases after it
stopped being so.

References: ``docs/design_domain_object_model.md`` (the domain classes, the
translation chain, ``Zone.placement``, per-zone ``D_max``),
``docs/design_domain_model_anomalies.md`` (A1, the anemic model this closes),
``docs/design_water_balance_reference_model.md`` (D4, a new zone starts at 0).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from .water_balance_model import DEFAULT_D_MAX, Deficit, ReferenceFrame

# Defaults mirror ``const.py`` so a Zone built with no overrides behaves exactly
# like today's zone sensor.
DEFAULT_EFFICIENCY: float = 0.85
DEFAULT_THRESHOLD_MM: float = 20.0
DEFAULT_MICROCLIMATE_FACTOR: float = 1.0


class Placement(StrEnum):
    """Where the zone sits, which decides what reaches it from the sky.

    Categorical rather than a boolean because of ``PATIO``: a covered terrace is
    fully outdoors for temperature and wind yet receives no rain, so "receives
    rain" and "is outdoors" are independent properties. With only the other three
    values one would be tempted to collapse them into a single flag.
    """

    OUTDOOR = "outdoor"
    PATIO = "patio"
    GREENHOUSE = "greenhouse"
    INDOOR = "indoor"

    @property
    def receives_rain(self) -> bool:
        """``True`` only when the zone is open to the sky.

        Gates both the measured rain credit and the forecast rain delay. Sharing
        one gate is what stops the two from ever disagreeing about a zone.
        """
        return self is Placement.OUTDOOR

    @property
    def driven_by_outdoor_et(self) -> bool:
        """``True`` when outdoor evapotranspiration describes this zone's demand.

        A patio is: it feels the same heat and wind as the lawn. Indoors is not —
        that case wants moisture-threshold logic rather than a weather-driven
        balance. A greenhouse is a sheltered regime of its own and is excluded
        here rather than approximated.
        """
        return self in (Placement.OUTDOOR, Placement.PATIO)


class IrrigationMode(StrEnum):
    """How this zone decides to water. Mirrors ``const.py``."""

    MANUAL = "manual"
    REACTIVE = "reactive"
    SCHEDULED = "scheduled"


@runtime_checkable
class Delivery(Protocol):
    """The structural shape :meth:`Zone.credit_delivery` needs from a delivery.

    A protocol rather than an import: ``driver.DeliveryResult`` satisfies it
    without this pure module taking on ``driver.py``'s Home Assistant
    dependency. Anything reporting how much water it actually delivered can
    settle a zone — including, later, a manual "I watered by hand" record.
    """

    liters_delivered: float
    elapsed_s: float


@dataclass(frozen=True)
class CycleSoakRule:
    """Split a run into segments with soak time between them.

    A Zone rule, not a Scheduler policy: the parameters follow from soil
    infiltration rate, slope and soil type, which are properties of this patch of
    ground. Executing the segments is controller/actuator mechanics. Unset means
    today's behaviour — one uninterrupted run.
    """

    max_segment_s: int | None = None
    soak_s: int | None = None

    @property
    def is_active(self) -> bool:
        """``True`` when both halves are configured; either alone means nothing."""
        return bool(self.max_segment_s and self.soak_s)


@dataclass
class WaterCounters:
    """The zone's delivered-water bookkeeping, kept together rather than loose.

    These are the attributes the controller reaches in and writes today; grouping
    them makes the surface that has to move explicit.
    """

    last_volume_l: float = 0.0
    session_water_l: float = 0.0
    total_water_l: float = 0.0
    yearly_water_l: float = 0.0
    yearly_water_year: int | None = None

    def credit(self, liters: float, *, year: int) -> None:
        """Add ``liters`` to every counter, rolling the yearly total on year change."""
        credited = round(liters, 1)
        self.last_volume_l = credited
        self.session_water_l = credited
        self.total_water_l += credited
        if self.yearly_water_year != year:
            self.yearly_water_l = 0.0
            self.yearly_water_year = year
        self.yearly_water_l += credited

    def reset_yearly(self, *, year: int) -> None:
        """Clear the yearly total, preserving the lifetime one (user-invoked reset)."""
        self.yearly_water_l = 0.0
        self.yearly_water_year = year


@dataclass
class Zone:
    """One irrigation unit: owns its deficit, and turns it into litres.

    The deficit is a :class:`Deficit` value object rather than a bare float, so
    the reference frame travels with the number and two deficits from different
    frames cannot be compared by accident.
    """

    name: str

    # ── Geometry and hydraulics ─────────────────────────────────────────────
    area_m2: float = 0.0
    efficiency: float = DEFAULT_EFFICIENCY

    # ── What grows here, and where here is ──────────────────────────────────
    plant_family: str | None = None
    manual_kc: float | None = None
    # Site exposure (GH #146): sun and wind relative to an open site. Multiplies
    # the seasonal curve rather than replacing it.
    exposure: str | None = None
    microclimate_factor: float = DEFAULT_MICROCLIMATE_FACTOR
    placement: Placement = Placement.OUTDOOR

    # ── Water balance ───────────────────────────────────────────────────────
    # D_max is the reservoir *this* soil can hold — soil type times root depth —
    # so it is per-zone. Only the clamping mechanism is shared across models.
    d_max: float = DEFAULT_D_MAX
    threshold_mm: float = DEFAULT_THRESHOLD_MM
    # Which frame this zone's deficit is measured against. It has to be given,
    # not assumed: a site running on a soil probe produces a VWC_SYSTEM deficit,
    # and tagging it ET would be the value object asserting the one thing it was
    # created to make impossible — a number carrying the wrong frame.
    frame: ReferenceFrame = ReferenceFrame.ET
    deficit: Deficit = field(default=None)  # type: ignore[assignment]

    # ── Scheduling ──────────────────────────────────────────────────────────
    irrigation_mode: IrrigationMode = IrrigationMode.MANUAL
    irrigation_time: str | None = None
    cycle_soak: CycleSoakRule = field(default_factory=CycleSoakRule)

    # ── Session state ───────────────────────────────────────────────────────
    counters: WaterCounters = field(default_factory=WaterCounters)
    last_irrigated: datetime | None = None
    last_source: str | None = None
    last_duration_s: int = 0
    # Deficit captured when a cycle opens; ``None`` outside a cycle. Present so
    # repeated real-time credits during a flow-metered run stay idempotent.
    _cycle_baseline_mm: float | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        # A new zone starts at zero rather than inheriting a shared reference
        # deficit, which drifts high under per-zone irrigation (reference model
        # D4, GH #123).
        if self.deficit is None:
            self.deficit = Deficit.zero(self.frame, d_max=self.d_max, source=self.name)

    @property
    def cycle_baseline_mm(self) -> float | None:
        """The deficit a cycle started from, or ``None`` outside one."""
        return self._cycle_baseline_mm

    @cycle_baseline_mm.setter
    def cycle_baseline_mm(self, value: float | None) -> None:
        self._cycle_baseline_mm = value

    # ── Crop coefficient ────────────────────────────────────────────────────

    def effective_kc(self, base_kc: float) -> float:
        """Apply this zone's site exposure to a seasonal or overridden ``base_kc``.

        The seasonal curve itself is intentionally *not* recomputed here: its
        plant-family table has one home already, and copying it in would create
        the second source of truth that anomaly E1 is about. The zone owns the
        part that is genuinely its own — where it sits relative to sun and wind.
        """
        return round(base_kc * self.microclimate_factor, 4)

    # ── Water balance ───────────────────────────────────────────────────────

    def accumulate(self, *, dt_h: float, et_h: float, base_kc: float, rain_mm: float = 0.0) -> Deficit:
        """Advance the deficit over ``dt_h`` hours and return it.

        ``+ET*Kc*dt - rain``, clamped into ``[0, d_max]``. Rain is credited only
        when :attr:`placement` is open to the sky: a patio or indoor zone never
        saw that water, and crediting it would silently under-water them.
        """
        credited_rain = rain_mm if self.placement.receives_rain else 0.0
        advanced = self.deficit.value_mm + et_h * self.effective_kc(base_kc) * dt_h - credited_rain
        self.deficit = self.deficit.with_value(advanced).clamped()
        return self.deficit

    @property
    def water_demand_l(self) -> float:
        """Litres needed to clear the current deficit over this zone's area.

        1 mm over 1 m² is 1 litre, divided by the system's application
        efficiency — a drip line puts more of what it emits into the root zone
        than a pop-up sprinkler does.
        """
        if self.efficiency <= 0:
            return 0.0
        return self.deficit.as_liters(self.area_m2) / self.efficiency

    @property
    def needs_water(self) -> bool:
        """``True`` when the deficit has reached this zone's trigger threshold."""
        return self.deficit.value_mm >= self.threshold_mm

    def delivered_mm(self, liters: float) -> float:
        """Convert delivered litres into millimetres over this zone's area."""
        if self.area_m2 <= 0:
            return 0.0
        return liters * self.efficiency / self.area_m2

    # ── The irrigation cycle ────────────────────────────────────────────────

    def begin_cycle(self) -> None:
        """Open a cycle, snapshotting the deficit it starts from."""
        self._cycle_baseline_mm = self.deficit.value_mm

    def credit_delivery(self, delivery: Delivery) -> Deficit:
        """Credit delivered water against the deficit — *the* one formula.

        Subtracts from the cycle snapshot when a cycle is open, and from the
        current value otherwise. That distinction is what makes repeated credits
        during a flow-metered run idempotent, and it is the point where the four
        divergent copies in today's code are reconciled: the manual path used to
        subtract from the live value even mid-cycle.
        """
        baseline = self._cycle_baseline_mm if self._cycle_baseline_mm is not None else self.deficit.value_mm
        remaining = baseline - self.delivered_mm(delivery.liters_delivered)
        self.deficit = self.deficit.with_value(remaining).clamped()
        return self.deficit

    def settle(self, delivery: Delivery, *, source: str, at: datetime) -> Deficit:
        """Close a cycle: credit the final figure, stamp it, and drop the snapshot.

        Takes the timestamp rather than reading the clock, so the module stays
        pure and the behaviour stays testable.
        """
        self.credit_delivery(delivery)
        self.counters.credit(delivery.liters_delivered, year=at.year)
        self.last_irrigated = at
        self.last_source = source
        self.last_duration_s = round(delivery.elapsed_s)
        self._cycle_baseline_mm = None
        return self.deficit

    def mark_irrigated(
        self,
        *,
        source: str,
        at: datetime,
        credited_liters: float | None = None,
        duration_s: int | None = None,
    ) -> Deficit:
        """The zone is watered: clear the deficit to exactly zero.

        Distinct from :meth:`settle`, and the distinction is what took a
        decision rather than a move. ``settle`` credits an amount and lets the
        arithmetic land where it lands — right for a delivery that stopped
        short. Here the *outcome* is known: the zone is full. The target volume
        was computed from the deficit in the first place, so delivering it
        clears the deficit by construction and any residue is rounding; leaving
        a few hundredths of a millimetre behind would be arithmetic outliving
        its own meaning.

        Two callers, one rule, differing only in where the credited volume comes
        from. A completed delivery passes what it measured. The hose case passes
        nothing, and the volume is inferred from the deficit about to be cleared
        — which is why it is read *before* the zeroing.
        """
        credited = self.water_demand_l if credited_liters is None else credited_liters
        self.deficit = self.deficit.with_value(0.0).clamped()
        self.counters.credit(credited, year=at.year)
        self.last_irrigated = at
        self.last_source = source
        # Left alone when unknown rather than zeroed: the hose case has no
        # duration, and writing 0 would claim one.
        if duration_s is not None:
            self.last_duration_s = duration_s
        self._cycle_baseline_mm = None
        return self.deficit
