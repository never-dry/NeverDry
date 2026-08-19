# Valve compatibility — what has actually been tested

**Status:** open register, started 2026-08-17. Contributions wanted (see the end).

This page records **what a given valve really exposes to Home Assistant**, model by
model and *firmware by firmware*. It is not a list of what should work in theory:
every row marked *verified* was read off a running installation.

Why it exists: "does NeverDry support my valve?" turned out to be the wrong
question. Two valves of the same brand, bought from the same shop, expose
different things — and two units of the *same model* with different firmware
expose different things too. The table below is the only honest answer.

## How to read the columns

| Column | What it means |
|---|---|
| **Valve** | The entity NeverDry commands. `switch.*` and `valve.*` are both supported and equivalent — the integration sends the services that domain understands. |
| **Flow rate** | An *instantaneous* volume/time reading: the witness that water is moving right now. Integrated over a run it also estimates a volume, so that estimate is only as good as the reporting cadence. |
| **Volume counters** | *session* = restarts at zero each run, and is the best source of all: when the run ends its value already **is** the volume delivered. *daily* / *hourly* = a total that resets on a calendar boundary — usable, with the [reset caveat](#the-caveats-that-apply-to-every-counter). |
| **History** | Past sessions kept on the device — the series in which delivery *decaying* over weeks becomes visible, which is how clogged drippers announce themselves. |
| **Needs YAML?** | Whether anything beyond picking an entity is required. ❌ means genuinely nothing. |
| **LoD** | Limit of detection — [what it is and how to measure it](#limit-of-detection-lod). Empty on every row so far, and it is the one number no config file contains. |
| **By** | Who established the row. Rows credited to *maintainer* were read off the project's own field installation. |

### R / W / G — read, write, get

Zigbee2MQTT declares, per value, what you may *do* with it, and the distinction
decides whether a capability is usable:

| Notation | Means | Consequence |
|---|---|---|
| **R** | published in the device's state payload | becomes a readable entity; you can select it |
| **W** | settable | can be commanded — but only through whatever path reaches it |
| **G** | can be requested with a `/get` | needed for values a sleeping battery valve does not push on its own |

`R--` is a plain sensor. `R-G` is a sensor that goes stale unless you ask.
`RW-` and `RWG` are controls. And **`-W-` is write-only**: you can set it and never
read back what you set, which is worth knowing before you build a decision on it.

The trap is that **R/W says nothing about reachability**. A value can be `RWG` and
still be impossible to touch from a dashboard, because it sits inside a composite
and no entity is ever created for it. That is exactly what happens to volume
dosing below.

## Verified valves

| Vendor / model | Firmware | Via | Valve | Flow rate | Volume counters | History | Needs YAML? | LoD | By |
|---|---|---|---|---|---|---|---|---|---|
| SONOFF **SWV** | 1.0.4 (20240820) | Z2M 2.13 | `switch.*` | ✅ m³/h | ✅ session + daily | ❌ | ❌ none | — | maintainer |
| SONOFF **SWV-ZFE** | 1.0.7 (20260317) | Z2M 2.13 | `switch.*` | ❌ | ✅ session + hourly | ⚠️ on request | ⚠️ for history | — | maintainer |
| SONOFF **SWV-ZFE** | 1.1.0 (20260724) | Z2M 2.13 | `switch.*` | ❌ | ✅ session + hourly | ⚠️ on request | ⚠️ for history | — | maintainer |

✅ works out of the box · ⚠️ reachable, with a documented step · ❌ not available ·
— not measured yet

**Read this table with two things in mind.** It is indexed by **firmware, not model**:
the two SWV-ZFE rows are the same product and differ, which is the whole reason the
page exists. And a ✅ under *Volume counters* means the entity is already there in Home
Assistant — nothing to install, nothing to write. Entity names, ranges and the
device's own quirks are in [the per-model notes](#per-model-notes-and-tricks); the
table is for deciding, not for configuring.

Two units of the SWV (fw 1.0.4) were compared expose-by-expose and are **identical**.
The two SWV-ZFE differ by exactly one entry: 1.1.0 adds a settable `water_flow_unit`.

### The short answer on counters and templates

**Every valve tested exposes its counter as an ordinary flat entity, in litres.**
No template, no MQTT, no YAML: you pick it in the zone form and you are done. If
it reads *unknown*, that is not a missing capability — a battery valve speaks
when it has something to say, and on the SWV-ZFE the volume entries need a `/get`
to be refreshed (Z2M access flag 5 rather than 1).

What *is* out of reach without extra work is the **history**, and only on the ZFE.

## Per-model notes and tricks

### SONOFF SWV (fw 1.0.4)

- The only model here with an **instantaneous flow**. That makes it the one where
  "the valve says it is open but no water is moving" can be judged directly.
- Two composites — `cyclic_timed_irrigation`, `cyclic_quantitative_irrigation` —
  do **not** become HA entities. They are the on-device repeat programmes, and
  they are *commanded*, not read: publish to `zigbee2mqtt/<name>/set`. NeverDry
  does not use them; leave them alone unless you want the valve to water itself
  behind the integration's back.
- No history. The daily total is all the device remembers.

### SONOFF SWV-ZFE (fw 1.0.7 and 1.1.0)

- **No flow sensor at all.** A zone on this valve can only be measured by its
  counter, which is worth knowing before you plan on a flow-based guard.
- Eight composites, none of which becomes an entity. Most are the on-device
  scheduler (`irrigation_plan_settings`, `irrigation_plan_report`,
  `irrigation_schedule_status`, `seasonal_watering_adjustment`,
  `valve_alarm_settings`, `manual_default_settings`). If you let NeverDry decide
  when to water, leave every one of these unset: two schedulers on one valve is
  the shortest path to a flooded bed.
- **The history trick.** `24_hours_records`, `30_days_records` and
  `180_days_records` are `text` entities, and they stay **empty** until you ask
  the device for them. The request is a composite, `read_swvzf_records`
  (`type`, `time_start`, `time_end`), so it cannot be a Jinja template alone — a
  template cannot publish to MQTT. It takes a script that publishes to
  `zigbee2mqtt/<name>/set`, and then a template sensor that decodes the blob the
  device puts in those text entities. That helper is deliberately **outside**
  NeverDry: it is useful whether or not you use this integration.
  *The payload format is not documented here because it has not been captured
  yet — those entities have been empty for the whole life of the test
  installation. First successful request wins the right to write this section.*

#### Why some toggles have no state: the write door and the read window

If you look at `Irrigation plan settings → enable_state` and find a switch with no
value — neither on nor off — nothing is broken. That field is **write-only**, and a
write-only value has no state by construction: the device accepts it and never
reports it back.

The ZFE splits each settable object into two composites, the same thing seen from
two sides:

| Composite | Access | All sub-fields | Role |
|---|---|---|---|
| `irrigation_plan_settings` | `RW-` | **`-W-`** | the **write door** — "Set irrigation plan", 6 slots (`plan_index` 0–5) |
| `irrigation_plan_report` | `R--` | **`R--`** | the **read window** — the same fields, reported back |
| `irrigation_schedule_status` | `R-G` | `R--` | what is executing *now*, and it can be **asked** |

So the answer to "what enables a plan?" is: you write `enable_state` through
*settings*, and you read it through *report*. Looking for the state in the same place
you write it will always come up empty.

**And this has a consequence for anyone letting NeverDry do the scheduling.** The
guidance is to disable the on-device plans, so that two schedulers are not fighting
over one valve. You can *write* that disable, per slot. Verifying it is harder than
it looks: `irrigation_plan_report` is `R--` with **no `G`**, so it cannot be
requested — it arrives when the device decides to send it — and it reports one plan
at a time. `irrigation_schedule_status` *is* gettable, but it describes what is
running now, not what is armed for later.

Practical reading: **treat "the device is not scheduling itself" as an assumption you
maintain, not a fact you can check on demand.** If a zone waters at an hour NeverDry
did not choose, an armed on-device plan is the first suspect, and the schedule status
is the entity to watch.

*Not verified: whether all six slots can be enumerated, or whether writing a
`plan_index` alone provokes a report. Deliberately untested — writing to plan
settings on a live irrigation system risks altering a plan rather than reading it.*

#### ⚠️ Firmware 1.1.0 can change the unit of its own counters

Firmware 1.1.0 adds one entry that 1.0.7 does not have: **`water_flow_unit`**, and
it is **settable**, with options `liter`, `us_gallon`, `imperial_gallon`.

This deserves a warning of its own, because it is the rare case where a device
silently changes the meaning of a number a piece of software is reading. Two of
those options are both called "gallon" and differ by about 20 % (US 3.785 L,
imperial 4.546 L). If you switch this setting, check what unit string reaches
Home Assistant before trusting any volume — and prefer leaving it on `liter`,
which is what the integration works in internally.

*Not yet verified: which unit string Z2M publishes for each option. Deliberately
untested, because the only way to find out is to change the setting on a live
irrigation system.*

## First assessment — what NeverDry needs, and what these valves can do

The table above says what a valve *exposes*. This one says whether that is
**enough**, per delivery mode. It is the more useful question, and it produced one
result worth the whole exercise.

| NeverDry needs | Where it comes from | SWV 1.0.4 | SWV-ZFE 1.0.7 | SWV-ZFE 1.1.0 |
|---|---|---|---|---|
| A valve entity | `switch.*` / `valve.*` | ✅ | ✅ | ✅ |
| **Timer mode** — a guard flow rate | a number *you type*, measured with a bucket | ✅ | ✅ | ✅ |
| **Flow-meter mode** — a delivery measurement | counter or flow-rate entity | ✅ both kinds | ✅ counter only | ✅ counter only |
| **Volume-dosing mode** — a volume target | a `number.*` entity on the device | ⚠️ hardware `RW-`, no entity | ⚠️ hardware `RWG`, no entity | ⚠️ hardware `RWG`, no entity |
| Battery reporting (optional) | `sensor.*_battery` | ✅ | ✅ | ✅ |
| Fault reporting (optional) | device status enum | ✅ `current_device_status` | ✅ `valve_abnormal_state` | ✅ `valve_abnormal_state` |

### The one that surprises

**Volume dosing is writable on every one of these valves, and reachable on none.**
The two are different failures and the table now separates them.

The capability is real and was read off the devices, not inferred:

| Valve | Where the volume target lives | Access | Range | Unit |
|---|---|---|---|---|
| SWV 1.0.4 | `cyclic_quantitative_irrigation.irrigation_capacity` | `RW-` | 0–6500 | **`liter`**, declared |
| SWV-ZFE 1.0.7 / 1.1.0 | `manual_default_settings.irrigation_amount` | `RWG` | 0–10000 | **none declared** — see below |

Both are settable. Neither produces a `number.*` entity, because both sit one
level down inside a composite, and Zigbee2MQTT's Home Assistant discovery does not
flatten composites. Checked on the running installation: **zero** `number.*`
entities across the four valves. NeverDry's volume-dosing mode asks for a
`number.*` to write the target into, so the mode is offered and cannot be
configured — on hardware that does the job natively.

The command path that *does* work is MQTT. For the ZFE:

```yaml
# zigbee2mqtt/<friendly_name>/set
manual_default_settings:
  irrigation_mode: capacity      # without this it doses by TIME, not volume
  irrigation_amount: 12
  fail_safe: 30                  # minutes; the device closes itself regardless
```

Two things in there deserve care. **`irrigation_mode` must be set to `capacity`**:
the same `irrigation_amount` field is ignored in `duration` mode, so a payload
that sets only the amount silently waters by the clock instead. And
**`irrigation_amount` carries no unit of its own** — on firmware 1.1.0 the unit is
the device-level `water_flow_unit` (`liter | us_gallon | imperial_gallon`), which
means the very same payload can deliver 12 L or 12 gallons depending on a setting
elsewhere on the device.

*On firmware 1.0.7 there is no `water_flow_unit` at all, and the unit of the manual
amount is not directly readable — only the plan report exposes an
`irrigation_amount_unit`, and only for plans. Whether the manual amount follows it
is **not verified**, and until it is, a volume target written to a 1.0.7 valve is a
number whose unit nobody can confirm.*

### Making volume dosing usable today

You do **not** need a template to build the JSON — `mqtt.publish` takes the payload
as-is. What you need is a way to give Home Assistant a `number.*` entity that
NeverDry can select, and a way to read a value back out of a composite. A template
number plus an MQTT sensor does both, in about ten lines.

That recipe, its three cautions, and the reason NeverDry will never do this for you
live in **[Preparing your hardware](hardware-interface.md)** — this page stays a
register of facts about devices, not a how-to.

## Performance indicators

Capability is not behaviour. A valve that exposes everything and answers in eight
seconds is worse to irrigate with than one that exposes little and answers in
one — the first spends every run inside a verification window.

**What NeverDry already measures for you**, per valve, with no configuration:

| Indicator | Where it lives | Why it matters |
|---|---|---|
| Open / close confirmation latency | `ValveLatencyTracker`, rolling window of the last 20 commands, persisted | The safety timeout is derived from it (mean + 3σ) rather than fixed, so a slow radio does not become a false failure |
| Retry consumption | the driver's retry budget | A valve that needs a second attempt most times is telling you about the mesh, not about itself |
| Consecutive failures | the valve state machine | What takes a zone out of service |

**What the device itself offers** (and worth recording per model):

- **Battery** — all three rows report it. Read it *before* the season, not during: a battery valve that goes flat mid-season keeps showing a perfectly ordinary `off`.
- **Device status / abnormal state** — the SWV calls it `current_device_status`, the ZFE `valve_abnormal_state`. Same idea, different name, which is itself a compatibility fact.
- **Link quality** — Zigbee2MQTT publishes `linkquality`, but Home Assistant leaves that entity disabled by default; enable it if you are diagnosing a mesh.
- **Reporting cadence of the counters** — the single most useful number nobody publishes. It decides whether an integrated flow rate means anything, and it is the difference between a counter that can time a one-minute run and one that cannot.

## Fill-in-the-values checklist

Everything above can be answered by running **one zone for one minute** and
writing down what moved. Copy this block, replace the right-hand sides, leave
anything you did not measure as `?` — a partial block is still worth sending.

```yaml
# NeverDry valve report — one-minute test
valve_model:            # e.g. SONOFF SWV-ZFE
firmware:               # e.g. 1.1.0
integration:            # zigbee2mqtt | zha | matter | esphome | cloud
valve_entity_domain:    # switch | valve

# --- timing (seconds) ---
open_confirm_s:         # command sent -> entity reported open
close_confirm_s:        # command sent -> entity reported closed
first_flow_s:           # open confirmed -> first non-zero flow or counter step
                        # leave ? if nothing measures flow

# --- what the counter did in that minute ---
counter_entity:         # the entity you read
counter_unit:           # L | gal | m³ ...
counter_before:
counter_after:
counter_updates:        # how many times it changed during the minute
counter_smallest_step:  # the smallest change you ever saw  <-- this is the LoD

# --- reference measurement (the honest part) ---
bucket_litres:          # what you actually caught, if you caught it
delivery_rate_lph:      # your best estimate of the zone's rate

# --- device health at the time ---
battery_pct:
device_status:          # whatever the status entity said
retries_needed:         # 0 if it opened first time
notes:                  # anything that surprised you
```

Two lines carry most of the value. **`counter_smallest_step`** is the limit of
detection, the one number no configuration file contains. And **`counter_updates`**
tells you whether the number is a measurement or a rumour: a counter that changed
once in a minute cannot describe a one-minute run, however precise its units look.

> The intention is that you will not have to fill this in by hand for long. A
> one-minute valve test inside the config flow is planned, and it is meant to emit
> exactly this block for you to copy — measuring instead of asking you to declare.
> Until it exists, the bucket and the stopwatch are the instrument.

## The caveats that apply to every counter

**None of these counters is a monotonic lifetime meter.** The session one
restarts at zero, the daily one resets at midnight, the hourly one every hour.
So the familiar recipe — read before, read after, subtract — has a hole in it: a
run that crosses the reset gives a **negative** difference, and a decrease is
never delivery. Prefer the session counter, where the final value already *is*
the delivered volume.

## Limit of detection (LoD)

A counter that exists is not the same as a counter that can see your irrigation.
A meter whose smallest step is 1 L, on a dripline delivering 4 L/h, cannot
distinguish "no water" from "one minute of water". The number is not wrong; it is
**below the limit of detection**, and treating it as a fault produces an alarm on
every healthy run.

This is why the column exists and why it is empty: LoD is a property of *your
whole installation* — valve, meter resolution, reporting cadence, and the
emitters on that zone — not of the valve alone. It has to be measured where it
lives.

**How to measure it, once:** run one zone for a fixed minute with a bucket under
a known emitter. Record what the counter reports, what it reported a minute
earlier, and how many times it updated in between. The smallest change the
counter ever shows is its resolution; that resolution divided by your delivery
rate is roughly the shortest run this installation can measure at all. Anything
shorter must be *estimated*, not *verified* — and the difference matters, because
an absence of evidence is not evidence of absence.

## Call for contributions — send us your valve

The table above covers **two models from one vendor**, both on Zigbee2MQTT. That
is a start, not a survey. If you run NeverDry — or any irrigation setup — on
something else, a single report closes a gap that no amount of reading
documentation can.

**What is genuinely useful, in order:**

1. **Vendor, model, firmware version and date code.** Not just the model:
   this page exists because one firmware bump changed the answer.
2. **How it reaches Home Assistant** — Zigbee2MQTT, ZHA, Matter, a cloud
   integration, ESPHome — and which entity domain the valve itself lands in
   (`switch.*`, `valve.*`, something else).
3. **Which of the four measurements it exposes**, with units: flow rate, session
   volume, aggregate volume, history.
4. **Whether any of them needed a trick** to become usable — a template, an MQTT
   publish, a YAML sensor. If you wrote one, paste it: that is the part nobody
   can guess.
5. **The LoD measurement above**, if you are willing. It is the one number in
   this table that cannot be read off a config file, and it is the one that
   decides whether a guard helps you or cries wolf.

### How to send it

**Open an issue with the *Valve report* form:**
[new valve report →](https://github.com/never-dry/NeverDry/issues/new?template=valve_report.yml)

It is a form, not a blank box, so you cannot accidentally leave out the field
that makes the report usable. Every box is optional except the model — a partial
report is still worth sending, and half a row beats no row.

What happens next: the report is read, transcribed into the table above, and the
issue is closed with a link to the new row. If you ticked the credit box, your
handle goes in **Reported by** — the table is a shared record, and it should say
who established each fact. If something in your report contradicts a row that is
already here, that is the most interesting kind of report and it will be
investigated rather than merged quietly.

For a Zigbee2MQTT setup, `tools/valve_report.py` in this repository prints
everything in items 1–3 in one go, ready to paste, and reads nothing but the
bridge's own device list:

```bash
python3 tools/valve_report.py --ha-url http://homeassistant.local:8123 --token <long-lived token>
```

It needs `pip install websockets`. If you would rather install nothing, copy the
`zigbee2mqtt/bridge/devices` message out of MQTT Explorer and run
`--from-file bridge_devices.json` instead. Either way the token stays on your
machine and nothing is uploaded — the script prints to your terminal and stops
there.

Reports of valves that **do not** work are as valuable as the ones that do —
arguably more, because they are the rows that stop someone else buying the wrong
hardware.
