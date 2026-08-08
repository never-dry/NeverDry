# Soil Moisture — Model of Use

**Status:** Draft → Proposed (RFC) → Accepted (ADR). Currently **Draft**.
**Discussion:** [#126](https://github.com/drake69/NeverDry/issues/126)
**Companions:** [`soil-sensors.md`](soil-sensors.md) (the *hardware*: reliability,
selection, placement, wiring), [`scientific-model.md`](scientific-model.md) (the ET
water balance), `design_domain_object_model.md` (where objects live).

`soil-sensors.md` answers *which probe to buy and where to bury it*. This document
answers a different question that we got wrong: **once a probe is in the ground,
what is its reading allowed to mean?**

It is deliberately unfinished. The reasoning below is derived from soil physics
and from reading the code; the parts that need contact with a real garden are
marked **OPEN** and are the subject of the questions in #126. Nothing here should
be implemented before those answers arrive.

---

## 1. The claim: "site-level" is not a physical category

NeverDry currently offers a single `vwc_sensor` configured at **system** level.
Every zone then derives its deficit from that one reading, scaled by its own Kc.

The implicit assumption is that one probe can stand in for the whole garden. It
cannot, and the reason is not statistical — it is that **a probe is always
somewhere**:

- it sits in a specific soil, at a specific depth;
- it sits under specific vegetation, or under none;
- it receives water only when *that spot* is irrigated.

Each of those three facts breaks a different half of the "shared reference" idea.

A useful way to put it: *"site-level" does not describe where the probe is. It
describes the fact that we never asked.* What we have been calling a system-level
probe is a **zone probe whose zone was never declared**.

Contrast this with the atmosphere. A thermometer, a rain gauge, a hygrometer and
an anemometer all measure the air above the whole garden. They have placement
rules — the gauge in the open, the thermometer screened — but those rules exist to
measure the *atmosphere* correctly, not to decide which zone the reading belongs
to. Hence the discriminant:

> **The atmosphere is shared. The soil is not.**
>
> Site level legitimately holds temperature, rain, humidity, wind, radiation,
> latitude and forecast. It does not hold anything that measures soil, because
> soil is always underneath something.

This leaves the `Environment` object of the domain model fully intact. It removes
exactly one thing from its inventory: the soil probe.

## 2. What a probe curve actually contains

A moisture trace is not one signal. It is at least four, on different timescales,
carrying different information:

| Phase | Timescale | What it tells you | Whose property |
|---|---|---|---|
| Infiltration (during and just after watering) | minutes | how fast the soil accepts water; onset of runoff | soil + emitter layout → **cycle/soak** |
| Gravitational drainage | hours, up to ~48 h on clay | the knee of the curve **is** field capacity | soil at the probe |
| Drying, early | days | slope set by **weather × the canopy above the probe** | not the soil |
| Drying, late | — | the curve bends: water release near the wilting point | soil at the probe |

Two consequences that are easy to get backwards, and that we did get backwards:

**Field capacity does not come from the "short" post-irrigation dynamic.** That
short dynamic is infiltration, a different phenomenon. Field capacity is the
plateau of the *drainage* curve — which is its operational definition, not an
analogy: the water content once free drainage has substantially ceased.

**The early drying slope is not a weather signal.** While water is freely
available the slope is set by evapotranspiration, which is weather *modulated by
whatever grows over the probe*. And this is not a scale factor: under bare soil
there is no transpiration at all, only evaporation, which self-limits sharply once
the dry front descends a few centimetres. You cannot divide out a canopy
coefficient and recover the weather.

This is what kills the last defence of a shared probe. Even if we gave up on
sharing the *state* and only shared the *rate*, the rate is still contaminated by
the canopy — so using it for other zones still requires knowing what grows above
the probe, which again means declaring its zone.

## 3. What the implementation does today, and why it is wrong

**OPEN — suspected defect, not yet reproduced on a real installation.**

In VWC mode the per-zone deficit is overwritten unconditionally on every
broadcast (`custom_components/never_dry/sensor.py:1241-1243`):

```python
if dt_h == 0.0 and et_h == 0.0 and rain == 0.0:
    kc = self._get_current_kc()
    self._zone_deficit = self._dryness.deficit * kc
```

After a zone waters, `reset_deficit()` sets `_zone_deficit = 0.0`
(`sensor.py:1383`). On the next probe reading the deficit is written over with
`probe_deficit × Kc`. If the probe is not in that zone it never saw that water, so
the deficit climbs straight back: **the irrigation that just happened is erased
from the model.**

The same broadcast drives the reactive handler, which compares deficit against
threshold (`controller.py:289`). The only guard in that path is
`MIN_SERVICE_INTERVAL_S = 10` (`const.py:187`) — a rate limit against service-call
spam, not an irrigation cooldown.

So under: VWC mode + a zone in reactive mode + a probe not in that zone +
`probe_deficit × Kc` above the zone's threshold, a zone can water repeatedly, once
per probe reading, until the probe's own location happens to get wet.

The root cause is the one from §2: the code takes the probe's **state** and
distributes it, when a shared reading can at most contribute a **rate**, and only
if the canopy above it is known.

## 4. Field capacity should be observed, not declared

Today `field_capacity` and `root_depth` are fixed at 0.30 / 0.30 (`const.py`) and
read in `sensor.py`, with no way to set them from the UI. The obvious fix — expose
a soil-type dropdown — is worth doing for users **without** a probe. For users
*with* one it is the wrong instinct, for a reason that also explains why the fixed
0.30 feels wrong in the field:

```
deficit = (field_capacity − probe_reading) × root_depth × 1000
           ↑ true VWC units               ↑ whatever the probe reports
```

Low-cost capacitive probes do not report true volumetric water content. They
report a raw or loosely-calibrated value. A field capacity taken from a soil-type
table is in real physical units; the probe reading is not. Subtracting one from
the other is a hidden unit mismatch.

If field capacity is instead **read off the same probe's own drainage plateau**,
both terms live in the same units and the calibration error largely cancels.
Observing it is not merely more convenient than declaring it — it is *more
correct*, and increasingly so the cheaper the sensor.

That reframes the soil-type dropdown: a lookup table whose output is
(θ_fc, θ_wp). Where those are observable, the table becomes the **fallback for
probe-less users**, not the source of truth.

Honest limits:

- The drainage knee is only visible in lucky windows — a good soaking followed by
  24–48 h with no rain and no irrigation. Detection must be opportunistic, not
  scheduled.
- The wilting point will rarely be observed at all: an irrigated garden is, by
  design, never allowed to get that dry. Expect θ_fc, not θ_wp.
- A probe reads one depth, and field capacity varies by horizon.

## 5. Contrast: why site exposure was free and this is not

The per-zone site exposure factor ([#146](https://github.com/drake69/NeverDry/issues/146),
merged) is a useful counter-example, and the contrast is what makes the criterion
legible.

Exposure is an attribute of the **Zone**. It multiplies Kc, and therefore collapses
into the single scalar already handed to the water-balance model — the model never
sees it. It corrects a zone's deviation from a quantity (`ET0`) that genuinely *is*
site-level, computed from atmosphere sensors. That is why it composes with every
ET tier without double counting, and why it cost the domain model nothing.

Soil parameters are the opposite: `field_capacity` and `root_depth` are not zone
attributes at all. They are constitutive parameters of the model itself — they
*are* the formula converting a probe reading into millimetres. They live inside
the model object, and the question of whether they can be per-zone is the same
question as where the probe is.

## 6. Consequences, if this holds

1. A soil probe is always declared **against a zone**. The system-level
   `vwc_sensor` field is deprecated rather than preserved for backwards
   compatibility; migration asks the one question the user can answer — *which
   zone is your probe in?*
2. The two VWC model variants (system-wide and per-zone) collapse into one. What
   varies is not the model but the declared location of its probe.
3. A zone with its own probe measures its deficit. A zone without one runs the ET
   model. Their deficits are not comparable across that boundary, which the
   `Deficit` reference-frame rule already enforces.
4. What a *neighbouring* zone may borrow from someone else's probe is at most a
   weather component, and only where the canopy above the probe is known well
   enough to divide out. Whether this is worth building at all is **OPEN**.
5. Field capacity is observed where a probe exists, declared by dropdown where one
   does not.

## 7. What we need from the field

These map to the numbered questions in [#126](https://github.com/drake69/NeverDry/issues/126).
The suspected defect in §3 is confirmed or refuted by direct observation, without
needing a lab reproduction.

| Need | Question | Settles |
|---|---|---|
| Does the deficit climb back after watering? | 6 | §3 |
| Does the same zone water repeatedly in a short window? | 7 | §3 |
| Does the probe react for every zone, or only one? | 8 | §1, §3 |
| Is field capacity understood as *the garden's soil* or *the probe's soil*? | 2 | §4, §5 |
| What told the user 0.30 was wrong? | 1 | §4 (unit-mismatch hypothesis) |
| Is the moisture *curve* watched, or only the number? | 10 | §2 |
| One probe per zone, or one probe informing all? | 11 | §6 — a product question, not a physics one |

## Revision history

| Date | Change |
|---|---|
| 2026-08-08 | Initial draft. Written after the per-zone site exposure review (#147) surfaced the question of where a microclimate correction belongs, which in turn exposed the soil-probe model. Pending field input on #126. |
