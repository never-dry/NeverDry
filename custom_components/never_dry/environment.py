"""The site — what the installation *has*, and what it can therefore compute.

This module materializes the object the RFC in ``docs/design_domain_object_model.md``
calls :class:`Environment`: the user's declared answer to "which sensors do you
have?", plus the shared quantities that belong to the sky rather than to any one
zone.

It replaces the object previously called **System**, which was a catch-all
bundling three unrelated responsibilities. The RFC dissolves it rather than
renaming it, redistributing what it held:

===========================  ==================================================
System attribute (before)    Now lives in
===========================  ==================================================
temperature + rain sensors   :class:`Environment` — environmental feeds
alpha (ET sensitivity)       ``ETModel`` — used *only* by the simple ET tier
D_max (deficit clamp)        ``Zone`` — the value is the zone's soil reservoir;
                             only the clamping *mechanism* is shared
master valve / pump          ``MasterDriver`` — a hydraulics concern
===========================  ==================================================

**Capability matching** is the reason this object earns its place. Each
water-balance model declares the sensors it requires; a zone may offer only the
models whose requirements this site satisfies::

    Environment.declared_sensors  >=  model.required_sensors   =>  model offered

So a user with a thermometer gets the simple ET tier; add humidity, wind and net
radiation and Penman-Monteith unlocks; add a soil probe and VWC becomes
available — with no model selectable by hand that the hardware cannot feed.

Design intent — this module is deliberately **pure**: no Home Assistant import,
no I/O. It holds *bindings* (entity ids as opaque strings) and the rules about
them, never the readings; resolving a binding to a value is the integration's
job. Same choice as ``water_balance_model.py``, for the same reason: the rules
are trivially testable when nothing has to be mocked.

**Wiring status — the site is this object.** ``DrynessIndexSensor`` holds an
:class:`Environment` and its bindings, backfill window, latitude and yearly rain
are views onto it; the roll-over of the yearly total lives here alone, where it
can be tested without a Home Assistant runtime.

What has *not* moved is :meth:`satisfies` — capability matching has no caller
yet, because nothing offers the user a choice of water-balance model. It becomes
reachable when the tiers do.

References: ``docs/design_domain_object_model.md`` (RFC: dissolve ``System``),
``docs/design_water_balance_reference_model.md`` (D3, yearly rain as a shared
quantity), GH #146 (site exposure, the per-zone counterpart to this object).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from math import ceil
from statistics import median

# Defaults mirror ``const.py`` so an Environment built with no overrides behaves
# exactly like today's system sensor. Kept as module constants (not a HA import)
# to keep the module pure; the integration passes the user-configured values in.
DEFAULT_LATITUDE: float = 45.0
DEFAULT_BACKFILL_DAYS: int = 90
DEFAULT_RAIN_DELAY_THRESHOLD: float = 0.60
DEFAULT_RAIN_DELAY_HOURS: float = 12.0

# ── Silence judgement: how quiet is too quiet ───────────────────────────────
#: Multiples of the peers' MAD allowed above their median before a valve is
#: called silent. Three mirrors the 3-sigma rule ``valve_latency`` already uses
#: for command latency — same idea, robust estimator.
DEFAULT_SILENCE_K: float = 3.0
#: Fewer peers than this and there is nothing to compare against. Two is the
#: real minimum: with one peer the "median" is that peer, which is still a
#: usable reference, but with none there is no reference at all.
DEFAULT_MIN_PEERS: int = 2
#: The floor is derived from how often the fleet normally speaks, not set in
#: minutes: a mesh that reports every 30 s and one that reports every two hours
#: deserve different floors, and neither should have to be configured.
#: It comes from the *upper tail* of observed intervals rather than the middle,
#: because real reporting is bursty: measured on a live Zigbee fleet, the median
#: gap between messages was 1 minute and the longest legitimate silence between
#: bursts was 9 to 16 hours. A floor built on the median would be three minutes
#: and would fire continuously.
DEFAULT_FLOOR_QUANTILE: float = 0.95
#: Below this many observations a quantile is a fiction; the longest silence
#: actually seen is the honest answer.
MIN_INTERVALS_FOR_QUANTILE: int = 20


class SensorKind(StrEnum):
    """A kind of environmental input a water-balance model may require.

    Deliberately about the *quantity*, not the entity: two thermometers are one
    ``TEMPERATURE`` capability. This is the vocabulary both sides of the
    capability match are written in.
    """

    TEMPERATURE = "temperature"
    RAIN = "rain"
    HUMIDITY = "humidity"
    WIND_SPEED = "wind_speed"
    NET_RADIATION = "net_radiation"
    TEMP_MAX = "temp_max"
    TEMP_MIN = "temp_min"
    SOIL_MOISTURE = "soil_moisture"
    RAIN_PROBABILITY = "rain_probability"


#: Which :class:`Environment` attribute carries the binding for each sensor kind.
#: Module-level rather than a dataclass field: it describes the class, not an
#: installation, and is identical for every instance.
BINDING_BY_KIND: dict[SensorKind, str] = {
    SensorKind.TEMPERATURE: "temperature_sensor",
    SensorKind.RAIN: "rain_sensor",
    SensorKind.HUMIDITY: "humidity_sensor",
    SensorKind.WIND_SPEED: "wind_speed_sensor",
    SensorKind.NET_RADIATION: "net_radiation_sensor",
    SensorKind.TEMP_MAX: "temp_max_sensor",
    SensorKind.TEMP_MIN: "temp_min_sensor",
    SensorKind.SOIL_MOISTURE: "soil_moisture_sensor",
    SensorKind.RAIN_PROBABILITY: "rain_probability_sensor",
}


class RainSensorType(StrEnum):
    """How the rain binding reports, which decides how a delta is derived.

    Mirrors ``const.py`` — and the values matter, because this scaffold used to
    declare three (``cumulative`` / ``rolling`` / ``event``) against the two the
    integration actually ships. That third distinction was not an omission on
    the shipped side: it was **deliberately removed**.

    Telling a midnight-reset total from a rolling 24-hour window required
    guessing from the shape of the readings, and the guess was wrong in both
    directions — it wiped deficits at 05:00 under clear skies on a rolling
    sensor, and dropped legitimate overnight rain on a true daily total (GH
    #123). The replacement needs no guess: on **any** accumulator, credit only
    the positive increment between readings. A decrease is never precipitation,
    whatever produced it — a reset, a window ageing out, a glitch.

    So there are two ways a sensor reports, not three: the value *is* the rain
    (``EVENT``), or the value accumulates it (``DAILY_TOTAL``, whose name is
    historical — the rule covers every accumulator).

    Carried here because it is a property of the *feed*, not of any zone: it
    says how to read the sensor, not what to do with it.
    """

    EVENT = "event"
    DAILY_TOTAL = "daily_total"


@dataclass(frozen=True)
class RainDelayPolicy:
    """Forecast-driven delay: *the signal*, not the decision.

    The environment supplies "rain is likely"; it never skips a watering itself.
    Whether a given zone honours the delay is the zone's business — an indoor or
    patio zone is unaffected, which is why the gate lives on ``Zone.placement``
    (see the RFC). Keeping the threshold here and the gate there is what stops
    forecast rain and measured rain from ever disagreeing about a zone.
    """

    enabled: bool = False
    probability_threshold: float = DEFAULT_RAIN_DELAY_THRESHOLD
    delay_hours: float = DEFAULT_RAIN_DELAY_HOURS

    def triggers_at(self, probability: float | None) -> bool:
        """``True`` when a forecast probability is high enough to delay watering."""
        if not self.enabled or probability is None:
            return False
        return probability >= self.probability_threshold


@dataclass
class Environment:
    """The declared sensor inventory of one installation, plus the shared sky.

    Holds *bindings* — opaque entity-id strings — never readings. A binding that
    is ``None`` means "the user does not have this sensor", which is exactly the
    input the capability match needs.
    """

    # ── Feeds: what the user declared at install ────────────────────────────
    temperature_sensor: str | None = None
    rain_sensor: str | None = None
    humidity_sensor: str | None = None
    wind_speed_sensor: str | None = None
    net_radiation_sensor: str | None = None
    temp_max_sensor: str | None = None
    temp_min_sensor: str | None = None
    soil_moisture_sensor: str | None = None
    rain_probability_sensor: str | None = None

    # ── How to read them ────────────────────────────────────────────────────
    rain_sensor_type: RainSensorType = RainSensorType.EVENT
    backfill_days: int = DEFAULT_BACKFILL_DAYS

    # ── Site constants ──────────────────────────────────────────────────────
    # Latitude is a property of the place, not of a sensor: Hargreaves needs it
    # for the astronomical radiation term, and the seasonal Kc curve needs it to
    # flip for the southern hemisphere.
    latitude: float = DEFAULT_LATITUDE

    # ── Policy the site offers, that zones consume ──────────────────────────
    rain_delay: RainDelayPolicy = field(default_factory=RainDelayPolicy)

    # ── Shared quantity: one sky over the whole garden ──────────────────────
    # Rain that fell this calendar year [mm]. A site quantity by nature, so every
    # zone mirrors the same figure instead of keeping its own drifting counter
    # (reference model D3). Note the asymmetry with the deficit, which is
    # emphatically *not* shared: rain falls on the garden, deficit belongs to a
    # patch of soil.
    yearly_rain_mm: float = 0.0
    yearly_rain_year: int | None = None

    # ── Capability matching ─────────────────────────────────────────────────

    @property
    def declared_sensors(self) -> frozenset[SensorKind]:
        """Every :class:`SensorKind` the user actually bound to an entity."""
        return frozenset(kind for kind, attr in BINDING_BY_KIND.items() if getattr(self, attr) is not None)

    def binding_for(self, kind: SensorKind) -> str | None:
        """The entity id bound to ``kind``, or ``None`` when undeclared."""
        return getattr(self, BINDING_BY_KIND[kind], None)

    def satisfies(self, required: frozenset[SensorKind] | set[SensorKind]) -> bool:
        """``True`` when this site declares everything ``required`` asks for.

        The whole capability rule, in one line: ``declared >= required``.
        """
        return self.declared_sensors >= frozenset(required)

    def missing_for(self, required: frozenset[SensorKind] | set[SensorKind]) -> frozenset[SensorKind]:
        """What ``required`` asks for and this site does not have.

        The complement of :meth:`satisfies`, kept separate because the UI needs
        to say *which* sensor unlocks a model, not merely that one is missing.
        """
        return frozenset(required) - self.declared_sensors

    # ── Shared-rain bookkeeping ─────────────────────────────────────────────

    def accrue_yearly_rain(self, rain_mm: float, *, year: int) -> Environment:
        """Return a copy with ``rain_mm`` added to the yearly total.

        Rolls over when ``year`` differs from the stored one. Credits only
        positive increments: a decreasing reading is never rain (GH #123), and
        that rule belongs with the feed rather than with each consumer.
        """
        if rain_mm <= 0 and self.yearly_rain_year == year:
            return self
        if self.yearly_rain_year != year:
            return replace(self, yearly_rain_mm=max(0.0, rain_mm), yearly_rain_year=year)
        return replace(self, yearly_rain_mm=self.yearly_rain_mm + rain_mm)

    def reset_yearly_rain(self, *, year: int) -> Environment:
        """Return a copy with the yearly rain total cleared (user-invoked reset)."""
        return replace(self, yearly_rain_mm=0.0, yearly_rain_year=year)

    def yearly_rain_liters(self, area_m2: float) -> float:
        """Project the yearly rain onto an area: 1 mm over 1 m² is 1 litre."""
        return self.yearly_rain_mm * area_m2


# ── Is this valve unusually quiet? ──────────────────────────────────────────
#
# The case this exists for is a battery that dies mid-season. The valve stops
# answering, the zone stops being watered, and nothing says so: the switch keeps
# reporting a perfectly ordinary "off", the battery sensor keeps showing its
# last reading, and the availability timeout a Zigbee coordinator applies to a
# battery device is measured in *days*, because a sleeping valve is supposed to
# be quiet. The plants find out first.
#
# So the question cannot be answered by looking at one valve. It can be answered
# by looking at the others: "has this one been quiet for N minutes" needs an N
# nobody can choose well, while "is this one unusually quiet compared to its
# siblings" needs no N at all and self-calibrates. When the whole mesh goes
# quiet at night the reference moves with it, and right after a restart
# everything is fresh together, so nobody is accused.
#
# This lives at site level rather than on the zone because no valve can judge
# itself — the comparison *is* the measurement.
#
# See ``docs/design/valve-reachability.md`` for the alternatives that were
# measured and rejected, with the numbers.


class Reachability(StrEnum):
    """What the silence of one actuator, seen against its peers, tells us.

    Three values rather than a boolean, because "we cannot tell" is a real and
    common answer — one valve configured, or a fleet too small to compare — and
    collapsing it into "fine" is how a warning system loses its meaning. Absence
    of evidence is not evidence of absence.
    """

    LIVE = "live"
    SILENT = "silent"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SilenceVerdict:
    """The judgement on one actuator, with the numbers it was reached from.

    Carries ``reference_s`` and ``threshold_s`` so the reason can be shown
    rather than asserted: "quiet for three hours while the others last spoke
    four minutes ago" is actionable, "not responding" alone is not.
    """

    status: Reachability
    silence_s: float
    reference_s: float | None = None
    threshold_s: float | None = None

    @property
    def is_silent(self) -> bool:
        """``True`` only on a positive finding — ``UNKNOWN`` is not a fault."""
        return self.status is Reachability.SILENT


def silence_floor(
    intervals_s: Sequence[float],
    *,
    quantile: float = DEFAULT_FLOOR_QUANTILE,
    min_observations: int = MIN_INTERVALS_FOR_QUANTILE,
) -> float | None:
    """Derive the "quiet is still normal up to here" floor from observed cadence.

    ``intervals_s`` is how long the fleet actually goes between messages. The
    floor is a **high quantile** of those, not a multiple of the middle, because
    device reporting is bursty rather than periodic: several messages a minute
    apart while the device is awake, then hours of legitimate sleep. Measured on
    a live Zigbee fleet of four valves over 24 h — median gap 1 minute, longest
    gap between bursts 9 to 16 hours. A floor of ``median x 3`` would have been
    three minutes, and would have called every sleeping valve dead.

    Below ``min_observations`` a quantile is a fiction, so the longest silence
    actually observed is used: it is the honest "this much quiet has happened
    and was fine". ``None`` when there is nothing to derive it from.
    """
    usable = sorted(i for i in intervals_s if i > 0)
    if not usable:
        return None
    if len(usable) < min_observations:
        return usable[-1]
    # Nearest-rank percentile: no interpolation, so the value returned is one
    # that genuinely occurred rather than an average of two that did not.
    rank = max(1, ceil(quantile * len(usable)))
    return usable[min(rank, len(usable)) - 1]


def mad(values: Sequence[float]) -> float:
    """Median absolute deviation — the spread of ``values``, robust to outliers.

    Stands to the standard deviation as the median stands to the mean, and that
    is exactly why it is used here: the thing being measured *is* an outlier, so
    an estimator that a single wild value can inflate would hide it.
    """
    if not values:
        return 0.0
    centre = median(values)
    return median([abs(v - centre) for v in values])


def judge_silence(
    silence_s: float,
    peer_silences: Sequence[float],
    *,
    floor_s: float,
    k: float = DEFAULT_SILENCE_K,
    min_peers: int = DEFAULT_MIN_PEERS,
) -> SilenceVerdict:
    """Judge one actuator's silence against its peers': ``median + k·MAD``, floored.

    ``peer_silences`` must **exclude** the actuator being judged. That is not a
    detail: with two valves and the dead one left in, the median sits halfway
    between healthy and dead, so the dead valve drags up its own reference and
    acquits itself. Leaving it out keeps the reference honest however small the
    fleet — and it is what makes the wild-sibling case work, where a valve that
    has just rejoined after a week would otherwise blow the bar wide open.

    The bar has two parts because they answer two questions. ``median + k·MAD``
    asks *is this unusual for this fleet*, and widens on its own when the fleet's
    cadence is genuinely irregular. The floor asks *is this unusual at all*, and
    stops a tight, freshly-restarted fleet — where the MAD is zero and the bar
    would collapse onto the median — from making ordinary jitter look like a
    fault.

    Tukey's fence (``Q3 + 1.5·IQR``) is the better-known criterion and was
    measured against this one. It is more conservative and needs a sample this
    domain does not have: quartiles over the three peers of a four-zone garden
    are interpolations between two numbers. It missed two-dead-of-four and the
    wild sibling, both of which this catches. Worth revisiting at ten zones or
    more; see the design note.
    """
    if len(peer_silences) < min_peers:
        return SilenceVerdict(Reachability.UNKNOWN, silence_s)
    reference = median(peer_silences)
    threshold = max(reference + k * mad(peer_silences), floor_s)
    status = Reachability.SILENT if silence_s > threshold else Reachability.LIVE
    return SilenceVerdict(status, silence_s, reference_s=reference, threshold_s=threshold)


def judge_fleet(
    silences: Mapping[str, float],
    *,
    floor_s: float,
    k: float = DEFAULT_SILENCE_K,
    min_peers: int = DEFAULT_MIN_PEERS,
) -> dict[str, SilenceVerdict]:
    """Judge every actuator against the others, leaving each out of its own reference.

    ``silences`` maps an actuator id to how long it has been quiet, in seconds —
    supplied at runtime by whatever drives that actuator, which is the only
    layer that knows when its entity last reported.

    When the whole mesh falls over, every silence rises together, the reference
    rises with them and nobody is singled out. That is the right answer: it is
    not a fault *of a valve*, and a coordinator that has gone away is its own
    signal, reported elsewhere.
    """
    return {
        name: judge_silence(
            silence,
            [s for other, s in silences.items() if other != name],
            floor_s=floor_s,
            k=k,
            min_peers=min_peers,
        )
        for name, silence in silences.items()
    }
