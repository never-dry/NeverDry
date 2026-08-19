# Three flow rates — design, telemetered, historical

**Status:** Accepted (ADR), 2026-08-18. All three are implemented: the historical
rate is collected from every metered session, the configured field is named
*design flow rate*, and planning prefers history over design.

NeverDry has called one thing "flow rate" while meaning three. They come from
different places, fail in different ways, and are trustworthy for different
questions. Collapsing them into one number is not a naming problem: it is the
root of a family of field defects, from valves reported as stuck open while
they were shut to zones that irrigate 57% of what they should.

| | Where it comes from | Known when | Fails by |
|---|---|---|---|
| **Design** | Sum of the emitters' rated output — sprinklers, drippers, micro-sprayers | Before a drop is delivered | Being optimistic: it ignores head loss, pressure at the end of the line, and clogging |
| **Telemetered** | What the meter reports right now | During the session | Silence that is not zero, coarse quantization, late reporting |
| **Historical** | Median of what past sessions actually delivered | After enough sessions | Being retrospective; needs a meter; drifts with mains pressure |

## Why three and not two

The tempting split is "declared versus real". It is wrong, because *real* has
two faces with opposite properties.

The **telemetered** rate is an instantaneous claim from hardware. It is precise
about the present and useless over short windows, because a counter has a step
size and a reporting cadence it chose, not one we chose.

The **historical** rate is an aggregate over sessions that lasted 15 to 60
minutes. It cannot tell you anything about the run happening now, and it is
robust precisely because it is long: over an hour, even a coarse counter ticks
often enough for quantization to stop mattering.

Treating those two as one grandeur is what produced the defects below. They are
not degrees of the same measurement. They answer different questions.

## The property that separates them: a one-way witness

**The telemetered rate is trustworthy in the positive and not in the negative.**

If the counter moves, water is flowing — that inference is sound. If the
counter does not move, nothing follows: the flow may be below the meter's
limit of detection, or the meter may simply not have reported yet.

This asymmetry is not a subtlety, it is the whole safety argument. Two defects
came from reading silence as evidence of absence; both are fixed, and both are
worth keeping on the record because the reasoning that produced them is easy to
repeat:

- **Open verification.** The FSM waits a fixed 10 s (`valve_fsm.py`,
  `flow_verify_timeout_s`) for flow to appear, then declares `ACTUATION_FAILED`.
  On a meter whose smallest observed step is 28 L, no garden flow rate can move
  the counter in 10 s. The valve opened; the verdict said it did not.
- **Close verification and leak recovery.** A counter that reports late delivers
  the session's last tick *after* the valve is already shut. Read as a rate,
  that late tick says water is still running through a closed valve —
  `CLOSE_LEAK`, then a stuck-open escalation that calls `never_dry.stop` on the
  whole integration. On 2026-08-18 this fired on a valve that HA recorded as
  closed twenty seconds earlier.

The rule the code follows now: **a still meter qualifies an action, it never
refuses one.** Absence of evidence is not evidence of absence.

Concretely: the verification window is derived per zone from
`resolution / flow rate`, and where that exceeds what is still useful as a
guard the check is declared *not applicable* — a distinct FSM event
(`FLOW_UNVERIFIABLE`) that lets the run proceed with flow demoted to observer,
rather than a failure that closes a working valve. On the closing side, a
cumulative counter is judged on whether it is **still climbing** after a second
close, never on whether its total exceeds a threshold.

## What each one is for

| Question | Use | Why not the others |
|---|---|---|
| How much water has this session delivered? | Telemetered, as a **difference of counter readings** over the session | The design rate would return the configuration; a rate integrated over wall-clock inherits every reporting delay |
| How long should this session take? What should the safety timeout be? | Historical, falling back to design | The telemetered rate is not known before the session starts |
| Is this zone healthy? | Historical ÷ design | Neither number means much alone; the ratio is the diagnosis |
| Is water moving right now? | Telemetered, **positive readings only** | See the one-way witness above |
| How much do I deliver when there is no meter at all? | Design | It is the only one that exists |

That last row is the honest tension in the taxonomy, and it is why the design
rate is **required** rather than optional: without it there is no way to turn a
volume into a duration at all, so a zone with no meter and no design rate cannot
water. It is the floor of the system, never the ceiling — the moment a zone has
enough real sessions, `effective_flow_lpm` prefers them, because the catalogue
describes an ideal installation and the history describes this one.

## What the field says

Two months of logs, 220 sessions, four zones (2026-06-17 → 2026-08-18).

**The design rate is optimistic by a large factor.** Zone *Melino* is
configured at 360 L/h. Across 43 sessions that reached their target volume, the
median delivered rate is **205 L/h** — 57% of the design figure. The gap is not
noise: the samples cluster tightly.

**The telemetered rate can be unusable at a zone's real flow.** A supervised
one-minute test on zone *Pino* recorded `smallest step = 28.0, updates = 1`: a
single jump of 28 litres. Any rate computed from that window is an artifact of
the counter's resolution, not a measurement.

**A zone can be broken in a way no rate can express.** Zone *Ortensia* is
configured at 24 L/h. Of its 26 metered sessions, **24 ended at the safety
timeout** having measured a median of 0.8 L/h. Two others recorded 99 and
300 L/h. This is not a flow rate to be learned, and averaging it would produce
a confident number describing nothing. Either water is not reaching the zone or
the meter cannot see it — a distinction that needs a bucket, not more code.

**Estimated-flow sessions cannot measure anything.** In `estimated_flow` mode
the delivered volume is *computed* as configured rate × duration, so recovering
a rate from it returns the configuration. The signature is unmistakable: 25
sessions of *Ortensia* in that mode span 96.5–102.0 L/h. Any learning that
consumes them learns its own input and confirms it forever.

