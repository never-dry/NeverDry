"""Water-balance model abstraction — the single home for computing *how much*
water a patch of soil needs, independent of *how* it is measured.

This module materializes two domain-model concepts that today live implicitly,
scattered inside ``sensor.py`` (``DrynessIndexSensor`` / ``ETSensor`` and the
per-zone deficit loop):

* :class:`WaterBalanceModel` (abstract) — the *scientific model*: "give me the
  deficit, no matter which inputs I compute it from". Its three concrete
  strategies mirror the reference frames of
  ``docs/design_water_balance_reference_model.md``:
  :class:`ETModel` (temperature + rain), :class:`VWCSystemModel` (one system
  soil-moisture probe), :class:`VWCPerZoneModel` (a per-zone probe, target
  AI-174).
* :class:`Deficit` (value object) — the *quantity*: millimetres **plus the
  reference frame they are defined against**. The load-bearing rule of the
  reference model is that *two deficits are comparable only if they share a
  frame*, so a bare ``float`` is not enough — the frame travels with the value.

Design intent — this module is deliberately **pure**: no Home Assistant import,
no I/O, only arithmetic on floats. The "how much water" math has no reason to
touch HA, which makes it trivially testable and reusable. This mirrors, on the
*sensing* side, what ``driver.py`` did on the *actuation* side: extract the
implicit domain object into a self-contained module now, wire the existing call
sites onto it in a later phase.

The seam is the **output**, not the input: the three models share no inputs
(ET needs weather, VWC needs a probe), but every model produces a
:class:`Deficit` in mm. This is exactly why the abstraction sits at the output —
each model copes with whatever sensors it has and exposes the same quantity.

Translation chain (see ``docs/design_domain_object_model.md``)::

    WaterBalanceModel  ──produces──▶  Deficit (mm)  ──Zone──▶  Actuator (liters)

References: ``docs/design_water_balance_reference_model.md`` (D1-D5, reference
frames), ``docs/design_domain_object_model.md`` (the domain classes),
GH #123 (the deficit reference-frame bug this model makes impossible).

**Wiring status — the model owns the balance.** ``DrynessIndexSensor`` holds a
:class:`WaterBalanceModel` and calls :meth:`step`; its ``_deficit`` is a view
onto the model, so there is one storage rather than two. Which model it holds is
decided by :func:`build_model` from what the site declared — the capability
match, ``Environment.declared_sensors >= model.required_sensors``.

The zones are reached through the rate, not the model: the hub asks its model for
``et_rate`` and broadcasts it, and each zone integrates that rate against its own
Kc. So the tier a site runs propagates to every zone without any zone knowing
which tier it is.

What has *not* moved, and is a real limit rather than an oversight: the recorder
**backfill** replays history through :meth:`ETModel.et_hourly` directly, so a
site running a higher tier is bootstrapped with the temperature-only estimate.
Replaying Penman-Monteith would need historical humidity, wind and radiation,
which is a different problem from choosing a model for the present.

``tests/test_architecture.py`` asserts that each of these formulas has exactly
one home, so a copy cannot quietly reappear.
"""

from __future__ import annotations

import abc
import math
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import ClassVar

from .environment import SensorKind

# Defaults mirror ``const.py`` so a model built with no overrides behaves exactly
# like today's sensors. Kept as module constants (not a HA import) to keep the
# module pure; the integration passes the user-configured values in.
DEFAULT_ALPHA: float = 0.22
DEFAULT_T_BASE: float = 9.0
DEFAULT_D_MAX: float = 100.0
DEFAULT_FIELD_CAPACITY: float = 0.30
DEFAULT_ROOT_DEPTH: float = 0.30
DEFAULT_KC: float = 1.0

# Standard sea-level atmospheric pressure [kPa], for the FAO-56 Penman-Monteith
# psychrometric constant. A site can override it (it varies with altitude).
DEFAULT_PRESSURE_KPA: float = 101.3

# Solar constant [MJ/m²/min], for the Hargreaves extraterrestrial radiation Ra.
_SOLAR_CONSTANT_MJ_MIN: float = 0.0820
# MJ/m²/day of radiation → equivalent mm/day of evaporation (FAO-56 eq. 20).
_MJ_TO_MM_EVAP: float = 0.408

# Metres → millimetres. A VWC fraction over a root depth in metres yields metres
# of water; x1000 expresses the deficit in the model's canonical millimetres.
_M_TO_MM: float = 1000.0


def _clamp(value: float, lower: float, upper: float) -> float:
    """Clamp ``value`` into ``[lower, upper]`` (the FAO-56 ``[0, D_max]`` box)."""
    return max(lower, min(value, upper))


# ── Reference frame: what a deficit is measured *relative to* ───────────────


class ReferenceFrame(StrEnum):
    """The measurement frame a :class:`Deficit` is defined against.

    Comparability follows the reference model: ``ET`` and ``VWC_SYSTEM`` are
    **shared** across zones (all zones read the same feeds/probe, differing only
    by Kc), so their deficits are comparable. ``VWC_PER_ZONE`` is **not** shared:
    each zone measures a different patch of soil, so two per-zone deficits are
    comparable only when they come from the *same* probe.
    """

    ET = "et"
    VWC_SYSTEM = "vwc_system"
    VWC_PER_ZONE = "vwc_per_zone"

    @property
    def is_shared(self) -> bool:
        """``True`` when every zone shares this frame (ET feeds or one system probe)."""
        return self is not ReferenceFrame.VWC_PER_ZONE


# ── The quantity: a deficit in mm, carrying its reference frame ─────────────


