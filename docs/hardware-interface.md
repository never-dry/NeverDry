# Preparing your hardware — the NeverDry interface contract

**Status:** Accepted (ADR), 2026-08-17. This is a boundary decision, not a guide to
a feature: it says what NeverDry will and will not do, permanently.

## The boundary, in one sentence

> **NeverDry consumes Home Assistant entities. It does not speak to your hardware.**

It never publishes to MQTT, never knows what Zigbee is, never parses a device's
JSON, never learns a vendor's cloud API. It reads entities, writes entities, and
that is the whole of its contact with the physical world.

Everything on the far side of that line — composites, nested payloads, `/get`
requests, firmware quirks, unit settings buried in a device menu — is **your
yard**. This document tells you how to prepare that yard so NeverDry finds a meal
ready on the table, and the [valve compatibility register](valve-compatibility.md)
tells you what each tested model actually serves.

### The corollary: use the mature mechanism, invent nothing

The boundary says what NeverDry will not do. This says what you should do instead,
and it is not "write something clever":

> **Prefer the mechanism that already exists in the layer that owns the problem.
> Document it. Do not reinvent it.**

Almost every gap between a device and Home Assistant already has a supported answer,
maintained by people who own that layer: external converters and quirks for Zigbee,
template and MQTT entities in Home Assistant core, blueprints for sharing. Each is
documented, versioned, and reviewed by more eyes than this project has.

The order of preference follows from that, and it is worth applying before writing a
single line:

1. **Fix it in the layer that owns the device** — a Zigbee2MQTT converter, a ZHA
   quirk. Produces first-class entities for everyone with that hardware.
2. **Get that fix upstream** — then nobody needs a local file at all.
3. **Use a core Home Assistant primitive** — template entities, MQTT entities.
   No custom code, and it survives upgrades.
4. **Share it as a blueprint** if it is reusable, a package if it is not.
5. **Write new code** only when the first four genuinely do not reach.

Nothing in this document reaches step 5, and that is the intended outcome.

## Why the line is drawn here, and why it will not move

This is worth explaining, because the alternative looks convenient. NeverDry could
learn to publish MQTT and reach the volume target buried inside a SONOFF
composite. It will not, for reasons that outlast any single device:

1. **It would tie the integration to one ecosystem.** The moment NeverDry speaks
   Zigbee2MQTT, the users on ZHA, Matter, ESPHome and vendor clouds become
   second-class — supported by a code path that nobody tests, for hardware nobody
   here owns. Today all of them are equal, because all of them produce entities.
2. **It would add a dependency on a broker being reachable** to a component whose
   job includes closing valves safely. A safety path that can fail because a
   broker is down is a worse safety path.
3. **The rules would stop being testable.** The water balance can be exercised
   without a runtime because it touches nothing outside itself. Every protocol
   NeverDry learns is a mock somebody has to write and trust.
4. **It would make the integration responsible for firmware trivia.** Which
   firmware calls the status entity what; whether an amount is litres or gallons
   this week. That knowledge belongs in a document that anyone can correct, not
   in code that has to be released.

So the answer to "can NeverDry handle my device's JSON?" is **no, deliberately** —
and the answer to "can I make my device look like something NeverDry handles?" is
**almost always yes, in about ten lines of YAML.**

## What NeverDry expects to find

Three shapes. Nothing else about your hardware matters — and on the hardware tested so
far, **two of the three are already native and need nothing at all.** Worth knowing
before you start building: the work is narrower than it looks.

| # | What it needs | Entity domain | Required for | Notes |
|---|---|---|---|---|
| 1 | **Something it can open and close** | `switch.*` or `valve.*` | any irrigation | Both are equal; the integration sends the services that domain understands. Omit it for monitoring-only zones. |
| 2 | **A delivery measurement** | `sensor.*` | flow-meter mode | Either a cumulative volume counter or an instantaneous flow rate. Both work; the unit tells them apart. |
| 3 | **A volume target it can write** | `number.*` | volume-dosing mode | The integration writes the litres it wants and lets the device close itself. |

Plus, all optional and all plain `sensor.*`: temperature, rain, humidity, wind,
solar radiation at site level; battery and soil moisture per zone.

**Requirement 1 must be the valve itself** — not a scene, a script, or a group of
several valves. NeverDry watches the entity it commanded to confirm that the valve
actually moved, and a script confirms nothing about water.

## Recipes — turning what you have into what it expects

Applied in the order of preference above: the first recipe is the one that asks you
*not* to use the others.

