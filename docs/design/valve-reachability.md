# Valve reachability — noticing a valve that has stopped answering

**Status:** Accepted (ADR), 2026-08-18
Implementation: `environment.py` (the criterion), `reachability_watch.py` (the
measurement), `controller.py` (`_check_reachability`, hourly), `sensor.py`
(`valve_reachable`)
Tests: `tests/test_silence_judgement.py`, `tests/test_valve_reachability.py`,
`tests/test_reachability_watch.py`

## The failure this is about

A battery-powered valve runs out of charge in the middle of the season. It stops
answering. The zone stops being watered. And **nothing says so**:

- the switch entity keeps reporting a perfectly ordinary `off`;
- the battery sensor keeps showing its last reading, which was fine;
- the coordinator does not mark it unavailable, because the availability timeout
  it applies to a battery device is measured in *days* — a sleeping valve is
  supposed to be quiet;
- the zone's deficit keeps rising, which looks exactly like a dry spell.

The plants find out first. This was reproduced on a live installation
(Zigbee2MQTT 2.13, availability enabled): a valve known to be off the mesh was
indistinguishable from three healthy siblings on every signal Home Assistant
exposes.

There is a second, milder version of the same failure: the user presses
*Irrigate* and, apparently, nothing happens. Underneath, the operator retries six
times over about fifty seconds and then blocks the zone. That one is now visible
too — see *Two signals* below.

## Which entity does NeverDry even look at?

Before asking *what* to measure, there is the question of *where*. In another
installation NeverDry knows only what the user typed into the config flow:

| Field | Required | |
|---|---|---|
| `valve` | **yes**, for any zone that irrigates | the valve entity, `switch.*` or `valve.*` |
| `battery_sensor` | no | |
| `flow_meter_sensor` | no | |

Everything else has to be **derived**, and it can be, using only Home Assistant
core — no knowledge of Sonoff, Zigbee or any integration:

```
zone.valve (entity_id) → entity registry → device_id
                       → device registry → every entity of that device
```

The point is that we never need to know *what* those entities are. We are not
hunting for "the battery" or "the link quality": **any** entity of that device
reporting is proof the device is on the mesh. The union is the signal; its
members are irrelevant. This is the same discovery `_discover_hw_max_duration`
already uses to find the on-device timer.

It degrades cleanly:

- **an entity with no device** (template or YAML switches) — no `device_id`, so
  the union is the configured entity alone. Less signal, no error;