@dataclass(frozen=True)
class Deficit:
    """A soil water deficit in millimetres, tagged with its :class:`ReferenceFrame`.

    Immutable value object. It is the common currency every
    :class:`WaterBalanceModel` returns and the Zone consumes to decide watering.
    The ``frame`` (and, for per-zone probes, ``source``) travel with the number
    so the "two deficits are comparable only within one frame" rule of the
    reference model is enforceable in code rather than left to convention.
    """

    value_mm: float
    frame: ReferenceFrame
    d_max: float = DEFAULT_D_MAX
    # Identity of the frame for per-zone deficits (e.g. the probe/zone id). For
    # shared frames (ET, system VWC) it is ``None`` — the frame alone identifies
    # comparability. See :meth:`is_comparable_to`.
    source: str | None = None

    @classmethod
    def zero(cls, frame: ReferenceFrame, d_max: float = DEFAULT_D_MAX, source: str | None = None) -> Deficit:
        """Return a fresh zero deficit — a new zone starts here (reference model D4)."""
        return cls(0.0, frame, d_max, source)

    def clamped(self) -> Deficit:
        """Return a copy with ``value_mm`` clamped into the FAO-56 ``[0, d_max]`` box."""
        return replace(self, value_mm=_clamp(self.value_mm, 0.0, self.d_max))

    def with_value(self, value_mm: float) -> Deficit:
        """Return a copy at ``value_mm`` (unclamped); pair with :meth:`clamped`."""
        return replace(self, value_mm=value_mm)

    def is_comparable_to(self, other: Deficit) -> bool:
        """``True`` when ``self`` and ``other`` live in the same reference frame.

        Shared frames (ET, system VWC) are comparable whenever the frame matches.
        Per-zone frames additionally require the same ``source`` — two zones each
        on their own probe are *not* comparable even though both are
        ``VWC_PER_ZONE`` (reference model, "Reference frames").
        """
        if self.frame is not other.frame:
            return False
        if self.frame.is_shared:
            return True
        return self.source is not None and self.source == other.source

    def as_liters(self, area_m2: float) -> float:
        """Project this deficit onto a zone area: 1 mm over 1 m² is 1 litre."""
        return self.value_mm * area_m2


# ── Model inputs: each strategy consumes its own reading ────────────────────


@dataclass(frozen=True)
class ETStep:
    """One integration step for :class:`ETModel`: a time delta plus its weather.

    ``dt_h`` is the hours elapsed since the previous step (forward-Euler variable
    step, as today's event-driven integrator); ``temp_c`` the current temperature
    in °C; ``rain_mm`` the rain credited over this step in mm.
    """

    dt_h: float
    temp_c: float
    rain_mm: float = 0.0


@dataclass(frozen=True)
class PenmanStep:
    """One integration step for :class:`PenmanMonteithModel` — a richer weather reading.

    Superset of :class:`ETStep`: it carries the same ``dt_h`` / ``temp_c`` /
    ``rain_mm`` the integrator needs, plus the extra inputs the FAO-56
    Penman-Monteith reference equation requires and *not every user has* — which
    is exactly why Penman-Monteith is a higher tier than the temperature-only
    :class:`ETModel`:

    * ``rh_pct`` — relative humidity [%], for the vapour-pressure deficit
    * ``wind_m_s`` — wind speed at 2 m [m/s] (``u2``)
    * ``net_radiation_mj`` — net radiation [MJ/m²/day] (``Rn``); soil heat flux
      ``G`` is taken as 0 for daily steps
    """

    dt_h: float
    temp_c: float
    rh_pct: float
    wind_m_s: float
    net_radiation_mj: float
    rain_mm: float = 0.0


@dataclass(frozen=True)
class HargreavesStep:
    """One integration step for :class:`HargreavesModel` — a temperature-range reading.

    The middle tier: it needs only the daily temperature **range** (``tmax_c`` /
    ``tmin_c``) plus the calendar ``day_of_year``, because its radiation term is
    the *extraterrestrial* radiation computed from latitude and date (FAO-56
    eq. 21) — no humidity, wind or radiation **sensor** required. Latitude is a
    site constant held on the model, not on the step.

    * ``tmax_c`` / ``tmin_c`` — daily max/min temperature [°C]
    * ``day_of_year`` — 1..365/366, for the astronomical radiation
    """

    dt_h: float
    tmax_c: float
    tmin_c: float
    day_of_year: int
    rain_mm: float = 0.0


@dataclass(frozen=True)
class VWCReading:
    """One reading for a VWC model: the current volumetric water content.

    ``vwc`` is a volumetric fraction in ``[0, 1]`` (e.g. ``0.22`` = 22 %),
    directly comparable to ``field_capacity``. Readings coming from a real
    sensor must pass through :func:`vwc_to_fraction` first — that is where the
    invariant is established, not here.
    """

    vwc: float


def vwc_to_fraction(value: float) -> float | None:
    """Normalise one raw soil-moisture reading to a VWC fraction, or reject it.

    Consumer probes report percentages almost by definition (Ecowitt, most
    Zigbee models): 45 rather than 0.45. Fed straight into
    ``(field_capacity - vwc)``, a percentage makes the bracket negative for
    *every* possible reading — even a bone-dry 15 % — and the clamp to zero
    then hides it: the deficit sits at 0 forever and the zone never waters, in
    silence (GH #170).

    A reading above 1 is unambiguously a percentage. There is no soil whose
    volumetric water content exceeds 1: soils saturate around 0.5 and peat
    below 0.9, so the value disambiguates itself and no new setting is needed.
    Exactly ``1.0`` is read as a fraction (fully saturated), not as 1 %, since
    the latter is not a state soil can be in.

    What remains outside ``[0, 1]`` after that conversion is **not a VWC** at
    all — a raw ADC count (Ecowitt exposes 70..500), a negative, a NaN — and is
    rejected rather than clamped. Clamping 310 to 1.0 would assert "saturated"
    about a probe that is not measuring water content, which is the same
    silence this function exists to remove. The caller keeps its previous
    deficit and warns.

    Note this is a safety net at the boundary, not a sensor model: it cannot
    tell a *calibrated* percentage from an uncalibrated one. Mapping a probe's
    own scale onto real VWC is two-point calibration, a separate concern.

    Returns the fraction, or ``None`` when the reading cannot be one.
    """
    if math.isnan(value) or math.isinf(value):
        return None
    fraction = value / 100.0 if value > 1.0 else value
    if not 0.0 <= fraction <= 1.0:
        return None
    return fraction


def vwc_deficit_mm(vwc: float, *, field_capacity: float, root_depth: float) -> float:
    """Millimetres of water missing from the root zone, from a VWC fraction.

    ``(field_capacity - vwc) · root_depth`` gives metres of water; x1000 puts it
    in the model's canonical millimetres. Unclamped on purpose — a negative
    result means wetter than field capacity, which is real information the
    caller may want before it is flattened to zero.

    A module-level function rather than a method because the entity layer needs
    exactly this arithmetic and nothing else about the model; one home for the
    formula is the point.
    """
    return (field_capacity - vwc) * root_depth * _M_TO_MM