### Your device buries the value inside a composite

This is the common case on Zigbee2MQTT, and the one worth learning once. A
*composite* is a group of values the device treats as one thing. Zigbee2MQTT
publishes it, but its Home Assistant discovery does not flatten it, so **no entity
is created** — even for values marked writable.

If flattening it one layer down (above) is not available to you, two core Home
Assistant pieces bridge it — and they are core pieces precisely so that nothing new
has to be written. An MQTT sensor reaches into the payload to read; a template number
publishes to write. Worked example, using the volume target of a SONOFF SWV-ZFE:

```yaml
# configuration.yaml
mqtt:
  sensor:
    # The composite IS published — it simply never became an entity.
    # Reaching a nested field is exactly what a value_template is for.
    - name: "Melograno irrigation amount (readback)"
      state_topic: "zigbee2mqtt/giardino_melograno"
      value_template: "{{ value_json.manual_default_settings.irrigation_amount | default(0) }}"
      unit_of_measurement: "L"

template:
  - number:
      - name: "Melograno irrigation amount"
        unique_id: melograno_irrigation_amount
        state: "{{ states('sensor.melograno_irrigation_amount_readback') | float(0) }}"
        min: 0
        max: 10000          # the device's own ceiling
        step: 1
        unit_of_measurement: "L"
        set_value:
          - action: mqtt.publish
            data:
              topic: "zigbee2mqtt/giardino_melograno/set"
              payload: >-
                {"manual_default_settings":
                  {"irrigation_mode": "capacity", "irrigation_amount": {{ value | int }}}}
```

That produces a real `number.melograno_irrigation_amount` — requirement 3, met,
with NeverDry none the wiser about how.

Three cautions, all learned from the device rather than guessed:

- **`irrigation_mode: capacity` is not optional.** Omit it and the same number is
  read as *minutes*: the valve waters by the clock and nothing reports an error.
- **The amount carries no unit.** On this model the unit is a separate device-level
  setting (`liter | us_gallon | imperial_gallon`). The `unit_of_measurement: "L"`
  above is a *claim about your device's configuration*, not something the valve
  confirms — switch the device to gallons and this YAML starts lying quietly.
- **`fail_safe` is left alone on purpose.** It is the valve closing itself
  independently of Home Assistant. Good to have, but be aware it is a second
  timeout running alongside NeverDry's own.

**Two shapes to share this in, and they are not interchangeable.** A *package* is
a config file you copy; a *blueprint* is a reusable template with inputs that Home
Assistant imports from a URL in one click. Blueprints are the better artefact, but
they have a structural limit: one blueprint produces **one entity**, and `mqtt:`
platform entities can never be blueprints at all. So the division is fixed by Home
Assistant, not by preference:

| Piece | Shape | Why |
|---|---|---|
| The writable `number` | **blueprint** — [`blueprints/template/z2m_volume_target.yaml`](../blueprints/template/z2m_volume_target.yaml) | a template entity with inputs; imports by URL |
| The read-back sensor | **package YAML** — [`docs/examples/volume-dosing-bridge.yaml`](examples/volume-dosing-bridge.yaml) | `mqtt:` cannot be a blueprint; four lines, copied once |
| Waking a sleeping valve | script, either shape | trivial either way |

### Not every valve should be bridged

The recipe above is for the SWV-**ZFE**, whose composite the vendor documents as
*single irrigation settings*: set the amount, open, the valve closes itself.

