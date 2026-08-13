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

**Phase 1 — mostly inert scaffold.** The models themselves are not wired yet:
``zone.py`` imports the :class:`Deficit` value object and ``sensor.py`` imports
:func:`vwc_to_fraction`, but ``DrynessIndexSensor`` / the per-zone loop still
compute their own deficit. Wiring them onto the models is a deliberate later
phase.
"""

from __future__ import annotations

import abc
import math
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import ClassVar

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

    def __init__(self, *, kc: float = DEFAULT_KC, d_max: float = DEFAULT_D_MAX, initial_mm: float = 0.0) -> None:
        """Configure the crop coefficient ``kc`` and the deficit clamp ``d_max``."""
        super().__init__(d_max=d_max, initial_mm=initial_mm)
        self._kc = kc

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
        super().__init__(kc=kc, d_max=d_max, initial_mm=initial_mm)
        self._alpha = alpha
        self._t_base = t_base

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

    def __init__(
        self,
        *,
        kc: float = DEFAULT_KC,
        pressure_kpa: float = DEFAULT_PRESSURE_KPA,
        d_max: float = DEFAULT_D_MAX,
        initial_mm: float = 0.0,
    ) -> None:
        """Configure ``kc`` and site atmospheric ``pressure_kpa`` (altitude-dependent)."""
        super().__init__(kc=kc, d_max=d_max, initial_mm=initial_mm)
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

    def __init__(
        self,
        *,
        latitude_deg: float,
        kc: float = DEFAULT_KC,
        d_max: float = DEFAULT_D_MAX,
        initial_mm: float = 0.0,
    ) -> None:
        """Configure the site ``latitude_deg`` (drives the astronomical radiation) and ``kc``."""
        super().__init__(kc=kc, d_max=d_max, initial_mm=initial_mm)
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
            (self._field_capacity - inputs.vwc) * self._root_depth * _M_TO_MM,
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