# Union of everything a model's :meth:`WaterBalanceModel.step` may accept.
ModelInput = ETStep | HargreavesStep | PenmanStep | VWCReading


# ── The abstract water-balance model (the "how much" seam) ──────────────────


class WaterBalanceModel(abc.ABC):
    """A strategy that turns sensor inputs into a per-frame :class:`Deficit` in mm.

    Concrete models differ in *what they read* (weather vs a moisture probe) and
    in whether they are **stateful** (ET integrates over time) or **stateless**
    (VWC recomputes each reading), but they all expose the same contract:
    :meth:`step` advances the model and returns the current deficit,
    :meth:`apply_irrigation` registers delivered water, :meth:`reset` returns to
    zero. The Zone talks to this interface and never to a specific model.
    """

    #: Whether the model accumulates state across steps (ET) or recomputes each
    #: reading from scratch (VWC). Subclasses set it as a class attribute.
    is_stateful: ClassVar[bool]

    #: The input DTO :meth:`step` accepts. Declared because the *host* has to
    #: build it: a model whose input nobody can produce is selectable and not
    #: runnable, which is worse than absent.
    input_type: ClassVar[type]

    #: Stable identifier for this method, stored in the config entry and shown
    #: in the form. A name, not a class path: the class may move, the user's
    #: choice must not.
    method_id: ClassVar[str]

    #: The environmental inputs this model cannot work without, in the same
    #: vocabulary :class:`~.environment.Environment` declares bindings in. It is
    #: half of the capability match — ``declared >= required`` — and it lives on
    #: the model because *what a method needs* is a property of the method, not
    #: of the installation. Rain is absent on purpose: it is subtracted when
    #: present and simply zero when not, so it never makes a model unavailable.
    required_sensors: ClassVar[frozenset[SensorKind]]

    def __init__(self, *, d_max: float = DEFAULT_D_MAX, initial_mm: float = 0.0, source: str | None = None) -> None:
        """Initialise the model at ``initial_mm`` (default 0 — reference model D4)."""
        self._d_max = d_max
        self._source = source
        self._value_mm = _clamp(initial_mm, 0.0, d_max)

    @property
    @abc.abstractmethod
    def reference_frame(self) -> ReferenceFrame:
        """The frame every :class:`Deficit` from this model is defined against."""

    @property
    def d_max(self) -> float:
        """The FAO-56 upper clamp on the deficit [mm]."""
        return self._d_max

    @property
    def deficit(self) -> Deficit:
        """The current deficit as a frame-tagged value object."""
        source = self._source if self.reference_frame is ReferenceFrame.VWC_PER_ZONE else None
        return Deficit(round(self._value_mm, 4), self.reference_frame, self._d_max, source)

    @abc.abstractmethod
    def step(self, inputs: ModelInput) -> Deficit:
        """Advance the model with one reading and return the updated deficit."""

    def apply_irrigation(self, delivered_mm: float) -> Deficit:
        """Register ``delivered_mm`` of water just applied and return the new deficit.

        Stateful models subtract it (and clamp at 0); stateless models override
        to a no-op because their next reading already reflects the wetter soil.
        """
        self._value_mm = _clamp(self._value_mm - delivered_mm, 0.0, self._d_max)
        return self.deficit

    def reset(self) -> Deficit:
        """Reset the deficit to zero (a zone was fully irrigated) and return it."""
        self._value_mm = 0.0
        return self.deficit

    def restore(self, value_mm: float) -> Deficit:
        """Adopt a deficit computed elsewhere — a persisted value or a backfill.

        Distinct from :meth:`step` on purpose: this is not a reading, it is the
        model being told where it already was. A restart and a recorder replay
        both need it, and neither can be expressed as a step.

        Clamped like every other entry point, because a stored value can outlive
        the ``d_max`` that was in force when it was written.
        """
        self._value_mm = _clamp(float(value_mm), 0.0, self._d_max)
        return self.deficit


# ── ET frame: shared forward-Euler integration, pluggable ET method ─────────


class ETBalanceModel(WaterBalanceModel):
    """Abstract ET-frame model: owns the integration, subclasses supply the ET rate.

    All ET methods share the same water balance — forward-Euler
    ``D += ET_h · Kc · Δt - rain``, clamped — and the same reference frame (the
    deficit is relative to the shared weather feeds, so it is comparable across
    zones). They differ **only** in how the hourly ET rate is computed, which is
    the single abstract hook :meth:`et_rate`. This is the "ET tier" seam: a
    temperature-only estimate (:class:`ETModel`) and the fuller FAO-56
    Penman-Monteith (:class:`PenmanMonteithModel`) are two rates behind one
    integrator, not two integrators.

    ``Kc`` (the crop coefficient) is a **Zone** attribute in the domain model,
    not a property of the physics: a per-zone instance is built with that zone's
    ``kc``, while the *system reference* uses ``kc = 1.0``. Each zone owns its own
    instance because irrigation resets are per-zone and independent — a shared
    reference cannot be scaled proportionally after the fact (reference model
    D1/D4).
    """

    is_stateful: ClassVar[bool] = True

    def __init__(
        self,
        *,
        kc: float = DEFAULT_KC,
        d_max: float = DEFAULT_D_MAX,
        initial_mm: float = 0.0,
        alpha: float = DEFAULT_ALPHA,
        t_base: float = DEFAULT_T_BASE,
    ) -> None:
        """Configure ``kc``, the clamp ``d_max``, and the warm-up rate parameters."""
        super().__init__(d_max=d_max, initial_mm=initial_mm)
        self._kc = kc
        self._alpha = alpha
        self._t_base = t_base

    def warmup_rate(self, inputs: ETStep) -> float:
        """The temperature-only rate, used while this tier's own inputs are missing.

        Every richer tier needs something that takes time to become available —
        a diurnal range that has to be observed for a day. Freezing the deficit
        until then is defensible for a restart and wrong for a fresh install,
        where it would mean a garden that appears not to dry out at all.

        So a tier that cannot compute its own rate yet falls back to the one
        every site can compute. It is a worse estimate, which is the point: it
        is the estimate this integration ran for everyone until now, and it is
        strictly better than pretending nothing evaporates.
        """
        return ETModel.et_hourly(inputs.temp_c, alpha=self._alpha, t_base=self._t_base)

    @property
    def reference_frame(self) -> ReferenceFrame:
        """ET deficits are shared across zones (same temperature + rain feeds)."""
        return ReferenceFrame.ET

    @property
    def kc(self) -> float:
        """The crop coefficient this instance integrates with (1.0 for the reference)."""
        return self._kc

    @abc.abstractmethod
    def et_rate(self, inputs: ModelInput) -> float:
        """Return the ET rate [mm/h] for this step (the only thing ET methods vary)."""

    def step(self, inputs: ModelInput) -> Deficit:
        """Integrate one step: add ``ET_h · Kc · Δt``, subtract rain, clamp.

        The concrete :meth:`et_rate` type-guards its own input DTO, so ``dt_h`` and
        ``rain_mm`` are only read once the input is known to be the right shape.
        """
        et_h = self.et_rate(inputs)
        self._value_mm = _clamp(
            self._value_mm + et_h * self._kc * inputs.dt_h - inputs.rain_mm,
            0.0,
            self._d_max,
        )
        return self.deficit