## How the historical rate is measured

One sample per metered session (`session_flow.py`, wired into
`ZoneDriver._deliver_by_volume`):

```
lpm = (meter[close + 30 s] − meter[before open]) / minutes the valve was open
```

Three decisions in that formula, each paid for by a field defect:

- **The reading after the close is delayed by 30 s.** The last tick routinely
  lands after the valve shuts — the same late report that fakes a leak. Sampling
  at the instant of closing quietly loses it. The delay is waiting, not measured
  time, and it is deferred rather than awaited so the session still ends when
  the water stops.
- **The baseline is not the delivery loop's running total.** That one is rebased
  when the counter resets mid-session; sharing it would file the reset as
  delivered water.
- **Sessions under 60 s are refused, not averaged in.** Below that, the
  counter's own resolution dominates. One bad sample is worth less than no
  sample — which is why the window reports a **median** over 20 sessions and
  stays silent below 3.

A supervised valve test is filed into the same series rather than written over
the configured value. It measures water that actually came out, so it is a
sample of the same kind the sessions produce; its value is being *first*, since
a new zone has no history and one supervised run gives the median something to
start from. The earlier design — a button that copied the test result into the
configured rate — destroyed the pair that carries the diagnosis, and is gone.

The learned rate is used for planning (durations, timeout scaling, the credited
estimate when a meter reads zero) and published as the *Measured flow rate*
sensor with `vs_design_pct` beside it. It is never written back into the user's
configuration: adopting a number is the owner's decision, not the integration's.

## Known limits of the historical rate

**The hour of the day is a confounder, and it is not controlled.** Flow depends
on the pressure at the tap, and on mains supply that pressure depends on who
else is drawing water — the neighbourhood in the early evening, a shower
upstairs, another zone still closing. The median is taken over sessions
*regardless of when they ran*, so the hour is a hidden variable folded into the
result.

Two consequences, both real:

- A zone that always waters at 06:00 learns *its 06:00 flow* and calls it the
  zone's flow. That is the right number for planning a 06:00 session and the
  wrong one for judging the installation.
- A zone whose sessions are spread across the day gets a median with real
  variance in it, and that variance is partly the aqueduct, not the zone. It
  will read as noise in the emitters when it is traffic in the pipe.

This matters for installations on **mains water** and much less for those on a
**pump or a tank**, where supply pressure is the installation's own and roughly
constant. The comparison against the design rate inherits the same caveat: a
zone reading 60% of design at 19:00 may read 85% at 04:00 without anything
being wrong with it.

Stratifying the samples by hour would fix this, and is not done: it needs
several times more sessions before any bucket has enough to speak, and the
window would have to grow to cover a season instead of a few weeks. Until then
the honest reading of the number is *"what this zone delivers, at the hours it
usually runs"* — not *"what this zone can deliver"*.

Two smaller limits worth stating:

- **It is retrospective.** It describes past sessions and says nothing about
  the run in progress; a blockage appearing today is only visible after enough
  sessions have carried it into the median.
- **It needs a meter.** Zones in `estimated_flow` produce no samples at all, by
  design — their delivered volume is computed from the configured rate, so
  learning from it would only echo the configuration back.

## Consequences for the configured field

The field is now labelled **Design flow rate** / *Portata di progetto*
(`CONF_ZONE_FLOW_RATE`, `flow_rate_lpm` — the storage key is deliberately
unchanged, since renaming it would force a migration that buys nothing), and is
described as the sum of the zone's emitters.

Its help text used to end with *"measure it with a bucket and a stopwatch"*,
which asked for a measurement while the label asks for a design figure. The
bucket is gone: with the historical rate collected automatically, measuring is
no longer the user's job.

The three quantities are named so they cannot be confused in the UI:

| Sensor | Reads | Was called |
|---|---|---|
| **Design flow rate** | the configured sum of emitters | "Flow rate" |
| **Measured flow rate** | the median of real sessions | (fed only by the last test) |
| **Water meter** | the raw cumulative counter | "Flow meter" |

The last rename matters more than it looks: the entity reports **litres**, not a
rate, and calling it a *flow meter* is what made it plausible to compare its
reading against a rate threshold — the defect behind the false leak reports.

## An observed model of the installation

Three quantities are now learned per valve rather than declared, and each one
replaces a constant that the field had already refuted:

| Learned | Replaces | Used for |
|---|---|---|
| Confirmation latency (`ValveLatencyTracker`) | fixed open/close timeouts | how long to wait for the switch to answer |
| Flow rate (`SessionFlowWindow`) | the design rate, for planning | durations, timeout scaling, credited estimates |
| Meter resolution (`resolution_l`) | the assumption that a counter ticks promptly | the flow-verification window, and whether a test is practicable |

Together they are a measured model of this particular controller, valve and
meter — more accurate than the ratings precisely because it is observed rather
than declared. Every control that used to rest on a constant now rests on a
number the installation produced itself, and each is published so the basis of
a decision is visible when that decision misfires.

The resolution is the newest of the three and the one that costs nothing: every
delivery reveals counter increments, and the smallest ever seen is the meter's
limit of detection. No test is required, though a supervised test contributes
its observed step too.

## Open

- Notifying when historical ÷ design falls below a fraction of the zone's own
  baseline. The ratio is published today; nothing watches it.
- Stratifying the historical samples by hour of day, to remove the mains-supply
  confounder described under *Known limits*.
- Deriving the supervised test's suggested duration from `resolution / flow`
  and offering it in the UI; today the figure is published as an attribute
  (`shortest_useful_test_min`) but the slider does not use it.