The plain **SWV has no such thing.** Its only volume mechanism is
`cyclic_quantitative_irrigation` — "circulating quantitative irrigation" — which
carries `total_number` (0–100 repeats) and `irrigation_interval` alongside the
capacity. It is a *repeat programme*, not a dose. Bridging it means arming an
on-device schedule and asking it to run once, which puts a second scheduler on the
valve — the one thing [What not to do](#what-not-to-do) tells you never to do. Leave
it alone: the SWV is the model that exposes a flow rate *and* two counters, so it is
already the best-measured valve in the register, and volume dosing buys it nothing.

> **Status: confirmed on a live payload (SWV-ZFE fw 1.1.0, 2026-08-17).** The composite
> is published, and it carries `irrigation_amount_real_liter` — already converted to
> litres — so the read-back does not depend on the device's unit setting. Read the
> composite and not the flattened copies Zigbee2MQTT also publishes at top level: in the
> observed payload one of those mirrors disagreed with its composite. If you try it,
> [say so](https://github.com/never-dry/NeverDry/issues/new?template=valve_report.yml)
> and it goes in the register with your name on it.

### First, ask whether the fix belongs one layer down

Before writing any of the YAML above, know that there is a **better place to fix
this**, and it is not Home Assistant. Both major Zigbee stacks provide a supported
way to change how a device is presented, and both sit exactly where the problem is:

| Stack | Mechanism | What it does |
|---|---|---|
| Zigbee2MQTT | **external converter / definition** (JavaScript) | overrides or extends the device definition, so a composite can be exposed as separate flat values — and Home Assistant discovery then creates ordinary entities, readable *and* writable |
| ZHA | **quirk** (Python, `zha-quirks` / zigpy) | remaps clusters and attributes into proper entities for the same reason |

These are strictly better than a template bridge in one way that matters: they
produce **first-class entities**. No template, no per-user file to copy, and the fix
benefits everyone who owns that device — including people who never heard of this
integration. And when the change is **upstreamed** (a pull request to
`zigbee-herdsman-converters` for Z2M, or to `zha-quirks` for ZHA), every user gets
it with no local files at all.

That is also the honest reading of this project's own boundary: if a composite
should have been flattened, the layer that owns the device is where it should be
flattened. An irrigation integration papering over it is the workaround, not the
fix.

**When the YAML bridge is still the right answer:**

- **You need it working today.** A converter is JavaScript running inside
  Zigbee2MQTT and its API has had breaking changes across major versions; a
  template number is core Home Assistant and will not move.
- **Your device does not arrive over Zigbee at all.** A cloud integration or a
  proprietary bridge has no quirk mechanism to fix.
- **You want it isolated.** A bad template makes one entity unavailable. A bad
  external converter can affect how Zigbee2MQTT handles the device.

*Not attempted here yet.* Whether the SONOFF SWV-ZFE composites *should* be
flattened upstream is a question for the Zigbee2MQTT maintainers — several of those
composites are genuinely single settings that belong together, and
`manual_default_settings` may well be one of them.

### If you do not know how to write any of this

Three routes, in the order you should try them. The first needs no YAML at all.

**1. Import the blueprint.** For a writable volume target on a Zigbee2MQTT valve,
[`blueprints/template/z2m_volume_target.yaml`](../blueprints/template/z2m_volume_target.yaml)
imports from a URL and asks three questions: the Zigbee2MQTT name, the device's
maximum, and the unit it is set to. It creates the `number.*` entity and nothing
else is needed. Validated end-to-end on Home Assistant 2026.8.

It cannot cover the **read** side: `mqtt:` entities can never be blueprints. Which
matters, because a counter or a flow rate has to be *read*, so if yours is buried the
next two routes are the ones.

**2. Have the YAML generated for you.** `tools/valve_report.py` already reads your
bridge's device list; emitting the matching YAML from it is deterministic — the topic,
the field names and the ranges all come from the device itself, so there is nothing to
get wrong. *(Not built yet; tracked in the backlog.)*

**3. Ask an LLM, then check its work.** This is a legitimate route and a fast one, but
this YAML opens valves, so it comes with an obligation. An LLM will confidently invent
a topic name that does not exist, and the failure is silent: the entity appears, the
write goes nowhere, and the valve never opens.

Copy your device's full state payload — in Home Assistant, *Developer tools → MQTT →
Listen to a topic*, `zigbee2mqtt/<your device>` — and ask for what you need in these
terms:

> Here is the JSON that my Zigbee2MQTT device publishes on the topic
> `zigbee2mqtt/NAME`. Write Home Assistant YAML that:
> (a) exposes `<the field I want to read>` as an `mqtt:` sensor, using a
> `value_template` that reads it from this exact payload structure, with the unit the
> payload implies;
> (b) exposes `<the field I want to write>` as a `template:` `number:` whose
> `set_value` publishes to `zigbee2mqtt/NAME/set`.
> Use only field names that appear in the JSON I gave you. If a field I asked for is
> not in it, say so instead of guessing.

**Then verify, before you let it near a valve — five checks, none optional:**

1. **Every field name in the YAML appears in your payload.** Search for each one. This
   single check catches most of what goes wrong.
2. **The topic matches your device exactly**, and the write topic ends in `/set`.
3. **`check_config` passes** — *Developer tools → YAML → Check configuration*. It will
   not catch a wrong field name, but it catches everything structural.
4. **The read-back moves.** Watch the new sensor while you change the setting from the
   vendor app or the Zigbee2MQTT frontend. A sensor that never moves is not reading
   anything.
5. **The write reaches the water, not just the entity.** Set the number, then confirm
   the *device* changed — in its own interface, not in Home Assistant. A template that
   publishes to the wrong topic looks perfectly healthy from the Home Assistant side.

### Which measurement to expose, if your valve has more than one

NeverDry uses a **counter** or a **flow rate**, and it will take either. If your valve
offers both, expose both: they are not redundant.

- The **counter** is the measurement. Read before and after, and the difference is the
  volume — no dependence on how often the device reports.
- The **flow rate** is the witness. It answers a different question — *is water moving
  right now* — which a counter answers only slowly, and which is what tells apart "the
  valve says open and nothing is flowing" from "the valve is watering".

So: counter for the volume, rate for the evidence, both where the hardware allows. If
you only have one, that is fine — a zone measured by a counter alone works, and a zone
measured by a rate alone works with the caveat that its accuracy is set by the
reporting cadence.

### Your device only reports when it feels like it

Battery valves sleep. An entity showing `unknown` after a restart is usually not a
missing capability — it is a device that has not spoken yet. On Zigbee2MQTT, values
whose access includes `G` can be woken:

```yaml
# a script you can call before a run, or from an automation
action: mqtt.publish
data:
  topic: "zigbee2mqtt/giardino_pino/get"
  payload: '{"real_time_irrigation_volume": ""}'
```

Do **not** wire this into a tight loop. Each request is a radio round trip and
battery valves pay for it.

### Your device exposes a rate but no counter (or the reverse)

Both are acceptable; they are not equivalent. A counter read before and after is a
direct measurement. A rate has to be integrated, so its accuracy depends on how
often the device reports — a rate that updates once a minute cannot describe a
one-minute run.

If you have both, prefer the counter. If your counter resets on a calendar
boundary (daily, hourly), be aware that a run crossing the reset produces a
*decrease*, and a decrease is never delivery. A **session** counter — one that
restarts at zero each run — is the best of all: when the run ends its value already
*is* the volume delivered.

### Your device only reports a total, and you want it per zone

Do not try. One meter on a shared main cannot attribute water to the zone that was
open, and a template that pretends otherwise produces numbers that look right and
are not. Leave the zone without a measurement and let it estimate; an honest
estimate beats a confident fiction.

## What not to do

- **Never point NeverDry at NeverDry's own sensors.** Its output entities
  (`sensor.neverdry_*_volume`, `*_session_water`) look plausible in the picker and
  would close a loop on themselves.
- **Never let the device schedule too.** If NeverDry decides when to water, clear
  the on-device plans and timers. Two schedulers on one valve is the shortest path
  to a flooded bed, and neither will report anything wrong. Note that on some
  hardware you can *write* that disable and not read it back — the enable flag is
  write-only, so this is an assumption you maintain rather than a fact you verify.
  See [the write door and the read window](valve-compatibility.md#why-some-toggles-have-no-state-the-write-door-and-the-read-window).
- **Never use a group or a script as the valve.** See requirement 1.
- **Do not "fix" a unit by writing a converting template** unless you have checked
  what the device actually sends. Home Assistant already converts anything with a
  proper `device_class`; a manual factor on top of that is applied twice.

## Before you configure NeverDry — check your prepared entities

Five minutes here saves a season of confusing numbers. In **Developer tools →
States**, for each entity you are about to select:

1. **It has a state**, not `unknown` or `unavailable` — or you know why, and know
   how to wake it.
2. **It has the unit you think it has**, spelled the way Home Assistant spells it.
   `gal` may be a US gallon or an imperial one, and they differ by about 20 %.
3. **It changes.** Watch it during a manual run. An entity that never moves is not
   a measurement, and it will not become one once NeverDry is watching.
4. **It belongs to the right zone.** A probe or meter attached to the wrong zone is
   worse than none, because it is believed.
5. **For a `number.*` you built**: write to it from Developer tools → Actions and
   confirm the *water* changes, not just the entity. A template number that
   publishes to the wrong topic looks perfectly healthy.

If all five hold, NeverDry has its meal ready and will not ask you about your
hardware again.

## Related

- [Valve compatibility register](valve-compatibility.md) — per-model, per-firmware
  facts, and which tricks a given valve needs
- [User manual](user_manual.md) — the fields these entities go into
- [Developer manual](developer_manual.md) §4 — where this boundary is enforced in
  the code, and the guards that keep it there