# ── Strategy 1a: simplified temperature-only ET (today's model) ─────────────


class ETModel(ETBalanceModel):
    """Simplified temperature-only ET — the linear estimate NeverDry uses today.

    Needs a single input, temperature, which makes it the baseline tier: any user
    with a weather/temperature sensor can run it. The demand is
    :func:`et_hourly`, the same formula written twice today (``ETSensor`` and
    ``DrynessIndexSensor``) that the abstraction unifies here.
    """

    input_type: ClassVar[type] = ETStep

    method_id: ClassVar[str] = "et_simple"

    required_sensors: ClassVar[frozenset[SensorKind]] = frozenset({SensorKind.TEMPERATURE})

    def __init__(
        self,
        *,
        alpha: float = DEFAULT_ALPHA,
        t_base: float = DEFAULT_T_BASE,
        kc: float = DEFAULT_KC,
        d_max: float = DEFAULT_D_MAX,
        initial_mm: float = 0.0,
    ) -> None:
        """Configure ET sensitivity ``alpha``, base temperature ``t_base`` and ``kc``."""
        super().__init__(kc=kc, d_max=d_max, initial_mm=initial_mm, alpha=alpha, t_base=t_base)

    @staticmethod
    def et_hourly(temp_c: float, *, alpha: float = DEFAULT_ALPHA, t_base: float = DEFAULT_T_BASE) -> float:
        """Instantaneous ET estimate [mm/h] — ``max(0, alpha · (T - T_base) / 24)``."""
        return max(0.0, alpha * (temp_c - t_base) / 24.0)

    def et_rate(self, inputs: ModelInput) -> float:
        """The temperature-only ET rate [mm/h] from an :class:`ETStep`."""
        if not isinstance(inputs, ETStep):
            raise TypeError(f"ETModel.step expects ETStep, got {type(inputs).__name__}")
        return self.et_hourly(inputs.temp_c, alpha=self._alpha, t_base=self._t_base)


# ── Strategy 1b: FAO-56 Penman-Monteith ET (higher tier, more inputs) ───────


class PenmanMonteithModel(ETBalanceModel):
    """FAO-56 Penman-Monteith reference ET — the physically-grounded higher tier.

    Same ET frame and same integrator as :class:`ETModel`; it only computes a
    better ET rate, at the cost of inputs not every user has: relative humidity,
    wind speed and net radiation (see :class:`PenmanStep`). This is the concrete
    payoff of abstracting at the *output*: a setup can move from ``ETModel`` to
    ``PenmanMonteithModel`` — or fall back — and the Zone still just reads a
    :class:`Deficit`, unaware of which tier produced it.

    The reference equation (FAO-56 eq. 6) yields ET₀ in mm/day; the integrator
    works in mm/h, so the rate is ET₀/24. Soil heat flux ``G`` is taken as 0
    (daily assumption).
    """

    input_type: ClassVar[type] = PenmanStep

    method_id: ClassVar[str] = "penman_monteith"

    # Radiation is absent on purpose. FAO-56 computes the net radiation this
    # equation reads, and can estimate the incoming shortwave from the diurnal
    # range when no pyranometer exists (eq. 50) — so a site with humidity and
    # wind can run this tier, more accurately with a radiation sensor and still
    # usefully without one. Requiring the instrument would exclude the majority
    # of stations to gain precision they cannot supply anyway.
    required_sensors: ClassVar[frozenset[SensorKind]] = frozenset(
        {SensorKind.TEMPERATURE, SensorKind.HUMIDITY, SensorKind.WIND_SPEED}
    )

    def __init__(
        self,
        *,
        kc: float = DEFAULT_KC,
        pressure_kpa: float = DEFAULT_PRESSURE_KPA,
        d_max: float = DEFAULT_D_MAX,
        initial_mm: float = 0.0,
        alpha: float = DEFAULT_ALPHA,
        t_base: float = DEFAULT_T_BASE,
    ) -> None:
        """Configure ``kc`` and site atmospheric ``pressure_kpa`` (altitude-dependent)."""
        super().__init__(kc=kc, d_max=d_max, initial_mm=initial_mm, alpha=alpha, t_base=t_base)
        self._pressure_kpa = pressure_kpa

    @staticmethod
    def _saturation_vapour_pressure(temp_c: float) -> float:
        """Saturation vapour pressure e°(T) [kPa] — FAO-56 eq. 11."""
        return 0.6108 * math.exp(17.27 * temp_c / (temp_c + 237.3))

    @classmethod
    def et0_daily(
        cls,
        *,
        temp_c: float,
        rh_pct: float,
        wind_m_s: float,
        net_radiation_mj: float,
        pressure_kpa: float = DEFAULT_PRESSURE_KPA,
    ) -> float:
        """FAO-56 Penman-Monteith reference ET₀ [mm/day] (eq. 6, ``G = 0``)."""
        es = cls._saturation_vapour_pressure(temp_c)
        ea = es * max(0.0, min(rh_pct, 100.0)) / 100.0
        vpd = es - ea
        # Slope of the saturation vapour-pressure curve Δ [kPa/°C] — FAO-56 eq. 13.
        delta = 4098.0 * es / (temp_c + 237.3) ** 2
        # Psychrometric constant gamma [kPa/°C] — FAO-56 eq. 8.
        gamma = 0.000665 * pressure_kpa
        numerator = 0.408 * delta * net_radiation_mj + gamma * (900.0 / (temp_c + 273.0)) * wind_m_s * vpd
        denominator = delta + gamma * (1.0 + 0.34 * wind_m_s)
        return max(0.0, numerator / denominator)

    def et_rate(self, inputs: ModelInput) -> float:
        """The Penman-Monteith ET rate [mm/h] from a :class:`PenmanStep` (ET₀/24)."""
        if isinstance(inputs, ETStep):
            return self.warmup_rate(inputs)
        if not isinstance(inputs, PenmanStep):
            raise TypeError(f"PenmanMonteithModel.step expects PenmanStep, got {type(inputs).__name__}")
        et0 = self.et0_daily(
            temp_c=inputs.temp_c,
            rh_pct=inputs.rh_pct,
            wind_m_s=inputs.wind_m_s,
            net_radiation_mj=inputs.net_radiation_mj,
            pressure_kpa=self._pressure_kpa,
        )
        return et0 / 24.0