- **a device with one entity** — same, with no special case to write;
- **a multi-valve controller** — and this one is a real limit, not a degradation.
  Four zones on one physical controller share one device, so they share one
  union and therefore one freshness *by construction*. The sibling comparison
  says nothing per valve; at best it says whether the controller is alive. A
  large share of installations are like this (see the reporter on
  [#173](https://github.com/never-dry/NeverDry/issues/173), whose four zones sit
  behind one `drip_controller`). Written down here rather than discovered by a
  user.

For the cases where derivation is not enough, the escape is an optional per-zone
*availability entity* binding — `Driver` already takes `availability_entity`,
today not configurable anywhere. Derive first, ask only when derivation fails;
never the other way round.

## Why one valve cannot be asked

Every direct question has an unusable answer:

| Question | Why it fails |
|---|---|
| Is the entity `unavailable`? | Eventually, yes — but the timeout a coordinator applies to a *battery* device is measured in days, so not on the timescale of tonight's watering. |
| Has it been quiet for more than N minutes? | Every mesh has its own cadence; no single N suits a chatty valve and a sleepy one, and asking the user to pick it is asking them to guess. |
| Is `last_seen` set? | Zigbee2MQTT 2.x publishes it to MQTT but nothing surfaces it in Home Assistant: entities carry no raw payload attributes and discovery creates no `last_seen` sensor. It would also require every user to change a Zigbee2MQTT default, would be undetectable when they had not, and would leave ZHA, Matter and Wi-Fi valves with nothing. |

What *is* available on every entity, in every integration, with nothing to
enable: **`last_reported`** — when Home Assistant last received a state write,
whether or not the value changed. That is the raw material.

## What the measurements actually said

Four attempts on the live instance before the data was read correctly. All four
failed the same way — **aggregating entities that measure something else** — and
they are recorded here so the dead ends are not walked again.

1. **"The recorder cannot give us cadence."** Measured `switch.*` and
   `sensor.*_battery` only: 15–20 points in 24 h, clustered around our own
   restarts. Concluded the recorder stores nothing useful. Wrong — those are two
   entities out of twenty-one, and the only two that almost never change.
2. **"The valve is alive, it reports every two minutes."** Swept everything
   matching the zone name: ~2000 points, median gap 2 min. Wrong — the traffic
   was **NeverDry's own sensors** (`duration`, `volume`, `deficit`), driven by our
   ET broadcast. They live on the NeverDry *zone* device, not the manufacturer's,
   so filtering by device rather than by name excludes them by construction.
3. **"The quiet valve talks a third as often as its siblings."** 15 distinct
   contact moments against 53. Wrong — the busy zones had simply *irrigated* more,
   moving their irrigation counters. That measured watering activity, not mesh
   health.
4. **"The floor is `median × 3`."** Wrong for this data. Reporting is **bursty**:
   several messages a minute apart while the device is awake, then a long sleep.
   Measured across the fleet: median gap **1 minute**, longest legitimate silence
   **9 to 16 hours**. A floor on the median is three minutes and calls every
   sleeping valve dead. The floor has to come from the upper tail.

Two findings survived, both from the configured switch alone:

- it **does** go `unavailable` sometimes, so the coordinator's availability
  tracking is real — just slow;
- over 24 h the failing valve **never went `on`**, while its three siblings did.
  A zone whose valve has not opened in a day, in watering season, is a signal in
  its own right, needs only the mandatory entity, and is free from the recorder.

## The rule

Ask a different question. Not *has this valve been quiet too long*, but **is this
valve unusually quiet compared to its siblings**.

```
reference = median(silence of the OTHER valves)
spread    = MAD(silence of the OTHER valves)
threshold = max(reference + k·spread, floor)
silent    = silence > threshold
```

with `k = 3` and the floor derived from observed cadence rather than set in
minutes: a high quantile (p95, nearest-rank) of the intervals actually seen
between messages.

Three properties follow from the shape, without special cases:

- **It self-calibrates.** When the whole mesh goes quiet at night the reference
  moves with it, so nobody is accused.
- **A restart accuses nobody.** Everything is fresh together, so the reference is
  tiny and the floor holds the line. The startup false positive is answered by
  the rule rather than by an exception to it.
- **A coordinator outage accuses nobody.** All silences rise together. That is
  correct: it is not a fault *of a valve*, and the bridge reports itself.

### The subject is left out of its own reference

Not a detail. With two valves and the dead one included, the median sits halfway
between healthy and dead: the dead valve drags up its own reference and acquits
itself. Leaving it out keeps the reference honest at any fleet size, and it is
what makes the wild-sibling case work — a valve that has just rejoined after a
week away would otherwise blow the bar wide open and hide a real fault.

### Why the bar has two parts

They answer different questions, and both are needed.

`reference + k·MAD` asks *is this unusual for this fleet*. It widens on its own
when the fleet's cadence is genuinely irregular, which a flat multiplier cannot
do: a fleet reporting every 10 min ± 1 and one reporting every 10 min ± 8
deserve different bars.

The floor asks *is this unusual at all*. On a tight, freshly-restarted fleet the
MAD is zero and the bar would collapse onto the median, making ordinary jitter
look like a fault.

## Three values, not a boolean

`LIVE` / `SILENT` / `UNKNOWN`. "We cannot tell" is a real and frequent answer —
one zone configured, or a fleet too small to compare — and collapsing it into
"fine" is how a warning system loses its meaning. Absence of evidence is not
evidence of absence.

The verdict carries the numbers it was reached from (`reference_s`,
`threshold_s`) so the warning can explain itself: *"quiet for three hours while
the others last spoke four minutes ago"* is actionable; *"not responding"* alone
is not.

## Estimators considered

Measured against the same set of cases before choosing. ✅ = correct verdict.

| Case | Fixed threshold | Tukey `Q3 + 1.5·IQR` | `median + 3·MAD` |
|---|---|---|---|
| One dead of four (3 h / 1 h / 40 min) | depends on N | ✅ | ✅ |
| Two dead of four | depends on N | ❌ missed | ✅ |
| Three valves, one dead | depends on N | ❌ n < 4 | ✅ |
| Two valves, one dead | depends on N | ❌ n < 4 | ✅ |
| One sibling wild (rejoined after a week) | depends on N | ❌ missed | ✅ |
| All fresh after a restart | ❌ false positives | ✅ | ✅ |
| Whole mesh down | ❌ false positives | ✅ | ✅ |

**Tukey's fence** is the better-known and better-principled definition of an
outlier, and it was the preferred candidate. It lost on sample size: quartiles
over the three peers of a four-zone garden are interpolations between two
numbers, and the fence is undefined below four points. It is also the estimator
that the wild sibling defeats — the outlier inflates the IQR and the fence opens
wide enough to swallow the fault it was meant to catch. **Worth revisiting for
installations with ten or more zones**, where the sample supports it.

**Mean + k·σ** was rejected for the peer comparison for the same reason the mean
is always wrong here: the thing being detected *is* an outlier, so an estimator a
single wild value can inflate will hide it. The MAD is its robust counterpart and
keeps the intuition — level plus dispersion — intact.

The floor is a **high quantile** of observed intervals (nearest-rank, so the
threshold is a value that genuinely occurred) rather than a multiple of any
central estimator. It answers a different question — "how long is quiet still
normal" — and on bursty reporting the middle describes the burst, not the sleep.
Below twenty observations a quantile is a fiction and the longest silence
actually seen is used instead.

## Passive is a backstop; the probe is the answer

The measurements above settle the ambition. Legitimate silence on this fleet
reaches **9 to 16 hours**. To avoid false positives the floor has to sit up
there, and by then a valve that died in the morning has already missed the
evening watering. Passive silence therefore answers a slower question — *this
valve has been gone for more than a day* — which is worth saying but is not the
question that matters.

The question that matters is *will this valve answer tonight*, and the only
portable way to answer it is to **ask**:

- a few minutes before a scheduled run, send an idempotent command —
  `switch.turn_off` on an already-closed valve is a round trip on the radio and
  no physical action;
- watch whether **anything in the device's union** reports within a few seconds;
- answered → live; nothing → warn *now*, while there is still time to act.

The union does double duty: it is how the entity is found and how the probe is
confirmed. And it is battery-friendly, because it wakes a valve a few times a
day rather than every few minutes — which is exactly why a periodic liveness
poll is the wrong shape and a pre-run probe is the right one.

`Driver.async_ping` is where this belongs; it exists and today falls back to
reading the actuator's own state, which is the same blind check that fails here.

## Honest limits

- **A majority quiet hides them all.** Once the silent valves are more than half
  the fleet they *are* the reference. A relative measure cannot do better; only
  an absolute floor low enough to be noisy would catch it. Covered by a test so
  the limit stays visible.
- **Fleets below three valves** fall back to the floor alone, and a single-zone
  installation returns `UNKNOWN` for ever. This is the honest answer, not a
  degradation: with nothing to compare against there is no measurement.
- **`last_reported` is not strictly "the device spoke".** It also advances when
  a coordinator republishes cached state on restart. The relative rule absorbs
  this: everyone resets together.

## Two signals, two budgets

Reachability is reported through two channels on purpose, with different costs
to the user.

**Ambient** — the amber warning on the zone card. Always visible, interrupts
nothing, costs nothing to be wrong about. This is where a suspicion belongs.

**Interruptive** — a notification. Rare by design, because *an alert repeated
about a fault nobody is fixing is how the alert that matters gets ignored*. The
policy:

- raised **once per episode** (`UNREACHABLE_PASSIVE`); `ValveNotifier`
  deduplicates on `(zone, kind)`, so a persisting fault does not re-notify;
- **cleared automatically on recovery**, so it never becomes a stale message the
  user has to dismiss;
- spoken again only when the silence **actually costs a watering** — a scheduled
  run skipped because the valve will not answer (`UNREACHABLE_AT_IRRIGATION`).
  One message per missed watering: bounded, and proportional to the harm.

Escalation is by *consequence*, not by elapsed time. A valve nobody is going to
fix until the weekend should not generate a reminder every hour; it should
generate one when a watering is actually lost.

## Where the runtime number comes from

`judge_fleet()` takes a mapping of actuator → seconds of silence. The number is
the **driver's** to supply: it is the only layer that knows which entity backs a
given actuator and when that entity last reported. The judgement is the
**site's**, because no valve can judge itself — the comparison *is* the
measurement.

That seam is why the rule lives in `environment.py` and is pure: it can be
exercised over lists of numbers, including every failure shape above, without a
Home Assistant runtime.

## The measurement, as built

`reachability_watch.py` supplies the numbers the judge consumes. It is
deliberately the only part that touches Home Assistant.

**The number stays the driver's.** `Driver.silence_s()` resolves its own device
through the registries — valve entity → `device_id` → every entity of that
device — and answers how long the union has been quiet. The members are never
inspected. A valve whose entity has no device degrades to watching itself: less
signal, no error, no special case. `reachability_watch.py` only collects those
numbers and hands the fleet to the judge, which keeps the split this note
argued for: the measurement is the driver's, the judgement is the site's,
because no valve can judge itself.

**The floor is learned rather than configured.** Each hourly tick records how
long a device has been quiet; when the device finally reports, that peak becomes
one sample of *silence that ended*, which is the only honest evidence of what
"normally quiet" looks like on this mesh. Until at least one silence has ended,
every verdict is `UNKNOWN`: with no observed cadence there is nothing to derive
a floor from, and judging would be guessing.

**Hourly, not tighter.** What is being detected is measured in hours — the floor
observed on live fleets sits between 9 and 16 — so a faster poll would spend
more to reach the same answer.

### One correction to the notification policy above

The policy section says a persisting fault does not re-notify because
`ValveNotifier` deduplicates on `(zone, kind)`. That is true only when the
*context* is also unchanged, and the passive warning carries the silence
duration in its context — which grows at every tick. Deduplication would
therefore never fire, and the warning would repeat hourly.

The controller holds a **24-hour quiet period per valve** for this reason. It is
not redundancy with the notifier: it is what makes the stated policy — *"raised
once per episode"* — actually true. Escalation by consequence
(`UNREACHABLE_AT_IRRIGATION` when a watering is genuinely lost) is unchanged.

### The verdict never blocks watering

A valve judged silent is still commanded when its time comes. The judgement is
evidence about the mesh, not proof about the valve, and refusing to try on
statistical grounds would turn a warning into an outage. It changes what the
user is *told*, never what the system *does*.

## Confirmed in the field, 2026-08-18

The shape the rule was designed for occurred on the reference installation, and
this is what moved the note from Proposed to Accepted.

Two of four valves (`Giardino Pino`, `Giardino Melograno`) were off the Zigbee
mesh — confirmed by the owner, who could not reach them from Zigbee2MQTT either.
The other two (`Melino`, `Ortensia`) answered normally.

Every direct signal called all four healthy:

| Signal | Dead valves | Healthy valves |
|---|---|---|
| `switch.*` state | `off` | `off` |
| entity `unavailable`? | no | no |
| battery sensor | 100% | 100% |
| `valve_reachable` (before this work) | `True` | `True` |
| notifications | none | none |

The only signal that separated them was the one this note argues for: the
freshest `last_reported` across each device's entities — 16:14:29 for both dead
valves against 16:43 for both healthy ones, a gap that widened for as long as
they stayed off the mesh.

It also confirmed dead end #1 concretely: Zigbee2MQTT availability was enabled
throughout, and never marked either device unavailable.

Two failures of the *active* evidence path were recorded the same day and are
worth keeping: a valve that had failed six commands correctly read
`valve_reachable: False` — and then `reset_valve` cleared `last_failure`,
returning the zone to "healthy" while the valve was still dead. The reset clears
the symptom along with the state. The comparative signal does not have this
weakness, because it is re-derived from the mesh at every tick.
