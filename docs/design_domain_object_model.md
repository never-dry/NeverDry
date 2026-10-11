# Design — Domain Object Model

**Status:** Accepted (ADR), 2026-08-16
**Date:** 2026-07-05 (updated 2026-07-06 with review feedback from GH #74; 2026-07-23 aligned with the water-balance reference model; 2026-07-26 the water-balance model made a first-class object; 2026-08-01 RFC to dissolve `System` into `Environment` + capability-matched models; 2026-10-10 a valve coordinator proposed for the electrical limit of a controller board, GH #320; 2026-10-10 the master's start order, linger and per-zone opt-out, GH #95; 2026-10-10 entries by place and a named water network shared across entries, discussion #239; 2026-10-10 the water network becomes an entry of its own, each zone names the network it draws from, and the per-zone master opt-out is retired; 2026-10-11 entry types are Environment and water network, place and hand-watering move to the zone; 2026-10-11 the valve coordinator becomes a third entry type, sibling of the water network)
**Related:** GH #74 (actuator abstraction discussion), GH #94 (`valve.*` support), GH #95 (master valve/pump), GH #320 (station limit of a 24 VAC controller), [Water-Balance Reference Model](design_water_balance_reference_model.md) (where the deficit lives), [Domain Model Anomalies](design_domain_model_anomalies.md) (code-verified audit against this model)

## Purpose

Define the conceptual objects of NeverDry's irrigation domain, independent of the current
module layout. This model guides where new features belong (e.g. "does the master valve go
in the scheduler or in the system?") and what should become an explicit first-class object
as the codebase evolves. For the current module/data-flow architecture see
`developer_manual.md` §1.

## The five objects

| Object | Responsibility | Key attributes |
|---|---|---|
| **Environment** | The **site**: what this installation has, and what it can therefore compute | The declared sensor inventory (temperature, rain, humidity, wind, net radiation, tmax/tmin, soil probe, rain probability) + latitude; **capability matching** — a zone may offer only the models this inventory can feed; yearly rain, one sky over the whole garden; the forecast rain-delay threshold. Written in `environment.py`. Replaces the former **System**, which was dissolved rather than renamed (α → `ETModel`, D_max → `Zone`, master valve → `MasterDriver`) |
| **Zone** | Irrigation unit; owns its deficit and translates it into water demand | Kc / plant family, area, efficiency, site exposure; **placement** (outdoor/patio/greenhouse/indoor — gates whether rain reaches it); **its own D_max**, the reservoir of this soil; cycle & soak rule; **its own deficit** (`+ET·Kc·Δt − rain − irrigation`, new zone starts at 0); translates mm → liters. Written in `zone.py` |
| **Scheduler** | The *when* — and the concurrency policy | Time windows, sequences, calendars; serial vs parallel zone runs, interleaving during soak |
| **ZoneDriver** | The *how* — actuation of one zone's water demand | Entity adapter (`valve.*`/`switch.*`), delivery mode (native volume in liters vs time in seconds via flow rate), flow rate, zero-flow guard; returns a **DeliveryResult** |
| **MasterDriver** | Coordination of shared hydraulics (pump / master valve) | ON when any ZoneDriver is active, OFF when none; configurable off-delay; no notion of liters |

ZoneDriver and MasterDriver are two specializations of a common **Driver** base, which owns
what they share: the entity adapter, ON/OFF command with state confirmation, adaptive
latency/timeout, and the safety layers (watchdog, close on error/stop/restart).

Two more objects are **proposed, not written**, and land together with the scheduler work:

- the **WaterNetwork**, the hydraulic line zones draw from: an entry of its own, declared once on
  the instance, that each zone names; it holds the hydraulic limit, the master and the
  turn-taking between entries. Without one, each Environment entry has an implicit network of its own,
  one line with no master and no limit ([its section](#waternetwork-proposed));
- the **ValveCoordinator**, which holds the electrical limit of a controller board (transformer), how many
  solenoids its transformer can hold open at once: the water network's sibling, an entry of its
  own (a *valve coordinator*) that each zone names. Without one, each Environment entry has an implicit
  virtual coordinator, limit 1 ([its section](#valvecoordinator-proposed)).

Why they are separate objects, and why neither is the scheduler, is in the design decisions
[Electrical parallel is not hydraulic parallel](#electrical-parallel-is-not-hydraulic-parallel-a-coordinator-not-the-scheduler)
and [Environments, water networks and valve coordinators: the three entry types](#environments-water-networks-and-valve-coordinators-the-three-entry-types).

## The water-balance model (the *how much*)

Where the **Driver** abstracts the *how* of actuation, the **WaterBalanceModel** abstracts the
*how much*: the scientific computation of a zone's water demand. It is the object that turns
whatever inputs a user's setup provides into a **Deficit**. It has two symmetries with the
Driver side, and modeling it explicitly buys the same thing the Driver did:

| Sensing side (*how much*) | Actuation side (*how*) |
|---|---|
| **WaterBalanceModel** (abstract strategy) | **Driver** (abstract) |
| ↳ `ETModel`, `VWCSystemModel`, `VWCPerZoneModel` | ↳ `ZoneDriver`, `MasterDriver` |
| **Deficit** (mm + reference frame) — the value returned | **DeliveryResult** (liters + quality) — the value returned |

- **WaterBalanceModel** — a strategy that produces a `Deficit`. Its concretes mirror the
  reference frames of the [Water-Balance Reference Model](design_water_balance_reference_model.md):
  the ET frame is an abstract `ETBalanceModel` (shared forward-Euler integration, pluggable ET
  rate) with three **tiers** by input cost — `ETModel` (temperature-only, today's baseline; any
  user can run it), `HargreavesModel` (FAO-56 Hargreaves-Samani; adds the diurnal temperature
  range, its radiation term is computed from latitude + date, so still **no extra sensor**), and
  `PenmanMonteithModel` (FAO-56 Penman-Monteith, physically grounded, needs humidity + wind + net
  radiation — inputs not every user has); plus `VWCSystemModel` (one system moisture probe,
  stateless) and `VWCPerZoneModel` (a per-zone probe — the AI-174 target). Adding a tier is one
  new `et_rate`, not a new integrator: the "user picks the ET method their sensors support"
  design falls straight out of the output seam.
- **Deficit** — the value object every model returns: millimetres **plus the reference frame**
  they are defined against. A bare number is not enough — the reference model's load-bearing rule
  is that *two deficits are comparable only within one frame*, so the frame (and, for per-zone
  probes, the source identity) travels with the value.

**The seam is the output, not the input.** The three models share no inputs — ET needs weather,
VWC needs a probe, and not every user has a soil-moisture sensor. What they share is the
**output**: every model yields a `Deficit` in mm. This is precisely why plug-and-play works at
the output and not the input: each model copes with the sensors it has and exposes the same
quantity, so a setup can switch models (or fall back ET ⇄ VWC) without the Zone knowing which
one ran. This also unifies the ET formula that today lives in two places (`ETSensor` and
`DrynessIndexSensor`) into a single `ETModel.et_hourly`.

Ownership follows the reference model unchanged: the **Environment** owns the shared feeds/probe, the
**Zone** owns its `Deficit` and its `Kc`. The model is the *mechanism* the Zone uses to advance
that deficit; `Kc` is passed into a per-zone `ETModel` instance (the system reference uses
`Kc = 1.0`), because per-zone irrigation resets are independent and a shared reference cannot be
scaled proportionally after the fact.

## Translation chain

```
Environment provides the feeds + the inventory  temperature, rain, RH, wind, Rn, tmax/tmin,
   │                                              probe, rain probability, latitude
   │            gates which models a zone may run  declared_sensors ⊇ model.required_sensors
   │
Zone        accumulates its own deficit (mm)      +ET·Kc·Δt − rain − irrigation; new zone starts at 0
   │         rain credited only if placement       is open to the sky
   │         then translates mm → liters (area)    applies cycle & soak, own D_max
   │
ZoneDriver translates liters → actuation        native volume if supported,
   │                                            else seconds via flow rate
Scheduler  decides in which window it happens
```

Liters are the **contract** between Zone and ZoneDriver: the zone always requests liters;
only the driver knows whether to deliver them by volume or by time. This makes the fallback
natural — same request, two actuation strategies.

The contract is a **round trip**: the driver does not just execute "water X liters", it
returns a **DeliveryResult** — the liters actually delivered, stated as truthfully as the
backend allows (see the design decision below).

## Class diagram

![UML class diagram of the NeverDry domain model](assets/domain_model_uml.svg)

*Rendered diagram (`assets/domain_model_uml.svg`), generated from the Mermaid source below,
which is the normative definition. After changing the source, regenerate the SVG rather than
editing it by hand:
`mmdc -i <source>.mmd -o docs/assets/domain_model_uml.svg -c <config> -b white`, with the
config `{"theme": "neutral", "class": {"htmlLabels": false}}` so the labels are plain SVG text.
Classes marked `<<proposed>>` are not written yet.*

```mermaid
classDiagram
    direction TB

    class Environment {
        +temperature_sensor
        +rain_sensor
        +humidity_sensor
        +wind_speed_sensor
        +net_radiation_sensor
        +temp_max_sensor
        +temp_min_sensor
        +soil_moisture_sensor
        +rain_probability_sensor
        +latitude
        +rain_delay : RainDelayPolicy
        +yearly_rain_mm : one sky
        +declared_sensors() SensorKind set
        +satisfies(required) bool
        +missing_for(required) SensorKind set
    }

    class Zone {
        +name
        +area_m2
        +efficiency
        +plant_family / manual_kc
        +exposure / microclimate_factor
        +placement : Placement
        +actuation : VALVE or BY_HAND, default VALVE
        +d_max : own reservoir
        +threshold_mm
        +cycle_soak : CycleSoakRule
        +network : WaterNetwork, default the entry's implicit one
        +coordinator : ValveCoordinator, default the entry's virtual one
        +deficit : Deficit, starts at 0
        +counters : WaterCounters
        +effective_kc(base_kc)
        +accumulate(dt_h, et_h, base_kc, rain_mm) Deficit
        +water_demand_l() liters
        +needs_water() bool
        +begin_cycle()
        +credit_delivery(d : Delivery) Deficit
        +settle(d : Delivery, source, at) Deficit
        +mark_irrigated(source, at) Deficit
    }

    class Placement {
        <<enumeration>>
        OUTDOOR / PATIO / GREENHOUSE / INDOOR
        +receives_rain : only OUTDOOR
        +driven_by_outdoor_et : OUTDOOR, PATIO
    }

    class Delivery {
        <<protocol>>
        +liters_delivered
        +elapsed_s
    }

    class Scheduler {
        +concurrency : ConcurrencyPolicy
        +min_service_interval_s
        +allows_overlap() bool
        +evaluate_scheduled(zone, is_running) Decision
        +evaluate_reactive(zone, is_running, is_throttled) Decision
        +next_eligible(zones, is_running) Zone
        deferred: time_windows, queue, interleave_during_soak
        proposed: freeze gate, by-hand requests
    }

    class Decision {
        +should_irrigate
        +trigger : Trigger
        +reason : SkipReason
    }

    class Driver {
        <<abstract>>
        +entity_adapter : valve.* or switch.*
        +adaptive_timeout
        +safety_layers : watchdog, close on error/stop/restart
        +ping_interval_min
        +turn_on() confirmed
        +turn_off() confirmed
        +ping() alive or unreachable
    }

    class ZoneDriver {
        +delivery_mode : native_volume or time_x_flow
        +flow_rate_lpm
        +flow_telemetry : optional
        +zero_flow_guard
        +deliver(liters) DeliveryResult
    }

    class MasterDriver {
        +start_delay_s : 1 s after the first zone opens, optional
        +off_delay_s : 5 s after the last confirmed close, optional
        +follow(any_zone_driver_active)
    }

    class WaterNetwork {
        <<proposed>>
        +name : its own entry, on the instance
        +hydraulic_limit : optional, max flow or max lines
        +master : MasterDriver, optional
        +admit(zone) bool
        +next_turn(waiting entries) entry
        default: implicit, one per Environment entry
    }

    class ValveCoordinator {
        <<proposed>>
        +name : its own entry, on the instance
        +max_active_zones : declared net of the master
        +zone_drivers : ZoneDriver set
        +start(zones) started count
        +on_outside_open(driver) warn, timer runs suspect
        default: implicit, one per Environment entry, limit 1
    }

    class ManualActuator {
        +role : manual
        +request_irrigation(liters) DeliveryResult
        +mark_irrigated(liters?) DeliveryResult
    }

    class DeliveryResult {
        +liters_delivered
        +quality : measured, estimated, partial, delayed, low_confidence, declared
        +elapsed_s
        +revise(measured_liters)
    }

    class WaterBalanceModel {
        <<abstract>>
        +reference_frame : ReferenceFrame
        +is_stateful : bool
        +deficit : Deficit
        +step(inputs) Deficit
        +apply_irrigation(mm) Deficit
        +reset() Deficit
    }

    class ETBalanceModel {
        <<abstract>>
        +kc
        +et_rate(inputs) mm/h
        +step(inputs) Deficit
    }

    class ETModel {
        +alpha, t_base
        +et_hourly(temp_c) mm/h
    }

    class HargreavesModel {
        +latitude_deg
        +et0_daily(Tmax, Tmin, doy) mm/day
    }

    class PenmanMonteithModel {
        +pressure_kpa
        +et0_daily(T, rh, wind, Rn) mm/day
    }

    class VWCSystemModel {
        +field_capacity, root_depth
        +step(VWCReading) Deficit
    }

    class VWCPerZoneModel {
        +source : probe/zone id
    }

    class Deficit {
        +value_mm
        +frame : ReferenceFrame
        +source : per-zone identity
        +is_comparable_to(other) bool
        +as_liters(area_m2) liters
    }

    Driver <|-- ZoneDriver
    Driver <|-- MasterDriver
    WaterBalanceModel <|-- ETBalanceModel
    ETBalanceModel <|-- ETModel
    ETBalanceModel <|-- HargreavesModel
    ETBalanceModel <|-- PenmanMonteithModel
    WaterBalanceModel <|-- VWCSystemModel
    VWCSystemModel <|-- VWCPerZoneModel
    Environment "1" o-- "*" Zone : feeds ET+rain to
    Environment ..> WaterBalanceModel : gates by capability match
    Zone "1" --> "1" WaterBalanceModel : advances deficit via
    WaterBalanceModel ..> Deficit : returns
    Zone ..> Deficit : holds, settles
    Zone *-- Placement : sits at
    Zone "1" --> "0..1" ZoneDriver : requests liters (VALVE)
    ZoneDriver ..> DeliveryResult : returns
    Zone ..> ManualActuator : requests (BY_HAND)
    ManualActuator ..> DeliveryResult : returns (declared)
    DeliveryResult ..|> Delivery : satisfies structurally
    Zone ..> Delivery : settles deficit with
    Scheduler --> Zone : decides when
    Scheduler ..> Decision : returns
    MasterDriver ..> ZoneDriver : ON while any is active
    Zone "*" --> "1" WaterNetwork : draws from
    Scheduler ..> WaterNetwork : asks for a line, through the zone
    WaterNetwork "1" o-- "0..1" MasterDriver : head of the line
    Scheduler "1" ..> "*" ValveCoordinator : asks to start zones, told how many started
    ValveCoordinator "1" o-- "*" ZoneDriver : opens up to its limit
    Zone "*" --> "1" ValveCoordinator : counted on
```

`Zone` depends on `Delivery`, the structural protocol, rather than on `DeliveryResult` itself.
That is what keeps `zone.py` free of Home Assistant while `driver.py` — which owns the entity
adapters — necessarily is not. `DeliveryResult` satisfies the protocol without either module
importing the other.

`ManualActuator` is a third materialization of the *how* — but deliberately **not** a
`Driver`: there is no entity, no FSM, no safety layers to inherit. It shares only the delivery
**contract** (`→ DeliveryResult`), so the Zone settles its deficit identically whether the
water came from a valve or a watering can.

Reading keys: liters flow down the association `Zone → ZoneDriver` and truth flows back up as
a `DeliveryResult`; the `Scheduler` never touches drivers — it only decides *which zone when*
(and, with cycle & soak, may interleave another eligible zone during a soak pause);
`MasterDriver` reacts to the aggregate driver activity, it takes no decisions. The liveness
`ping()` lives in the abstract `Driver`, so both specializations inherit it.

The proposed `ValveCoordinator` sits between the two: the `Scheduler` asks it to start zones
and is told how many it started, so the scheduler still never touches a driver, and the
coordinator owns the `ZoneDriver`s it may open. The `MasterDriver` is not connected to it: the
master is hydraulic, and the coordinator's limit is electrical.

## The classes in detail

Attribute-by-attribute and method-by-method reference for each class, with the
responsibility that justifies every member. This expands the diagram above; the
diagram stays the source of truth for relationships.

### Environment — `environment.py`

The site: which sensors this installation declared, and therefore which models it can run.
*Declares* the master valve/pump but never commands it. Holds bindings — entity ids as opaque
strings — never readings.

| Member | Kind | Meaning |
|---|---|---|
| `temperature_sensor`, `rain_sensor` | attr | The two feeds every ET tier consumes |
| `humidity_sensor`, `wind_speed_sensor` | attr | What unlocks Penman-Monteith |
| `net_radiation_sensor` | attr | A pyranometer (solar radiation). Improves Penman-Monteith; not required, since the radiation is estimated from the diurnal range when absent. Named for the quantity the equation reads, not the one the user supplies — a rename worth doing |
| ~~`temp_max_sensor`, `temp_min_sensor`~~ | — | **Withdrawn.** The daily extremes are observed from the thermometer (`DiurnalRange`), not declared: the same entity in both fields gives a zero range and an evapotranspiration of exactly zero |
| `soil_moisture_sensor` | attr | What unlocks VWC mode |
| `rain_probability_sensor` | attr | Forecast feed behind the rain delay |
| `latitude` | attr | A property of the place: the astronomical radiation term, and the hemisphere flip of the seasonal Kc |
| `rain_sensor_type`, `backfill_days` | attr | How to read the rain feed, and how far back to replay |
| `rain_delay: RainDelayPolicy` | attr | Threshold + delay. The site supplies the *signal*; it never skips a watering itself |
| `yearly_rain_mm` | attr | Rain this calendar year — one sky over the whole garden (reference model D3). Note the asymmetry with the deficit, which is emphatically not shared |
| `declared_sensors → {SensorKind}` | property | Every kind actually bound to an entity |
| `satisfies(required) → bool` | method | The whole capability rule: `declared ≥ required` |
| `missing_for(required) → {SensorKind}` | method | *Which* sensor unlocks a model — what the UI needs to say |
| `accrue_yearly_rain(mm, year)` | method | Credits positive increments only; a decreasing reading is never rain (GH #123) |

### Zone — `zone.py`

The irrigation unit: owns its deficit, turns it into litres, and settles it with the driver's
reported truth.

| Member | Kind | Meaning |
|---|---|---|
| `name` | attr | Identity; also the `source` tag on its `Deficit` |
| `area_m2`, `efficiency` | attr | Irrigated surface, and how much of what is emitted reaches the root zone |
| `plant_family`, `manual_kc` | attr | What grows here — the seasonal curve, or an explicit override |
| `exposure`, `microclimate_factor` | attr | Sun and wind relative to an open site (GH #146) |
| `placement: Placement` | attr | Where the zone sits. `receives_rain` is true only outdoors; `driven_by_outdoor_et` also covers a patio |
| `actuation` | attr | **Proposed.** How the zone is watered: `VALVE` (default, today's behaviour) through its `ZoneDriver`, or `BY_HAND` through the [`ManualActuator`](#manual-actuation-a-valve-less-how-for-hand-watered-plants): a due zone becomes a request to a person, settled by **Mark irrigated**. Independent of `placement`: a pot in the greenhouse or a bed outdoors can be watered by hand, a house plant on a drip line by a valve. A `BY_HAND` zone names no network and no board, and the freeze gate does not apply to it |
| `d_max` | attr | **This soil's** reservoir. Only the clamping mechanism is shared across models |
| `threshold_mm` | attr | Deficit that triggers irrigation |
| `cycle_soak: CycleSoakRule` | attr | Dose/pause — a Zone rule, not a Scheduler policy |
| `network: WaterNetwork` | attr | **Proposed.** The line this zone draws water from, picked from the networks declared on the instance. Empty: the implicit network of its entry, one line with no master and no limit, which is today's behaviour. The zone form shows the network's master read-only, so the zone says what it depends on without holding a copy of it |
| `coordinator: ValveCoordinator` | attr | **Proposed.** The valve coordinator this zone's valve is wired to, picked from the coordinator entries declared on the instance. Empty: the virtual coordinator of its entry, limit 1, which is today's behaviour. A zone watered by hand names none |
| `deficit: Deficit` | attr | Its own deficit, carrying its reference frame. A new zone starts at 0 (D4) |
| `counters: WaterCounters` | attr | last / session / total / yearly delivered litres |
| `effective_kc(base_kc)` | method | Applies site exposure. The seasonal curve is deliberately *not* recomputed here — copying its plant table in would create the second source of truth anomaly E1 is about |
| `accumulate(dt_h, et_h, base_kc, rain_mm)` | method | `+ET·Kc·Δt − rain`, clamped. Rain is credited only when `placement.receives_rain` |
| `water_demand_l` | property | mm → litres via area and efficiency; litres are the contract towards the driver |
| `needs_water` | property | Deficit has reached the threshold |
| `begin_cycle()` | method | Opens a cycle, snapshotting the deficit it starts from |
| `credit_delivery(d: Delivery)` | method | **The one crediting formula.** Subtracts from the snapshot while a cycle is open, from the current value otherwise — which is what makes repeated real-time credits idempotent |
| `settle(d, source, at)` | method | Credits the final figure, stamps it, drops the snapshot — exactly once |
| `mark_irrigated(source, at)` | method | The hose case: nothing was measured, so the volume is inferred from the deficit being cleared |

### Scheduler

The *when* and the concurrency policy. It never touches drivers: it only decides which
zone runs in which window.

The seam is **decision versus execution**: the scheduler answers "may this zone water now, and
why not"; registering time listeners, spawning tasks and driving valves stay on the Home Assistant
side. It takes the world's facts (`is_running`, `is_throttled`) as arguments rather than reading
them, which is what makes the rules testable without a controller.

| Member | Kind | Meaning |
|---|---|---|
| `concurrency` | attr | `SERIAL` (today's behaviour) or `PARALLEL`. Naming it turns an emergent property — both handlers happen to bail when something is running — into a stated policy |
| freeze gate, by-hand requests | behaviour | **Proposed.** One scheduler per Environment entry, with no type of its own: its behaviour follows the facts it already has. The freeze interlock reads **its Environment's** temperature, so a greenhouse with its own sensors gates on the greenhouse air; it applies to `VALVE` zones whose placement is not `INDOOR`. A due `BY_HAND` zone becomes a request to a person rather than a command to a valve. See [Environments, water networks and valve coordinators: the three entry types](#environments-water-networks-and-valve-coordinators-the-three-entry-types) |
| `min_service_interval_s` | attr | Rate limit between service calls with the same key |
| `evaluate_scheduled(zone, is_running) → Decision` | method | The daily top-up. Deliberately does **not** consult the threshold: gating a schedule on the reactive threshold turns every scheduled run into a reactive one (AI-183). Only a zone already full is skipped |
| `evaluate_reactive(zone, is_running, is_throttled) → Decision` | method | Mode A: water once the deficit crosses the zone's threshold |
| `next_eligible(zones, is_running) → Zone` | method | Driest first — an ordering that needs no memory. Deliberately **not** a queue: a queue remembers what is waiting, which is the deferred design |
| `Decision` | value object | Water (with a `Trigger`) or skip (with a named `SkipReason`). The reasons are named because two of them — `ALREADY_RUNNING`, `THROTTLED` — are what make a watering look mysteriously missing |

**Deliberately absent:** time windows, calendars, the queue, parallel runs and interleaving during
soak. Those are deferred until a concrete demand for parallel zones appears (GH #74). Writing them
now would be building a mechanism for a question nobody has asked; what the module does contain is
behaviour that already runs, merely written where it can be read.
| `interleave_during_soak()` | method | During a soak pause it may interleave another eligible zone |

### Driver «abstract»

The common base of the two specializations: everything about commanding a physical entity
and not blindly trusting the answer.

| Member | Kind | Meaning |
|---|---|---|
| `entity_adapter` | attr | Adapter over the HA entity (`valve.*` or `switch.*`) |
| `adaptive_timeout` | attr | Verification window adapted to observed latency (rolling mean + 3σ) |
| `safety_layers` | attr | Watchdog; close on error/stop/restart |
| `ping_interval_min` | attr | Active liveness: periodic ping, not just passive state |
| `turn_on() / turn_off()` | method | Command with state confirmation (and bounded retry with backoff) |
| `ping() → alive \| unreachable` | method | Reachability check independent of commands |

### ZoneDriver

The *how* for a single zone: receives liters, picks the actuation strategy, returns the
truth.

| Member | Kind | Meaning |
|---|---|---|
| `delivery_mode` | attr | `native_volume` when the device doses in liters, otherwise `time × flow` (seconds via flow rate) |
| `flow_rate_lpm` | attr | Nominal guard flow rate, stored in L/min; the form takes and shows L/h (gal/h in imperial) |
| `flow_telemetry` | attr | Flow telemetry, when available (optional) |
| `zero_flow_guard` | attr | Guard against zero-flow sessions |
| `deliver(liters) → DeliveryResult` | method | Actuates the request and reports delivered liters with their degree of truth |

### MasterDriver

Coordinates the shared hydraulics (pump / master valve). Reacts to aggregate driver
activity, takes no decisions, has no notion of liters.

| Member | Kind | Meaning |
|---|---|---|
| `start_delay_s` | attr | When the master starts relative to the first zone valve. **Default: 1 s after the first zone valve is confirmed open.** Optional and signed: negative starts it before the zone |
| `off_delay_s` | attr | Linger after the last zone of the sequence is **confirmed** closed. **Default: 5 s.** Optional |
| `follow(any_zone_driver_active)` | method | ON while any ZoneDriver is active, OFF (after the linger) when none is. Across a serial sequence the scheduler keeps it ON through the gap between zones (see the design decision below) |

The written code still has `off_delay_s = 10` and no start delay; both change to the values
above when the master is wired, together with the scheduler.

### WaterNetwork (proposed)

**Proposed, not written. It lands with the scheduler work.** The hydraulic line zones draw from,
and the point where entries that share it converge. Without it, two entries on one main would
each have to ask the other what it is running, a protocol between peers with nobody holding the
truth; with it, each scheduler asks one object, and the schedulers never talk to each other.

**A network is an entry of its own**, one of the two types offered when an entry is added, beside
the Environment. It is declared once, with its own device and its own options form,
and **each zone names the network it draws from**. The scheduler reaches the network through the
zone: it never holds one itself.

| Member | Kind | Meaning |
|---|---|---|
| `name` | attr | The network entry's title. Zones pick it from a list of the networks declared on the instance, so a typing slip cannot split one line in two |
| `hydraulic_limit` | attr | Optional, and one of two: a **usable maximum flow** in L/h (gal/h in imperial), the unit of the zones' flow rates (the form suggests about 80% of the pump's plate; the software applies no margin of its own), compared with the **design** flow rates of the zones running on it, never the measured ones; or a **maximum number of lines**. Absent, parallel is free on the hydraulic side |
| `master` | attr | The master valve or pump at the head of the line, if any: one per network, declared once, so two entries sharing a pump never command it twice. Every zone on the network uses it; a zone that does not is on another network. It stays on while any entry on the network has a zone running or next in its sequence |
| `admit(zone) → bool` | method | Whether the line can take one more zone now, against its limit |
| `next_turn(waiting entries) → entry` | method | Turn-taking between entries that have zones waiting: **one zone at a time, in turn**, so no entry waits through another's whole sequence. Within an entry, order stays its scheduler's |

Serial and parallel remain the entry's setting; the network decides only whether there is water
for one more line, and whose turn it is.

**The defaults**, each the safe one and each today's behaviour where today has one:

| Case | What happens |
|---|---|
| No network entry declared | Each Environment entry has an **implicit network**: one line, no master, no limit. The terrace and the four zones on one line are configured without meeting a network at all |
| A zone that names no network | It draws from its entry's implicit network |
| A network entry removed while zones name it | Those zones return to their entry's implicit network, and the form says so, naming them, before the removal is confirmed |
| A network named by a zone is not loaded, or failed to set up | The zone **does not start** until the network answers. Opening a zone without knowing whether a pump must be started is worse than waiting; the zone reports why it is waiting |

### ValveCoordinator (proposed)

**Proposed, not written. It lands with the scheduler work, together with its wiring.** The
electrical side of "how many at once": the only object that knows how many zone solenoids
its controller board (transformer) can hold open. A 24 VAC controller has one transformer, one common wire
and a set of station terminals, and the transformer is sized for a fixed number of solenoids
(on most residential controllers, one station plus the master). Raised in GH #320.

| Member | Kind | Meaning |
|---|---|---|
| `max_active_zones` | attr | Zone valves the board (transformer) may hold open at once, **declared by the user net of the master**: on a board rated for one station plus master, the limit is 1. The coordinator never counts the master, so neither it nor the scheduler needs to know how the board is wired |
| `name` | attr | The valve coordinator entry's title. Zones pick it from a list of the coordinators declared on the instance, so one coordinator can hold zones of several Environments (a garden and a greenhouse on one controller) and a typing slip cannot split one coordinator in two |
| `zone_drivers` | attr | The `ZoneDriver`s of the zones that name this coordinator, from whichever Environment they belong to |
| `start(zones) → started` | method | Opens as many of the requested zones as the limit allows and reports **how many it started**. It does not queue: the scheduler knows the answer and keeps the rest waiting |
| `on_outside_open(driver)` | method | A valve opened by hand or by another automation is only observed, never refused or closed: NeverDry does not own the relays. An overlap on one board warns, naming both valves, and in timer mode marks both runs as suspect, since neither delivery can be trusted |

**A valve coordinator is an entry of its own, the water network's sibling.** It is one of the types
offered when an entry is added, declared once with its own device and options form, and **each
zone names the coordinator its valve is counted on**, as it names the network it draws from. The two are
independent: a network is the water, a coordinator is how many valves may be open at once, and
one controller can drive valves on two networks just as one network can be fed through two
controllers.

**General, not a model of any controller.** The coordinator holds one number, how many valves
may be open at once, whatever imposes it: a 24 VAC transformer sized for one solenoid, a
controller that runs one station at a time by design, a relay module with an interlock, a cloud
service that accepts one run per device. NeverDry does not model the device behind it, its
wiring, its protocol or its timing; whoever configures it reads the limit off the device and
declares it. A property that belongs to one brand of controller is not added here: it stays in
that controller's integration, or it is expressed through the entities Home Assistant already
exposes.

**The defaults** mirror the network's, each the safe one:

| Case | What happens |
|---|---|
| No valve coordinator entry declared | Each Environment entry has an **implicit virtual coordinator** holding every one of its zone valves, limit 1: today's behaviour, one valve at a time, stated instead of emerging from the handlers |
| A zone that names no coordinator | Its valve is counted on its entry's virtual coordinator |
| A coordinator entry removed while zones name it | Those zones return to their entry's virtual coordinator, and the form says so, naming them, before the removal is confirmed |
| A coordinator named by a zone is not loaded, or failed to set up | The zone **does not start** until the coordinator answers. Opening a valve without knowing how many the transformer already holds is worse than waiting; the zone reports why it is waiting |
| A zone watered by hand | Names no coordinator: it has no solenoid |

**Composition, not a third `Driver`.** `Driver` means one actuator and one entity: adapter,
confirmed commands, adaptive timeout, watchdog and liveness probe all assume a real switch.
The coordinator has no entity of its own; it owns several `ZoneDriver`s. Making it a `Driver`
subclass would give it a watchdog and a latency over an entity that does not exist, the same
reason `ManualActuator` is not a `Driver` either.

**The hard interlock stays in the hardware.** Firmware that owns the relays enforces the limit
on every open, including a manual one (ESPHome: `interlock:` on GPIO switches). The coordinator
keeps NeverDry's own commands inside the limit and makes an outside overlap visible; it does
not replace the interlock.

### DeliveryResult

The return trip of the truth: the driver does not just execute — it states how much it
delivered and how much that figure can be trusted.

| Member | Kind | Meaning |
|---|---|---|
| `liters_delivered` | attr | Liters actually delivered, as far as the backend allows to know |
| `quality` | attr | `measured` · `estimated` · `partial` · `delayed` · `low_confidence` |
| `elapsed_s` | attr | Real session duration |
| `revise(measured_liters)` | method | Late revision for slow-reporting backends (e.g. Hydrawise): the true measure arrives later and corrects the estimate |

**Proposed addition (backlog AI-163, not yet part of the model):** a `device_reported`
quality level between `measured` and `estimated`, fed by the device's own end-of-session
report (duration + start/end volume — e.g. Sonoff SWV via Z2M). Some valves cannot stream
flow in real time but do report a trustworthy session total: more truthful than a
`flow_rate × time` estimate, less than live metering. It belongs to the driver as a
capability and will land with the driver abstraction.

## Design decisions

### The deficit lives in the Zone; the site holds feeds, not state

The `Environment` is not a global deficit. It owns the shared **feeds** — the
temperature sensor (→ ET) and the rain sensor — and broadcasts them; each Zone
accumulates **its own** deficit (`+ET·Kc·Δt − rain − irrigation`). Irrigating a
zone resets only that zone. A new zone starts at 0 rather than inheriting a
global reference, which drifts high under per-zone irrigation. The old global
"Dryness Index" accumulator is retired as ET state (kept only as an interim
system-level value for the single-probe VWC mode). The full rationale, reference
frames, and the retire/keep table are in the
[Water-Balance Reference Model](design_water_balance_reference_model.md)
(decisions D1–D5).

### Master valve/pump: declared at site level, executed by a Driver

The master valve is not scheduling logic — it takes no decisions. It reacts to the aggregate
execution state (an OR over zone drivers), with an off-delay to avoid pump cycling during
sequential zone runs. It is shared hydraulic infrastructure, like the global sensors, so its
*configuration* lives at system level (as requested in GH #95: "master entity configurable at
integration level").

Its *execution* however is a Driver: modeling it as a Driver specialization means the safety
layers (never leave the pump running on error/stop/restart) are written once in the base and
inherited — instead of duplicating watchdog and error handling inside "system" as a special
case.

**What the user configures: the entity, and nothing else.** *Decided 2026-10-10 on GH #95,
from @MrEcosse's working setup and @safepay's review.* Everything else has a default that is
safe for both kinds of master, so the kind is never asked:

- **Start order: 1 s after the first zone valve is confirmed open.** A pump-start relay needs
  exactly this, or it runs against a closed line. A master valve tolerates it, since a zone line
  without pressure for one second harms nothing. The opposite default, master first, would be
  right for valves and harmful for pumps, so it is the one that stays optional.
- **Kept on for the whole sequence.** While the scheduler has another zone to run in the same
  sequence, the master stays on through the gap between zones, however long the inter-zone delay
  is. The linger applies only after the last zone, and counts from its **confirmed** close, not
  from the command.
- **Linger: 5 s**, the figure from the field.
- **Every zone on the network uses it.** A zone that does not (drip from the mains beside a lawn
  on a tank pump) draws from a different source, so it names a different network, one with no
  master. *Revised 2026-10-10:* an optional per-zone "does not use the master" flag stood here
  while the network was a name on the entry and two sources inside one entry had no other way to
  be told apart. With zones naming their network, the flag described a second network under
  another name, and it is retired.

Optional, for whoever wants to look under the bonnet: a signed start offset and the linger. The
master belongs to the **water network** it heads, not to an entry and not to a zone: it is
declared once on the network, two entries whose zones draw from one network share one master,
and a second pump is a second network. It is **not**
counted in a controller board's (transformer's) electrical limit: it stays hydraulic, and that limit is declared
net of it (the `ValveCoordinator`).

### Cycle & soak: a Zone rule

Cycle/soak parameters depend on soil infiltration rate and zone properties (slope, soil
type), so they are per-zone configuration. The *execution* of the cycles is driver/controller
mechanics, but the rule lives in the Zone.

### DeliveryResult: the driver reports the most truthful delivered value it can

*From the GH #74 review (fpytloun, 2026-07-06).* Estimating delivered liters from expected
flow can diverge badly from reality — a dirty filter reduces the actual flow rate; a backend
like Hydrawise refreshes measured values only periodically, so the true figure may arrive
late. And for some backends, **command acceptance, physical valve state, and final measured
delivery are three distinct moments**, not one.

The driver therefore returns a **DeliveryResult**, not a bare number: delivered liters plus a
**quality qualifier** — `measured`, `estimated`, `partial`, `delayed`, `low-confidence`. Rules:

- The driver always reports the *most truthful* value available for its backend: cumulative
  flow-meter reading first, flow-rate integration second, configured flow × elapsed time as
  the estimated floor, each labeled accordingly.
- A result may be **revised**: a backend that reports measured volume late (e.g. a periodic
  API refresh) first returns an estimated/`delayed` result and corrects the deficit settlement
  when the measured figure lands.
- A `partial` or zero result with the valve confirmed open still **settles the deficit** with
  the best available estimate — the water was physically delivered whether or not it was
  measured (this is the field bug behind the zero-measured-flow timeout: an unmeasured
  session must never leave the deficit untouched and trigger a retry loop).

The Zone consumes the DeliveryResult to settle its deficit; the quality qualifier flows into
diagnostics (session log, `SESSION_RESULT`) so the user can see *how* the figure was obtained.

**Cycle & soak makes delivery self-correcting.** When a zone waters in cycles, the gap
between the liters requested and the most truthful delivered value of one cycle is simply
added to the next cycle's request: an under-delivery (dirty filter, low pressure, partial
result) is **replenished within the same session**, instead of surfacing a day later as
residual deficit. This is a direct synergy between the DeliveryResult contract and the
cycle & soak rule — it requires truthful per-cycle accounting to work.

### Manual actuation: a valve-less *how* for hand-watered plants

*Idea 2026-07-26.* Not every plant has a valve. A **house plant** is watered by hand, so its
"actuation" is a person: NeverDry raises an **alert** when the deficit says water is due, and
the user presses **Mark irrigated** once they have watered. `ManualActuator` models this as a
third materialization of the *how* — a materialization that proves the abstraction, because it
has **no hardware at all**.

It deliberately does not extend `Driver`: there is no entity, no FSM, no watchdog,
no liveness. It shares only the delivery **contract** (`→ DeliveryResult`), so the Zone settles
its deficit identically whether the water came from a valve or a watering can. Two existing
pieces are reused rather than reinvented: the alert is a **notification**, and **Mark
irrigated** is the existing `reset_deficit` action, here doubling as the delivery confirmation.
The human-paced, asynchronous nature is already covered by the DeliveryResult contract:
`request_irrigation()` returns a `delayed` pending result and `mark_irrigated()` the final one,
tagged with a new **`declared`** quality (assumed/declared by a human, not measured) — a person
is simply the extreme case of "a backend that measures late".

**Actuation and model are orthogonal.** A house plant picks the manual *how* **and** the right
*how much*: indoors the demand is not weather-driven, so it pairs with a VWC / indoor
water-balance model, **not** `ETModel`. The two axes (`Driver` family × `WaterBalanceModel`
family) compose freely — a house plant is just one corner of that grid.

**Settled 2026-10-11:** the manual *how* is chosen per zone by `Zone.actuation` (`VALVE` or
`BY_HAND`), independent of `Zone.placement`, which says where the zone sits. Neither is a type of
the entry.

**To explore (open questions, not decided):**
- A **pot-based characterization** for house-plant zones: today a zone's water is `area × root
  depth`; a potted plant is bounded instead by **pot volume**, and its evapotranspiring surface
  is better described by **plant height / canopy diameter** than by ground area. This likely
  wants its own "pot" water model (a sibling of the VWC/ET models) rather than stretching the
  open-field geometry.

### Serial vs parallel irrigation: a Scheduler policy

*From the GH #74 review (fpytloun, 2026-07-06) and the shared-resource discussion earlier in
the thread.* Whether two zones may run at the same time is not a property of a zone or a
driver — it is a property of the shared hydraulics (one well, one pipe, one pump) and
therefore a **Scheduler policy**:

- **Serial** (default for shared-resource systems): one zone runs at a time; eligible zones
  queue.
- **Parallel**: zones with independent hydraulics may overlap.
- **Soak interleaving**: soak pauses are schedulable time — while one zone is soaking,
  another eligible zone can run its cycle, then control returns. This keeps total watering
  windows short without violating the one-valve-at-a-time constraint.

The queue/scheduler implementation stays deferred until real demand for parallel zones shows
up (as agreed in GH #74), but the model reserves the concept now so cycle & soak (a Zone
rule) and concurrency (a Scheduler policy) don't get entangled when either lands.

This policy is the **hydraulic** half of concurrency only. The electrical half, how many
solenoids a board (transformer) can hold open, is not the scheduler's: see the next decision.

### Electrical parallel is not hydraulic parallel: a coordinator, not the scheduler

*From GH #320 (2026-09-27) and the decision taken on it on 2026-10-10.* Two zones running at once
must pass two independent limits, and they belong to different objects:

| Limit | Question | Who knows it | Owner |
|---|---|---|---|
| **Hydraulic** | Does the water allow two zones at once? | The person who sees the plumbing, and the pump's plate when there is one | `WaterNetwork`: an optional limit, either a usable maximum flow or a maximum number of lines; the `SERIAL`/`PARALLEL` setting stays the scheduler's |
| **Electrical** | Can the board (transformer) hold two solenoids open at once? | Printed on the transformer and on the solenoid datasheet | `ValveCoordinator`: `max_active_zones`, declared net of the master |

The two are never merged into one number, because they fail differently. A hydraulic overlap
gives two weak runs and two wrong flow rates; an electrical one sags the voltage, and valves
buzz, fail to open or drop out mid-run, with the run still reported as delivered in timer mode.

The path of a decision: the scheduler decides which zones should run now, asks the water network
whether the line can take them and whose turn it is, and asks the coordinator to start them; the
coordinator starts as many as its electrical limit allows and answers how many; the scheduler
keeps the rest waiting. **A run goes parallel
only when both say yes**, so permission given for the plumbing never crosses the transformer.

The **master** valve or pump stays on the hydraulic side. It follows zone activity as it does
today and is not counted by the coordinator; the user declares the coordinator's limit net of
it. Tying the master to the electrical count would make both the coordinator and the scheduler
know how each board is wired.

With no valve coordinator declared, each Environment's virtual coordinator (limit 1) keeps today's
behaviour, so an installation that declares nothing changes nothing.

**Not written yet.** The coordinator, the water network and the wiring
between them land together with the scheduler work. Until then the only action is
documentation: a relay board on one transformer should have its interlock set in firmware.

### Environments, water networks and valve coordinators: the three entry types

*Decided 2026-10-10, from discussion #239 and GH #320; revised the same day: the network became
an entry of its own, named by each zone; revised 2026-10-11: entries are not typed by place, and
the valve coordinator became an entry of its own, the network's sibling.* "One valve at a time" turned
out to be three questions, and each has its own object:

| Object | Question | Level |
|---|---|---|
| `Scheduler` | **When**, and in what order, for the zones of one Environment | One per Environment entry |
| `WaterNetwork` | **Is there water** for one more line, and whose turn is it? | One per network entry, on the instance; one implicit per Environment entry when none is named |
| `ValveCoordinator` | **Is there power** for one more solenoid? | One per valve coordinator entry, on the instance; one implicit virtual coordinator per Environment entry when none is named |

A zone starts only when its network and its coordinator both say yes.

**There are three entry types: Environment, water network and valve coordinator.** An Environment entry
is a climate: its sensors, its latitude, its scheduler and the zones that sky falls on. A garden
is one; a greenhouse with its own sensors is a second. A water network entry holds a name, the
master if there is one, and the hydraulic limit if one is declared. A valve coordinator entry holds a
name and how many zone valves may be open at once. The zone is always the
sub-element of an Environment, never an entry type; it names the network it draws from and the
coordinator its valve is counted on. As a rule of thumb, one coordinator per transformer: whatever
supplies the solenoids is what limits how many can be open.

**Why the coordinator is a sibling and not a field.** The first form kept it as a board name typed
or created in the zone form. That is the shape the network had just left, for the same reason: a
coordinator holds zones of several Environments, so its limit cannot live in any one of them, and
a name typed in two forms can split one coordinator in two. As an entry it is declared once, with its
limit, and the zones pick it from a list.

**Place and hand-watering are facts of the zone, not types of the entry.** The first form of this
decision typed entries as outdoor, greenhouse or indoor and hung three behaviours on the type. Each
turned out to belong elsewhere:

| Behaviour once tied to the entry type | What it really is | Where it lives |
|---|---|---|
| No rain in a greenhouse or indoors | Where the zone sits | `Zone.placement` |
| A request to water by hand indoors | How the zone is watered | `Zone.actuation`: `VALVE` or `BY_HAND` |
| The freeze gate outdoors, not in a greenhouse | What the air around the valves reads | The Environment's temperature sensor |

The type by place confused where a plant is with how it is managed: a pot in the greenhouse
watered by hand, or a bed outdoors watered with a can, had no type to go in. House plants can sit
in the garden's Environment as `INDOOR`, `BY_HAND` zones; a second Environment is needed only
where the climate genuinely differs.

**A network is an entry of its own, and each zone names one.** It is configured once, and every
zone that draws from it picks it from a list. The scheduler of an Environment reaches the network
through its zones. A greenhouse on the garden's mains has its zones name the garden's network,
and the two schedulers take turns through it; a greenhouse on its own tank names nothing, or a
network of its own.

**Why the zone and not the entry.** The first form of this decision put the network on the
entry, as a name typed the same on every entry that shared a line. That held one source per
entry, and failed on two sources inside one: drip from the mains beside a lawn on a tank pump. It
needed a per-zone flag, "does not use the master", which was a second network described under
another name. With the zone naming its network, the two sources are two networks in one entry,
the flag goes, and the master is declared in exactly one place.

**Why not a copy per entry.** Declaring the master on every entry that uses it reads as more
explicit, and it lets two entries on one line name two different masters for one pump, command
it twice, and each ask the other whether it is still in use before switching it off. The network
entry holds it once; each zone shows it read-only, which is the explicitness without the copy.

**Simple by default: every new field is optional.** Water network, valve coordinator, hydraulic limit,
master and actuation all have a default that is today's behaviour, and a simple garden is
configured without seeing any of them. Two installations are the test this design must pass:

- **A terrace with seven pots** on drippers: one entry, seven zones, nothing else asked.
- **Four independent zones on one hydraulic line**, one Environment entry: nothing else asked
  either.

If either of them meets a question about networks, coordinators or types, the form is wrong, not the
user.

- The **first entry** on an instance asks nothing new: it is an Environment, as every
  installation today is.
- **Adding an entry** from the second on opens a menu of three: **Environment**, **water
  network** and **valve coordinator**. Whoever adds a second entry is already doing something beyond the
  simple case.
- **The zone form** gains three optional fields in a collapsed section: the **network**, listing
  the network entries on the instance (empty: the entry's implicit network, exactly as today), the
  **valve coordinator**, listing the coordinator entries on the instance (empty: the entry's virtual coordinator,
  limit 1, exactly as today), and **watered by hand** (off: a valve, as today).
  With watered by hand on, the valve, network and coordinator fields do not apply.

**Existing entries are Environments**, and nothing changes in them. Zones whose placement is
greenhouse or indoor keep working as now; moving them to an Environment of their own is a choice,
useful only where the climate genuinely differs.

| Installation | Entries | Network | Coordinator |
|---|---|---|---|
| A terrace with seven pots | 1 Environment | none: implicit | none: virtual |
| Four zones on one line | 1 Environment | none: implicit | none: virtual |
| Four zones on one line, with a pump | 1 Environment + 1 network | the network, named by the four zones | none: virtual |
| Two Netro controllers on one main | 1 Environment + 2 coordinators | none: implicit | each zone names its controller, limit 1 each |
| Garden and greenhouse with its own sensors, one controller on one main | 2 Environments + 1 network + 1 coordinator | the network, named by the zones of both | the coordinator, named by the zones of both |
| Garden on the mains, greenhouse with its own sensors on a tank | 2 Environments | none, or a network for the tank | none: virtual, one per Environment |
| Drip from the mains beside a lawn on a tank pump | 1 Environment + 1 network | the lawn names the pump's network; the drip names none | none: virtual |
| House plants beside the garden | the garden's Environment | none: watered by hand | none |

The defaults (none declared, a zone naming none, one removed, one not loaded) are listed with
the [WaterNetwork](#waternetwork-proposed) and the [ValveCoordinator](#valvecoordinator-proposed). The validation of the hydraulic limit
runs from both sides: a zone is checked against the network and the coordinator it names, and
changing a network's limit or a coordinator's checks again every zone that uses it, so the order of
configuration does not matter. The full reasoning is in the scheduler note
(`design/scheduler.md`, sections 8.7 and 8.8).

### Driver liveness: an active availability ping, not just passive state

Passive observation of the HA entity is not enough to know a valve is reachable. A WiFi
valve that drops off the network is marked `unavailable` by its integration; a **Zigbee
valve** often is not — availability tracking in Z2M/ZHA is optional or slow for
battery-powered (sleepy) end devices, so the entity can keep showing a stale `off` for hours
after the device is gone. Discovering that at irrigation time is too late.

The Driver base therefore owns an **active liveness probe**: every *N* minutes (configurable)
it verifies the device is actually reachable, using the cheapest backend-appropriate means —
an attribute read / availability-topic check for Zigbee (MQTT), the entity's own
availability for backends that report it honestly. Probe outcomes feed the existing
machinery rather than inventing a new one: a failed probe drives the FSM `unreachable` state
and the `UNREACHABLE_PASSIVE` / `UNREACHABLE_AT_IRRIGATION` notifications, so the user learns
about a dead valve *before* the next scheduled run, not from a failed one.

### RFC: dissolve `System` into `Environment` + capability-matched models

**Status: Accepted (ADR) — 2026-08-16.** Supersedes the earlier "rename System → Weather/Environment"
note. Promoted on the condition this section set for itself: it is wired. `Environment` exists and is
built from the config entry; each model declares `required_sensors` in the same vocabulary the site
declares bindings in; `models_offered_by` joins the two; and the form asks for the inputs the richer
tiers need, so `declared_sensors` can actually satisfy them.

Two consequences of wiring it are worth recording, because neither was foreseen when this was
written:

- **The dropdown is not filtered to what the site supports.** A form that narrows its options at
  render time goes stale within the same submission, since it does not react to what is typed — and
  a user who cannot see a method has no way to learn which sensor unlocks it. Every method is
  offered, and the choice is refused on submit with the missing sensors named. The capability match
  is therefore a *validator* as well as a selector, which is a role this RFC did not give it.
- **A stored choice degrades rather than fails.** A sensor can be removed after the method was
  chosen. Refusing to start would stop the watering; running a model whose inputs are missing would
  produce a confident wrong number. The third way — fall back to the richest supported model — is
  the only one that keeps both the water and the honesty, and it is what `build_model` does.

**What is not yet true**, and is deliberately not hidden by this promotion: the recorder backfill
still replays history through the temperature-only formula, so a site on a higher tier is
bootstrapped with the simple estimate. Replaying Penman-Monteith needs historical humidity, wind and
radiation — a different problem from choosing a model for the present.

**Problem.** The object today called **System** is a catch-all that bundles three unrelated
responsibilities, and one of its "global params" is not global at all:

| System attribute (today) | Really belongs to | Why |
|---|---|---|
| temperature + rain sensors (feeds) | **`Environment`** | Environmental inputs the zones consume |
| **α** (ET sensitivity) | **`ETModel`** | Used **only** by the simple temperature ET tier — verified: `ETModel.et_hourly = max(0, α·(T−T_base)/24)`. Hargreaves uses its own 0.0023 + extraterrestrial radiation; Penman-Monteith is an energy balance; VWC has no ET. α is meaningless for every other model, so it cannot be a system-global param |
| **D_max** (deficit clamp) | **`Zone`** (value), water-balance config (default) | The *mechanism* is shared — every model clamps its `Deficit` to `[0, D_max]`, ET tiers *and* VWC. The *value* is not: D_max is the zone's soil reservoir, set by soil type × root depth, so a sandy zone under shallow turf and a clay zone under deep shrubs do not hold the same water. Shared mechanism ≠ shared value — every zone has a Kc too, without Kc being global. See "D_max is per-zone" below |
| master valve / pump (declaration) | **`MasterDriver`** | A hydraulics/actuation concern, not an environmental one |

With those redistributed, **nothing is left on `System`** — so `System` is **dissolved**, not renamed.

**`Environment` — the declared sensor inventory.** `Environment` becomes the user's answer to
"which sensors do you have?", declared at install from the config flow. It owns the bindings to
**all** model inputs, not just the temperature+rain of the simple tier:

- temperature, rain
- relative humidity, wind speed, net radiation (Penman-Monteith)
- daily tmax / tmin (Hargreaves), plus **latitude** (site constant → the astronomical radiation term)
- system soil-moisture (VWC) probe
- **`rain_probability`** — forecast feed (see the forecast extension below)

**Capability matching.** Each `WaterBalanceModel` tier declares the sensors it requires; a `Zone`
offers **only** the models whose requirements are satisfied by what `Environment` declares:

```
Environment.declared_sensors  ⊇  Model.required_sensors   ⇒   Zone offers that model
```

The requirement per tier is already encoded implicitly in the typed step input; the RFC promotes it
to an explicit `required_sensors` set on each model + a matching method on `Zone`:

| Model | `required_sensors` (from its `*Step`) |
|---|---|
| `ETModel` | temperature |
| `HargreavesModel` | tmax, tmin *(latitude/day-of-year derived)* |
| `PenmanMonteithModel` | temperature, humidity, wind, net radiation |
| `VWCSystemModel` / `VWCPerZoneModel` | soil-moisture probe |

*Naming note.* `VWCSystemModel` still carries the name of the dissolved object. It is left alone
deliberately: whether a site-level probe is a coherent category at all is the open question of the
soil-moisture model, and renaming the class before that is settled would only have to be undone. The
same question decides whether the two VWC classes collapse into one.

*(rain is a credit feed shared by all ET tiers, defaulting to 0 — not a gating requirement.)*

So a user with only a temperature sensor gets `ETModel`; add humidity + wind + radiation and
Penman-Monteith unlocks; add a soil probe and VWC mode becomes available — per zone, automatically,
with no model chosen by hand that the hardware can't feed.

**Forecast extension** (unchanged from the earlier note, now as `Environment` properties, config-flow
configurable):
- **`rain_probability`** — forecast rain probability, exposed as a feed alongside temperature and rain.
- **`rain_delay_above_threshold`** — above a configurable probability, delay irrigation by a
  configurable amount. A *decision input* the environment provides; the Scheduler/Zone consumes it
  (the environment supplies signals, it does not itself skip watering).

**Where the rain rules live — resolved 2026-08-09.** `rain_delay` is a **Zone** rule, not a
`Scheduler` policy. The test that settles it: a Scheduler that must *know* indoor zones are unaffected
by rain has already conceded the rule belongs to the Zone — it is asking each zone whether it is
exposed, in order to decide on the zone's behalf. It also matches the criterion this document already
uses elsewhere: cycle&soak is a Zone rule because it concerns that zone's soil; serial/parallel is a
Scheduler policy because it arbitrates a **shared resource**. Rain is not shared — it falls on a zone
or it does not.

The property to model is not a bespoke `rain_delay` flag but **whether the zone is open to the sky**,
expressed as a categorical `Zone.placement`:

| `placement` | Receives rain | Driven by outdoor ET |
|---|---|---|
| `outdoor` (default) | yes | yes |
| `patio` | no | yes |
| `greenhouse` | no | sheltered — own regime |
| `indoor` | no | no (moisture-threshold logic instead) |

`patio` is what makes the categorical necessary rather than a boolean: a covered terrace is fully
outdoors for temperature and wind, yet receives no rain. Without it one is tempted to collapse
"receives rain" and "is outdoors" into a single flag, which the middle rows show are independent.

One attribute then gates three things that must not be allowed to diverge: the **measured** rain
credit, the **forecast** rain delay, and whether the outdoor ET model applies at all. That also
answers the double-counting question below — forecast and measured rain pass through the same
per-zone gate, so they cannot disagree about whether a zone sees rain.

Note the consequence for today's code: `_broadcast_to_zones` credits `rain` to every registered zone
unconditionally, and no indoor/outdoor discriminator exists yet. That is latent rather than live —
there are no non-outdoor zones today — but it becomes a defect the moment `placement` ships, so the
gate and the discriminator must land together.

*Naming note.* `placement` rather than `environment`, deliberately: this RFC already uses
`Environment` for the site-level sensor inventory, and `exposure` is taken by the microclimate factor
(#146). Three overlapping words at two different levels is a collision worth resolving before wiring,
not after. `placement` says literally what it holds — where the zone sits.

**D_max is per-zone — resolved 2026-08-09.** The earlier reading ("genuinely shared, stays a global
setting") conflated two things. What is shared is the **mechanism**: every model clamps its `Deficit`
into `[0, D_max]`. The **value** is a property of the zone's soil — D_max is the reservoir that soil
can hold, a function of soil type and root depth. A sandy zone under shallow turf and a clay zone
under deep shrubs currently receive the same reservoir, which is simply wrong. Shared mechanism does
not imply shared value: every zone has a Kc, without Kc being global.

The scaffolds already assume this. `Deficit` carries `d_max` as its own field and exposes
`clamped()`; `WaterBalanceModel` surfaces it as a property. And today's code already keeps a per-zone
field — `IrrigationZoneSensor._d_max` — merely *seeded* from the system value
(`self._d_max = dryness_sensor._d_max`, itself a reach into another object's private, cf. anomaly
A1). So the work is to expose it in config and stop seeding it globally, not to relocate state.

**Caveat — do not derive it silently.** In FAO-56 the reservoir is `TAW = (θ_FC − θ_WP) · Z_r`, and
the model has **no wilting point**: `const.py` defines `DEFAULT_FIELD_CAPACITY = 0.30` and
`DEFAULT_ROOT_DEPTH = 0.30` but nothing for θ_WP, while `DEFAULT_D_MAX = 100.0` is an independent
constant, not derived from either. Deriving D_max properly means the soil-type presets of #126 must
carry a wilting point as well — and the resulting values land materially lower than today's default
(a loam at Z_r = 0.30 m gives roughly 45–50 mm, about half). That is a real change in irrigation
behaviour and must be made deliberately, with a migration for existing installs, not slipped in.
Note also the natural pairing it exposes: D_max ≈ TAW, while the zone's existing trigger threshold
plays the role of RAW = p · TAW.

**Open questions before Accepted.** None outstanding. This RFC raises the **α-ownership** finding
(α modeled on `System` but usable only by `ETModel`) — to be logged as an anomaly in the
[Domain Model Anomalies](design_domain_model_anomalies.md) audit when promoted. Promotion to Accepted
still waits on wiring, and on the `Zone` class the model presumes but the code does not yet have
(anomaly A1).

## Mapping to current code (2026-08-09)

Every object in this document now has a module. The table says, for each, how far the written
class is from the code that still does the work — because **none of the scaffolds is wired**.
Read the two columns as "what the model says" against "what runs today".

| Object | Current state (2026-08-16) |
|---|---|
| Environment | ✅ **wired**: `environment.py` holds the site — bindings, backfill window, latitude, yearly rain — and `DrynessIndexSensor` reads it rather than keeping eleven loose attributes. `satisfies`/`missing_for` are the capability match, and they now have a caller: `models_offered_by` |
| Zone | ✅ **wired**: every path that changes zone state goes through `zone.py`. `settle` (amount known) and `mark_irrigated` (outcome known) are named apart rather than reconciled. Still designed, not implemented: cycle & soak, placement, per-zone `d_max` |
| Scheduler | ✅ **wired**: both handlers ask `scheduler.py` and act on the answer; the serial concurrency policy has a name, and skips are reported as `THROTTLED` / `ALREADY_RUNNING` instead of one falling silent. `next_eligible` stays unreached: the queue is deferred (DA-3) |
| ZoneDriver | ✅ **command layer wired** (2026-08-16): `sensor.py` builds a `ZoneDriver` and the controller issues `async_turn_on/off`. This delivered the already-open confirmation fix, which had lived in the unwired copy, and the entity adapter for `valve.*` (GH #94). The **delivery loop** still lives in the controller — the next seam. `valve_operator.py` is superseded and imported by nothing, kept until the field test is done (AI-270) |
| MasterDriver | ❌ not implemented (GH #95); the class exists in `driver.py`, reached by nothing |
| ManualActuator | ❌ not implemented; `ManualActuator` in `driver.py`, unreached. For hand-watered house plants — a *how* with no hardware |
| WaterBalanceModel | ✅ **wired**: `DrynessIndexSensor` holds a model and calls `step()`; its `_deficit` is a view onto it, so there is one storage. All four methods run and are selectable — `ETModel`, `HargreavesModel`, `PenmanMonteithModel`, `VWCSystemModel` — with `VWCPerZoneModel` still unreached (AI-174). Two inputs are derived rather than declared: the daily extremes (`DiurnalRange`) and the day's solar energy (`DailySolarEnergy`), which feeds a computed net radiation |
| Deficit | ✅ **wired**: the value object carries millimetres *and* the reference frame, and the frame a zone reports now follows the model actually running rather than defaulting to ET |

The refactoring direction is symmetric on both axes: make the **Driver** base explicit when
implementing GH #95 (so `MasterDriver` inherits the safety layers rather than reimplementing
them), and make the **WaterBalanceModel** explicit so the ET/VWC switch becomes polymorphic
dispatch over a shared `Deficit` output instead of an `if self._vwc_sensor:` fork with a
duplicated ET formula. All five objects now exist as self-contained modules; the remaining phase
is wiring the existing call sites onto them.

### Where model and code still disagree

Stated plainly, so the divergence is a decision rather than a surprise:

| Divergence | Status |
|---|---|
| ~~`Actuator` family in code vs `Driver` family in this document~~ | ✅ **Resolved 2026-08-09.** `actuator.py` → `driver.py`, `Actuator`/`ZoneActuator`/`MasterActuator` → `Driver`/`ZoneDriver`/`MasterDriver`. Free to do: no production module and no test referenced the scaffold, so the rename touched nothing that runs |
| `Zone` and `Environment` written but nothing imports them | Deliberate. Phase 1 is the class, phase 2 is the wiring; conflating them is how a refactor becomes unreviewable |
| Seasonal Kc curve lives in `sensor.compute_kc`, not on `Zone` | Deliberate: the plant-family table has one home, and copying it onto the Zone would create the duplicate source of truth anomaly E1 is about. `Zone.effective_kc` owns only the part that is genuinely the zone's — its exposure |
| `D_max` per-zone in the model, seeded from the site in code (`self._d_max = dryness_sensor._d_max`) | Decided per-zone; the field already exists on the zone, so the work is to expose it in config and stop seeding it. Deriving it properly needs a wilting point the model does not have — see the caveat in the RFC |
| VWC mode overwrites the zone deficit unconditionally after irrigation | A defect, tracked separately. It is the same shape wiring a model into an anemic Zone would reproduce: two writers on one truth |