# ── Strategy 1c: Hargreaves-Samani ET (middle tier, computed radiation) ─────


class HargreavesModel(ETBalanceModel):
    """FAO-56 Hargreaves-Samani ET — the middle tier between temperature-only and Penman.

    Physically better than :class:`ETModel` (it captures the diurnal temperature
    range and the seasonal/latitude radiation cycle) yet needs **no extra
    sensor**: its radiation term is the *extraterrestrial* radiation ``Ra``,
    computed purely from latitude and day-of-year (FAO-56 eq. 21). So the only
    inputs are daily max/min temperature and the date — the sweet spot for users
    who have a temperature sensor but no humidity/wind/radiation gear.

    ``ET0 = 0.0023 · (Tmean + 17.8) · sqrt(Tmax - Tmin) · Ra_mm`` [mm/day], with
    ``Tmean = (Tmax + Tmin) / 2`` and ``Ra_mm`` the extraterrestrial radiation
    expressed in mm/day. Latitude is a site constant on the model; the date rides
    on each :class:`HargreavesStep`. The rate is ET0/24 for the hourly integrator.
    """

    input_type: ClassVar[type] = HargreavesStep

    method_id: ClassVar[str] = "hargreaves"

    # Only a thermometer. The daily extremes used to be two more bindings the
    # user had to build with helpers; they are observed from this same sensor
    # now (see :class:`DiurnalRange`), so the requirement is the reading, not
    # the summary of it.
    required_sensors: ClassVar[frozenset[SensorKind]] = frozenset({SensorKind.TEMPERATURE})

    def __init__(
        self,
        *,
        latitude_deg: float,
        kc: float = DEFAULT_KC,
        d_max: float = DEFAULT_D_MAX,
        initial_mm: float = 0.0,
        alpha: float = DEFAULT_ALPHA,
        t_base: float = DEFAULT_T_BASE,
    ) -> None:
        """Configure the site ``latitude_deg`` (drives the astronomical radiation) and ``kc``."""
        super().__init__(kc=kc, d_max=d_max, initial_mm=initial_mm, alpha=alpha, t_base=t_base)
        self._latitude_deg = latitude_deg

    @staticmethod
    def extraterrestrial_radiation(day_of_year: int, latitude_deg: float) -> float:
        """Extraterrestrial radiation Ra [MJ/m²/day] — FAO-56 eq. 21 (no sensor needed)."""
        phi = math.radians(latitude_deg)
        # Inverse relative Earth-Sun distance (eq. 23) and solar declination (eq. 24).
        dr = 1.0 + 0.033 * math.cos(2.0 * math.pi / 365.0 * day_of_year)
        decl = 0.409 * math.sin(2.0 * math.pi / 365.0 * day_of_year - 1.39)
        # Sunset hour angle (eq. 25), guarded for polar day/night.
        cos_ws = -math.tan(phi) * math.tan(decl)
        ws = math.acos(max(-1.0, min(1.0, cos_ws)))
        return (
            (24.0 * 60.0 / math.pi)
            * _SOLAR_CONSTANT_MJ_MIN
            * dr
            * (ws * math.sin(phi) * math.sin(decl) + math.cos(phi) * math.cos(decl) * math.sin(ws))
        )

    @classmethod
    def et0_daily(cls, *, tmax_c: float, tmin_c: float, day_of_year: int, latitude_deg: float) -> float:
        """FAO-56 Hargreaves-Samani reference ET₀ [mm/day]."""
        tmean = (tmax_c + tmin_c) / 2.0
        trange = max(0.0, tmax_c - tmin_c)
        ra_mm = _MJ_TO_MM_EVAP * cls.extraterrestrial_radiation(day_of_year, latitude_deg)
        return max(0.0, 0.0023 * (tmean + 17.8) * math.sqrt(trange) * ra_mm)

    def et_rate(self, inputs: ModelInput) -> float:
        """The Hargreaves ET rate [mm/h] from a :class:`HargreavesStep` (ET₀/24)."""
        if isinstance(inputs, ETStep):
            return self.warmup_rate(inputs)
        if not isinstance(inputs, HargreavesStep):
            raise TypeError(f"HargreavesModel.step expects HargreavesStep, got {type(inputs).__name__}")
        et0 = self.et0_daily(
            tmax_c=inputs.tmax_c,
            tmin_c=inputs.tmin_c,
            day_of_year=inputs.day_of_year,
            latitude_deg=self._latitude_deg,
        )
        return et0 / 24.0


# ── Strategy 2: VWC from a system probe (stateless measurement) ─────────────


