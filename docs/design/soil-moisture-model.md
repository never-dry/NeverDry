# Soil Moisture — Model of Use

**Status:** Draft → Proposed (RFC) → Accepted (ADR). Currently **Draft**.
**Discussion:** [#126](https://github.com/never-dry/NeverDry/issues/126)
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

> **First, a distinction this document nearly lost.** `field_capacity` has **two
> unrelated jobs**, and treating them as one is what makes the topic feel bigger
> than it is:
>
> 1. **Probe calibration** — converting a moisture reading into millimetres. This
>    is its *only* use in NeverDry today (`sensor.py:790`, VWC path). Without a
>    probe it does nothing at all. **This section is about job 1.**
> 2. **Sizing a zone's reservoir** — together with root depth it sets how much
>    water a zone can hold and lose before stress (FAO-56's TAW). No probe is
>    involved, and it matters to *every* user. NeverDry does not do this today:
>    `D_max` is a single global value that every zone copies verbatim
>    (`sensor.py:1157`), so sand under shallow turf and clay under deep shrubs
>    get the same reservoir.
>
> Job 2 is a separate, zone-only piece of work. Nothing in this document blocks
> it, and it should not wait on any of the open questions here.

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

## 5. Which model is in charge — **the decision this document exists to force**

Everything above is about *where* a probe is. This section is about something
prior: **who owns the deficit.** Today there is no answer, and that is the actual
source of the confusion.

Configuring a `vwc_sensor` makes VWC *replace* the ET model outright — ET is
bypassed. Two models therefore coexist with no declared relationship: one is
silently switched off by the presence of a config field. That switch is precisely
the mechanism that produces the defect in §3.

**Recommendation: ET is always primary; a probe is a corrector, never an owner.**

1. **ET is the only model that exists for every zone.** It is the universal
   substrate — every zone always has it. Making the primary model depend on
   hardware that most users do not own, and that `soil-sensors.md` argues is
   usually poor hardware, inverts the reliability ordering.
2. **Correction is the one use where the canopy problem disappears.** Comparing
   the probe's drying slope against the ET prediction *for the zone the probe is
   in* — whose Kc is known — compares two estimates of the same spot, so the
   canopy divides out. This is the exact inverse of transplanting the probe's
   state to other zones, where the canopy is what breaks it.
3. **It makes §3 structurally impossible.** If a probe never owns a deficit there
   is nothing to overwrite. That is not a patch; it is a defect that stops being
   expressible.

What the probe does instead, none of which requires owning the deficit:

- calibrate the ET sensitivity `α` against observed drying;
- flag anomalies — a trace that stays flat through an irrigation means a dead or
  badly placed sensor, not a dry garden;
- supply the observed field capacity of §4.

**Not foreclosed:** a user with a good probe *per zone* genuinely holds better
information than any estimate, and should eventually be able to let it own that
zone's deficit — its own, and no other's. That is the advanced, explicitly
declared path, not the default.

**OPEN** — the recommendation above is a design position, not yet a decision.
Question 9 in #126 ("*how dry the garden is right now*" vs "*how fast it is
drying*") asks a real user which mental model they hold, and question 11 asks what
they would actually buy. Both bear directly on this.

## 6. Contrast: why site exposure was free and this is not

The per-zone site exposure factor ([#146](https://github.com/never-dry/NeverDry/issues/146),
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

## 7. Consequences, if this holds

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
5. For **probe calibration** (job 1 of §4): field capacity is observed where a
   probe exists, declared by dropdown where one does not.
6. For **reservoir sizing** (job 2 of §4): soil type and root depth become
   per-zone regardless of any probe, and `D_max` is derived from them rather than
   being one global number. Independent of everything else here.

## 8. What we need from the field

These map to the numbered questions in [#126](https://github.com/never-dry/NeverDry/issues/126).
The suspected defect in §3 is confirmed or refuted by direct observation, without
needing a lab reproduction.

| Need | Question | Settles |
|---|---|---|
| Does the deficit climb back after watering? | 6 | §3 |
| Does the same zone water repeatedly in a short window? | 7 | §3 |
| Does the probe react for every zone, or only one? | 8 | §1, §3 |
| Is field capacity understood as *the garden's soil* or *the probe's soil*? | 2 | §4, §6 |
| What told the user 0.30 was wrong? | 1 | §4 (unit-mismatch hypothesis) |
| Is the moisture *curve* watched, or only the number? | 10 | §2 |
| One probe per zone, or one probe informing all? | 11 | §7 — a product question, not a physics one |

## Revision history

| Date | Change |
|---|---|
| 2026-08-08 | Initial draft. Written after the per-zone site exposure review (#147) surfaced the question of where a microclimate correction belongs, which in turn exposed the soil-probe model. Pending field input on #126. |