class VWCSystemModel(WaterBalanceModel):
    """Deficit read directly from one system soil-moisture probe (stateless).

    ``D = (field_capacity - vwc) · root_depth · 1000``, clamped to ``[0, d_max]``.
    Stateless: every reading recomputes the deficit from the current measurement,
    so there is no drift and no seeding — which is why the interim system-level
    VWC deficit is benign (reference model D5). All zones scale the same current
    reading by their Kc downstream; the frame is shared.
    """

    input_type: ClassVar[type] = VWCReading

    method_id: ClassVar[str] = "vwc_system"

    required_sensors: ClassVar[frozenset[SensorKind]] = frozenset({SensorKind.SOIL_MOISTURE})

    is_stateful: ClassVar[bool] = False

    def __init__(
        self,
        *,
        field_capacity: float = DEFAULT_FIELD_CAPACITY,
        root_depth: float = DEFAULT_ROOT_DEPTH,
        d_max: float = DEFAULT_D_MAX,
    ) -> None:
        """Configure ``field_capacity`` (fraction) and ``root_depth`` (metres)."""
        super().__init__(d_max=d_max)
        self._field_capacity = field_capacity
        self._root_depth = root_depth

    @property
    def reference_frame(self) -> ReferenceFrame:
        """A single system probe is a shared frame across zones."""
        return ReferenceFrame.VWC_SYSTEM

    def step(self, inputs: ModelInput) -> Deficit:
        """Recompute the deficit from a ``VWCReading`` — no accumulated state."""
        if not isinstance(inputs, VWCReading):
            raise TypeError(f"{type(self).__name__}.step expects VWCReading, got {type(inputs).__name__}")
        self._value_mm = _clamp(
            vwc_deficit_mm(inputs.vwc, field_capacity=self._field_capacity, root_depth=self._root_depth),
            0.0,
            self._d_max,
        )
        return self.deficit

    def apply_irrigation(self, delivered_mm: float) -> Deficit:
        """No-op: a stateless probe reflects the wetter soil on its next reading."""
        return self.deficit

    def reset(self) -> Deficit:
        """No-op: there is no accumulated state to clear (the probe is the truth)."""
        return self.deficit


# ── Strategy 3: VWC from a per-zone probe (target, AI-174) ──────────────────


class VWCPerZoneModel(VWCSystemModel):
    """Deficit from a zone's *own* soil-moisture probe (the AI-174 target).

    Same stateless measurement as :class:`VWCSystemModel`, but the frame is
    **per-zone**: each zone measures a different patch of soil, so its deficit is
    not comparable with a sibling's (the ``source`` identity — the probe/zone id
    — guards this in :meth:`Deficit.is_comparable_to`). When this lands, the
    system-level VWC deficit disappears entirely (reference model D5).
    """

    def __init__(
        self,
        *,
        source: str,
        field_capacity: float = DEFAULT_FIELD_CAPACITY,
        root_depth: float = DEFAULT_ROOT_DEPTH,
        d_max: float = DEFAULT_D_MAX,
    ) -> None:
        """Configure a per-zone VWC model; ``source`` identifies the zone/probe frame."""
        super().__init__(field_capacity=field_capacity, root_depth=root_depth, d_max=d_max)
        self._source = source

    @property
    def reference_frame(self) -> ReferenceFrame:
        """A per-zone probe is *not* shared: deficits differ patch by patch."""
        return ReferenceFrame.VWC_PER_ZONE


class DailySolarEnergy:
    """The day's solar energy [MJ/m2/day], accumulated from a flux reading.

    A pyranometer reports **power** — watts per square metre, right now. FAO-56
    works in the day's **energy**, and the two are not the same number in
    different units: taking an instantaneous 66 W/m2 at six in the evening and
    scaling it to a day gives 5.7 MJ, where the day actually delivered four
    times that. Everything downstream inherits the error, and the only symptom
    is a garden watered less than it needs.

    So the flux is integrated over a rolling 24 hours, in the same hourly
    buckets the diurnal range uses: each bucket holds the mean power seen in
    that hour, and the day's energy is their sum times one hour each. Night
    hours contribute zero and are still needed — they are what makes the average
    a day's average rather than a daytime one.
    """

    #: Hours of coverage below which the total understates the day and is not
    #: worth reporting; the caller estimates from the diurnal range instead.
    MIN_COVERAGE_H: ClassVar[int] = 20

    def __init__(self, window_h: int = 24) -> None:
        """Accumulate over the last ``window_h`` hours."""
        self._window_h = window_h
        self._buckets: dict[int, tuple[float, int]] = {}

    def observe(self, hours: float, watts_m2: float) -> None:
        """Record an instantaneous flux [W/m2] seen at ``hours``."""
        index = int(hours)
        total, count = self._buckets.get(index, (0.0, 0))
        self._buckets[index] = (total + max(0.0, watts_m2), count + 1)
        cutoff = index - self._window_h + 1
        for stale in [k for k in self._buckets if k < cutoff]:
            del self._buckets[stale]

    @property
    def coverage_h(self) -> int:
        """How many distinct hours the window holds."""
        return len(self._buckets)

    def energy_mj(self) -> float | None:
        """The day's energy [MJ/m2/day], or ``None`` while the window is too thin.

        Each hour contributes its mean power for one hour: W/m2 x 3600 s is
        joules per square metre, and a million of those is a megajoule.
        """
        if self.coverage_h < self.MIN_COVERAGE_H:
            return None
        mean_w = sum(total / count for total, count in self._buckets.values())
        return mean_w * 3600.0 / 1_000_000.0


# ── Net radiation: computed, never asked for ───────────────────────────────
#
# FAO-56 does not expect net radiation to be measured. Rn is a *balance* —
# incoming shortwave minus what the surface reflects, minus the net longwave the
# ground exchanges with the sky — and measuring it takes a four-sensor net
# radiometer, a research instrument. What a consumer weather station reports is
# global solar radiation Rs, a pyranometer reading, and FAO-56 derives Rn from
# it (eq. 38-40). So the integration asks for Rs and computes the rest.

#: Albedo of the FAO-56 reference crop (clipped grass): the fraction of incoming
#: shortwave the surface sends straight back.
_REFERENCE_ALBEDO: float = 0.23

#: Stefan-Boltzmann constant [MJ/K^4/m^2/day], for the longwave term.
_STEFAN_BOLTZMANN: float = 4.903e-9

#: Fraction of extraterrestrial radiation reaching the ground on a clear day
#: (FAO-56 eq. 37 at sea level). Turns Ra into Rso, which is what tells a bright
#: day from a dull one when Rs is measured.
_CLEAR_SKY_FRACTION: float = 0.75

#: Adjustment coefficient for estimating Rs from the diurnal range (FAO-56
#: eq. 50). 0.16 is the interior value; coastal sites run nearer 0.19, and the
#: difference is smaller than the error of having no measurement at all.
_KRS_INTERIOR: float = 0.16

#: W/m2 -> MJ/m2/day. A pyranometer reports an instantaneous flux; the equations
#: work in daily energy, and 86400 seconds over a million joules is the bridge.
W_M2_TO_MJ_DAY: float = 0.0864


def solar_radiation_from_range(ra_mj: float, tmax_c: float, tmin_c: float) -> float:
    """Estimate Rs [MJ/m2/day] from the diurnal range — FAO-56 eq. 50.

    The fallback for a site with no pyranometer: a wide range means the ground
    both heated and cooled freely, which is what a clear sky looks like from the
    ground. Same physical reasoning as the Hargreaves term, used here to produce
    a radiation rather than an evapotranspiration.
    """
    return _KRS_INTERIOR * math.sqrt(max(0.0, tmax_c - tmin_c)) * ra_mj


def wind_at_2m(speed_m_s: float, height_m: float) -> float:
    """Convert a wind speed measured at ``height_m`` to the 2 m value — FAO-56 eq. 47.

    The equation is defined for wind at two metres above a grass surface, and a
    station on a mast reads faster air: a 10 m reading is about three quarters of
    itself once brought down. Left unconverted it inflates the aerodynamic term,
    and the error is systematic rather than noisy — the same direction every hour
    of every day.
    """
    if height_m <= 0 or abs(height_m - 2.0) < 1e-9:
        return speed_m_s
    return speed_m_s * 4.87 / math.log(67.8 * height_m - 5.42)


def net_radiation_mj(
    *,
    solar_mj: float,
    ra_mj: float,
    tmax_c: float,
    tmin_c: float,
    rh_pct: float,
) -> float:
    """Net radiation Rn [MJ/m2/day] from measured or estimated Rs — FAO-56 eq. 38-40.

    Two halves. The shortwave one is what the surface keeps of what arrives,
    ``(1 - albedo) * Rs``. The longwave one is what it loses to the sky, and it
    is not a constant: it grows on dry air, because water vapour is what sends
    the ground's heat back, and it grows on clear nights, which is why the ratio
    of measured to clear-sky radiation appears in it.

    ``rh_pct`` enters through the actual vapour pressure; ``ra_mj`` (astronomy,
    from latitude and date) sets the clear-sky reference the measurement is
    judged against.
    """
    net_shortwave = (1.0 - _REFERENCE_ALBEDO) * solar_mj

    saturation = 0.6108 * math.exp(17.27 * ((tmax_c + tmin_c) / 2.0) / (((tmax_c + tmin_c) / 2.0) + 237.3))
    actual_vapour = saturation * _clamp(rh_pct, 0.0, 100.0) / 100.0

    clear_sky = _CLEAR_SKY_FRACTION * ra_mj
    # FAO-56 defines this ratio over a *day*; here it arrives instantaneous,
    # which breaks at night: Rs is zero, the bracket below turns negative, and
    # the ground appears to *gain* longwave radiation from a colder sky. The
    # factor is therefore floored at zero — the loss can be neutralised, never
    # reversed. The cost is that night-time cooling is not credited; carrying
    # the daytime ratio into the night, as FAO-56 prescribes, needs a daily
    # accumulation of Rs that nothing keeps yet.
    cloudiness = _clamp(solar_mj / clear_sky, 0.0, 1.0) if clear_sky > 0 else 1.0
    sky_factor = max(0.0, 1.35 * cloudiness - 0.35)

    tmax_k4 = (tmax_c + 273.16) ** 4
    tmin_k4 = (tmin_c + 273.16) ** 4
    net_longwave = (
        _STEFAN_BOLTZMANN
        * ((tmax_k4 + tmin_k4) / 2.0)
        * (0.34 - 0.14 * math.sqrt(max(0.0, actual_vapour)))
        * sky_factor
    )
    return net_shortwave - net_longwave


# ── The diurnal range, observed rather than asked for ──────────────────────


class DiurnalRange:
    """The daily temperature extremes, kept from the readings we already take.

    Hargreaves-Samani needs the day's maximum and minimum, and asking the user
    for two more entities is asking them to build with helper templates
    something the integration observes anyway — and inviting the worst version
    of the mistake, since the same entity in both boxes yields a range of zero,
    which the formula turns into an evapotranspiration of exactly zero.

    A rolling 24-hour window rather than a calendar day: it is always available
    once filled, instead of being meaningless until midnight and thin every
    morning. The cost is that the window straddles two dates, which for a
    quantity meant to characterise "a day's weather" is the smaller error.

    Storage is bounded by construction — one bucket per hour, each holding that
    hour's min and max — because the caller observes on every sensor change and
    an unbounded list of readings would grow without limit.
    """

    #: How many distinct hours must be present before the extremes are trusted.
    #: Below this the window is a fragment of a day, and its range understates
    #: the real one — which would understate the water the garden needs.
    MIN_COVERAGE_H: ClassVar[int] = 20

    #: A range this small over a full day is not weather; it is a sensor that
    #: does not see the sky. Reported so the caller can say so rather than
    #: silently producing an evapotranspiration near zero.
    IMPLAUSIBLE_RANGE_C: ClassVar[float] = 2.0

    def __init__(self, window_h: int = 24) -> None:
        """Track extremes over the last ``window_h`` hours."""
        self._window_h = window_h
        self._buckets: dict[int, tuple[float, float]] = {}

    def observe(self, hours: float, temp_c: float) -> None:
        """Record ``temp_c`` seen at ``hours`` (any monotonic hour count)."""
        index = int(hours)
        low, high = self._buckets.get(index, (temp_c, temp_c))
        self._buckets[index] = (min(low, temp_c), max(high, temp_c))
        cutoff = index - self._window_h + 1
        for stale in [k for k in self._buckets if k < cutoff]:
            del self._buckets[stale]

    @property
    def coverage_h(self) -> int:
        """How many distinct hours the window currently holds."""
        return len(self._buckets)

    @property
    def is_ready(self) -> bool:
        """Whether the window covers enough of a day to be worth reading."""
        return self.coverage_h >= self.MIN_COVERAGE_H

    def extremes(self) -> tuple[float, float] | None:
        """``(tmin, tmax)`` over the window, or ``None`` while it is too thin.

        ``None`` rather than a guess: a partial window's range is systematically
        too small, and a too-small range reads as an overcast day. The caller
        freezes the deficit instead, exactly as it does before the temperature
        buffer has enough readings.
        """
        if not self.is_ready:
            return None
        lows = [low for low, _ in self._buckets.values()]
        highs = [high for _, high in self._buckets.values()]
        return min(lows), max(highs)

    def is_implausible(self) -> bool:
        """Whether a full window shows a range too small to be real weather."""
        got = self.extremes()
        return got is not None and (got[1] - got[0]) < self.IMPLAUSIBLE_RANGE_C


# ── The catalogue, and choosing from it ────────────────────────────────────
#
# Two halves of one rule live here. The catalogue is what the integration can
# offer at all; the capability match is which of those a given installation may
# actually pick. Keeping them together is deliberate: a model added to the
# catalogue without declaring what it needs would be offered to everyone, and
# that is precisely the failure the match exists to prevent.

#: Every model the integration can offer, richest first. Order is the tie-break
#: when nobody has expressed a preference: with more sensors declared you get a
#: better estimate without having to ask for it.
MODEL_CATALOGUE: tuple[type[WaterBalanceModel], ...] = (
    VWCSystemModel,
    PenmanMonteithModel,
    HargreavesModel,
    ETModel,
)

#: The input DTOs the integration can actually build today. It is a statement
#: about the *host*, kept here so the catalogue and the constraint on it are
#: read together: ``sensor.py`` produces an :class:`ETStep` from the temperature
#: buffer and a :class:`VWCReading` from the probe, and nothing yet produces the
#: daily extremes Hargreaves needs or the full weather Penman-Monteith needs.
#:
#: Until it grows, a model outside it must not be offered — not in the dropdown
#: and not by the automatic choice, which would otherwise pick the richest
#: *declared* model and then raise on every reading. Widening this set is the
#: last step of wiring a tier, not the first.
RUNNABLE_INPUTS: frozenset[type] = frozenset({ETStep, HargreavesStep, PenmanStep, VWCReading})


def models_offered_by(env) -> tuple[type[WaterBalanceModel], ...]:
    """The models this installation may choose, richest first.

    The whole rule is ``env.satisfies(model.required_sensors)``. A site with a
    thermometer alone gets one option; adding a humidity, wind and radiation
    sensor unlocks Penman-Monteith without touching any code.

    Takes the site rather than a set of sensor kinds so the caller cannot
    accidentally ask the question against a stale snapshot of the bindings.

    Two conditions, not one: the sensors must be declared **and** the host must
    be able to build the model's input. The second is what stops a site that
    declares humidity, wind and radiation from being handed Penman-Monteith by
    the automatic choice and crashing on its first reading.
    """
    return tuple(
        model
        for model in MODEL_CATALOGUE
        if model.input_type in RUNNABLE_INPUTS and env.satisfies(model.required_sensors)
    )


def model_by_id(method_id: str) -> type[WaterBalanceModel] | None:
    """The catalogue entry with this identifier, or ``None`` if unknown."""
    return next((m for m in MODEL_CATALOGUE if m.method_id == method_id), None)


def build_model(
    env,
    *,
    method_id: str | None = None,
    diurnal_range_c: float | None = None,
    alpha: float = DEFAULT_ALPHA,
    t_base: float = DEFAULT_T_BASE,
    d_max: float = DEFAULT_D_MAX,
    field_capacity: float = DEFAULT_FIELD_CAPACITY,
    root_depth: float = DEFAULT_ROOT_DEPTH,
    kc: float = DEFAULT_KC,
) -> WaterBalanceModel:
    """Build the water-balance model this site should run.

    ``diurnal_range_c`` is the range actually observed, when it is known. It is
    evidence, and it is used **only** to narrow the automatic choice: an explicit
    choice is always honoured, because a user who names a method is asserting
    something the statistics cannot see — a sensor about to be moved, a site
    where the flatness is real.

    ``method_id`` is the user's choice when they have made one. It is honoured
    only if the site still satisfies it: a sensor can be removed after the
    choice was stored, and silently running a model whose inputs are missing
    would produce a confident wrong number. In that case the richest satisfied
    model is used instead — degrading, not failing, because irrigation must
    keep working.

    With no choice stored, the richest satisfied model wins, which preserves
    today's behaviour exactly: a site with only a thermometer gets
    :class:`ETModel`, and one with a soil probe gets the VWC model that already
    bypassed ET.
    """
    offered = models_offered_by(env)
    chosen = model_by_id(method_id) if method_id else None
    if chosen is None or chosen not in offered:
        automatic = offered
        if diurnal_range_c is not None and diurnal_range_c < DiurnalRange.IMPLAUSIBLE_RANGE_C:
            # A day that never warms and never cools is not weather; it is a
            # thermometer that does not see the sky. A tier reading the diurnal
            # range would take that flatness for permanent overcast and
            # understate the water needed, every hour, invisibly. Those tiers
            # are withdrawn from the **automatic** choice only — never from an
            # explicit one, which is why this narrowing lives inside this branch
            # and not above it. A user who names a method is asserting something
            # the statistic cannot see.
            automatic = tuple(m for m in automatic if m.input_type is not HargreavesStep)
        # Automatic means the best the declared sensors support, for an upgrade
        # exactly as for a fresh install. The alternative — pinning existing
        # gardens to what they happened to be running — makes "automatic" mean
        # "whatever you had", which is not a promise anyone would ask for.
        #
        # It does mean the number moves on upgrade for a site that declared more
        # than the simple tier needs. That is why the running method is
        # published as an entity and logged at startup: a change the user can
        # see is a different thing from a change that happens in silence.
        chosen = automatic[0] if automatic else ETModel
    if issubclass(chosen, VWCSystemModel):
        return chosen(field_capacity=field_capacity, root_depth=root_depth, d_max=d_max)
    if issubclass(chosen, ETModel):
        return chosen(alpha=alpha, t_base=t_base, kc=kc, d_max=d_max)
    if issubclass(chosen, HargreavesModel):
        # The astronomical radiation term is a function of *where you are*, and
        # the site knows it. Hargreaves is the one tier whose constructor needs
        # something from the environment beyond the sensors it reads.
        return chosen(latitude_deg=env.latitude, kc=kc, d_max=d_max, alpha=alpha, t_base=t_base)
    return chosen(kc=kc, d_max=d_max, alpha=alpha, t_base=t_base)
