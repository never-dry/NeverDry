# Design — Domain Model Anomalies (code-verified companion)

**Status:** Draft
**Date:** 2026-07-24
**Related:** [Domain Object Model](design_domain_object_model.md) (the target model this audits against), [Water-Balance Reference Model](design_water_balance_reference_model.md), GH #74, GH #95

## Purpose

This is the **audit companion** to the [Domain Object Model](design_domain_object_model.md).
That document defines the *target* objects — System, Zone, Scheduler, Driver / ZoneDriver /
MasterDriver, DeliveryResult — and where each responsibility belongs. This document is the
other half of the loop: a code-verified register of the **anomalies** where the current
implementation diverges from that model.

The two documents are meant to be used together:

- The **Domain Object Model** is the *yardstick* — it says who should own what.
- This **Anomalies register** is the *measurement* — it says where the code violates that
  ownership, with file:line evidence, and points each fix back at the responsible object.

Every anomaly below names the domain object that *should* own the misplaced behaviour, so the
correction is not "refactor for cleanliness" but "move responsibility X back to object Y as
the model already prescribes."

Verified against the code at `custom_components/never_dry/` on 2026-07-24. All line numbers
refer to that snapshot.

## Health map (as-is vs. target model)

| Target object | Current carrier | Health | Root anomaly |
|---|---|---|---|
| **System** (feeds, not state) | `ETSensor`, `DrynessIndexSensor` | ⚠️ | Still holds a `_deficit` accumulator + duplicated FAO-56 recurrence (§E1) |
| **Zone** (owns deficit, `settle(DeliveryResult)`) | `IrrigationZoneSensor` | 🔴 | Anemic: its accounting behaviour lives in the controller (§A1) |
| **Scheduler** | implicit in `IrrigationController` | 🟡 | Out of scope here (deliberately deferred, GH #74) |
| **Driver** `<<abstract>>` | — | 🔴 | Never declared; no `ABC`/`Protocol` anywhere (§C1) |
| **ZoneDriver** (uniform seam, `deliver(liters)`) | `ValveOperator` | 🟠 | Not uniform: `volume_preset` bypasses it (§A2); mode = string dispatch (§D1) |
| **MasterDriver** | — | ❌ | Not implemented (tracked in the Domain Object Model, out of scope here) |
| **DeliveryResult** | bare `float` | 🟠 | No quality qualifier; deficit-crediting formula duplicated ×4 (§A1) |

The reference-quality pair in the codebase is `ValveFsm` (pure, event-driven, emits `FsmAction`
*data*) + `ValveOperator` (host that executes them). It is exactly the decoupling the target
model prescribes for `Driver`. **It is the template for fixing everything below.**

---

## A. Decoupling anomalies

### A1 — 🔴 Zone accounting lives in the Controller (anemic domain model)
**Target owner:** `Zone` (`accumulate`, `water_demand()`, `settle(DeliveryResult)`).
**As-is:** `IrrigationController` reads *and writes* 13 private attributes of the zone plus
`zs._dryness`:

```
zone._zone_deficit  _efficiency  _area  _flow_rate  _deficit_at_irrigation_start
_last_irrigated  _last_irrigation_source  _last_session_duration_s
_last_volume_delivered  _session_water_delivered  _total_water_delivered
_yearly_water_delivered  _threshold
```

The deficit-crediting formula `max(0, deficit_at_start − delivered·efficiency/area)` is
**duplicated in 4 sites**: `_settle_irrigated_zones` (controller.py:694),
`_update_deficit_realtime` (:1144), `_finalize_manual_session` (:1412), and
`reset_deficit(delivered_liters=…)` (sensor.py:1227).

**Gap vs. model:** the model says the Zone owns its deficit and settles it from a
`DeliveryResult`. Today the Zone is a data-bag and the controller is its accounting engine.
**Direction:** move behaviour into the Zone — `zone.begin_cycle()`, `zone.credit_delivery(result)`,
`zone.settle(result)` — collapsing the 4 copies into one. Closing this also mechanically
closes C1 and half of E1.

**Why this is the keystone, not just the largest entry (2026-08-09).** The `Zone` is **designed and
not yet written**: it has a row in the five-object table, a box in the class diagram with its
attributes and methods, and an owner recorded for every behaviour listed above — but no module. A
search across every ref finds only `IrrigationZoneSensor` and eighteen `Zone*Sensor` numeric
projections of it. Now look at where the two existing scaffolds point: `driver.py` returns a
`DeliveryResult` — to *whom*? To the Zone that settles it. `water_balance_model.py` produces a
`Deficit` — for *whom*? For the Zone that owns it. Both seams face a class that has been specified
but never typed out.

So the scaffolds are not inert because wiring them is hard; they are inert because the object they
attach to has not been written yet. That reframes A1: it is not an anomaly to clean up eventually,
it is the unwritten half of the object model, and it gates the wiring of everything else. It is also why the
duplicated crediting formula matters beyond tidiness — wire a `WaterBalanceModel` in while the
controller still writes `zone._zone_deficit` from four sites and you get **two writers on the same
truth**, with the model authoritative on paper and the controller authoritative in fact. That failure
has already happened once: it is the shape of the VWC overwrite defect (probe reading overwrites the
zone deficit after irrigation), reproduced one layer up.

### A2 — 🟠 `volume_preset` leaks the ZoneDriver abstraction
**Target owner:** `ZoneDriver` (uniform seam; `deliver(liters)` for *every* mode).
**As-is:** `estimated_flow` and `flow_meter` route through `operator.open()/close()`
(controller.py:1196/1222), but `volume_preset` bypasses the operator entirely
(controller.py:824–913) and re-implements its own precheck (:852–862), duplicating
`ValveOperator._precheck` (valve_operator.py:301). The operator is deliberately *not created*
for these zones (sensor.py:414–416).

**Gap vs. model:** the safety layers the model puts in the `Driver` base (watchdog, adaptive
timeout, latency, FSM, notifier dedup) silently do **not** apply to `volume_preset` zones.
**Direction:** make `volume_preset` a `ZoneDriver` delivery strategy behind the same seam, so
smart-valve self-close is a *strategy detail*, not a bypass of the whole abstraction.

---

## B. Injection present but unused

### B1 — 🔴 `notifier` injected, used for 1 of 12 notification kinds
**Target owner:** the notification bus (`ValveNotifier`) — single seam for user-facing conditions.
**As-is:** `IrrigationController.__init__` receives `notifier` (controller.py:68) but calls it
only for `UNREACHABLE_AT_IRRIGATION`. Three sites bypass it with raw
`hass.services.async_call("persistent_notification", "create", …)`:

| Site | Line | Template already in the notifier |
|---|---|---|
| `_on_battery_change` | controller.py:1636 | **`BATTERY_LOW`** exists (valve_notifier.py:113) |
| `_check_deficit_anomaly` | controller.py:1674 | — (mappable to a new/`WATER_ME_NOW` kind) |
| `_check_and_notify` (monitoring) | controller.py:1725 | **`WATER_ME_NOW`** exists (valve_notifier.py:125) |

Evidence of the cost: `_on_battery_change` hand-rolls a `_battery_alerted` set
(controller.py:117, 1632) to emulate the dedup `ValveNotifier.is_active()` already provides.

**Cross-check:** only **5 of 12** `NotificationKind` values are emitted in production
(`COMMAND_FAILED`, `STUCK_OPEN`, `WATCHDOG_TRIGGERED`, `ZONE_DISABLED`,
`UNREACHABLE_AT_IRRIGATION`). The other 7 — `BATTERY_LOW`, `WATER_ME_NOW`, `LEAK_DETECTED`,
`FLOW_METER_DEAD`, `IRRIGATION_INEFFECTIVE`, `MODEL_DRIFT`, `UNREACHABLE_PASSIVE` — have full
templates but **no emitter**. `BATTERY_LOW` and `WATER_ME_NOW` are dead *precisely because*
their natural emitters use the raw path.

**Direction:** route the 3 raw sites through the injected notifier; drop the ad-hoc dedup set.
Isolated, low-risk, unlocks already-written templates — the recommended first fix.

---

## C. Undeclared interfaces

### C1 — 🟠 No `Protocol`/`ABC` in the whole package
**Target owner:** `Driver <<abstract>>` in the model; and the implicit contracts Zone/System.
**As-is:** zero abstract types. The controller's key collaborators are untyped:
`dryness_sensor` (untyped), `zone_sensors: list` (untyped), `zone` parameters as bare names.
Two large interfaces exist only implicitly:

- **`ZoneProtocol`** (~25 members: `zone_name, valve, volume_liters, duration_s,
  delivery_mode, delivery_timeout, flow_meter_sensor, volume_entity, battery_sensor,
  irrigation_mode, irrigation_time, set_irrigating, reset_deficit, set_deficit_mm,
  notify_session_listeners, extra_state_attributes, async_write_ha_state` + the 13 private
  attributes from A1).
- **`DrynessProtocol`** (`deficit, reset, set_deficit_mm, register_zone_listener,
  async_write_ha_state`).

**Gap vs. model:** the model draws `Driver` as `<<abstract>>` and expects declared seams; the
code has none. **Direction:** declare `ZoneProtocol` / `DrynessProtocol` (and the abstract
`Driver` when MasterDriver lands). A `Protocol` **cannot include `_private` members**, so
declaring `ZoneProtocol` forces the public accessors that fix A1.

*(Note: `ValveOperator._execute_action`'s 7-way `isinstance(action, …)` dispatch
(valve_operator.py:377–390) is an undeclared "action visitor" — acceptable, it is the price of
keeping the FSM pure. Listed for completeness, not for correction.)*

---

## D. Missing polymorphism

### D1 — 🟠 Delivery mode = string dispatch
**Target owner:** `ZoneDriver` delivery strategy (`native_volume` vs `time_x_flow`).
**As-is:** `_deliver_water` (controller.py:735) branches `if mode == DELIVERY_MODE_*` over
three constants into three methods. Worse, the rate-vs-cumulative logic of the *commanded*
path is reimplemented in the *manual* path (`_external_session_monitor` →
`_monitor_via_flow_meter`, controller.py:1529).
**Direction:** a `DeliveryStrategy` (`EstimatedFlowDelivery` / `FlowMeterDelivery` /
`VolumePresetDelivery`) shared by commanded and manual paths — this is the model's `ZoneDriver`
+ `DeliveryResult` made concrete.

### D2 — 🟡 Flow-sensor type = `if is_rate` repeated ×4
**As-is:** the rate/cumulative branch appears in `_deliver_flow_meter`, `_deliver_flow_rate`,
`_monitor_via_flow_meter`, `_finalize_manual_session`.
**Direction:** a polymorphic `FlowReader` (`RateReader` / `CumulativeReader`) collapses the
four copies and the `_read_flow_meter` / `_read_volume_liters` / `_rate_to_lpm` wrappers.

---

## E. Usable inheritance / composition

### E1 — 🟠 System and Zone duplicate the FAO-56 recurrence
**Target owner:** a shared `WaterBalanceModel` / deficit account; the model already says
"deficit lives in the Zone, System holds feeds not state."
**As-is:** `DrynessIndexSensor` and `IrrigationZoneSensor` both implement the identical
recurrence and deficit API:

```python
deficit = max(0, min(deficit + et*kc*dt − rain, d_max))   # sensor.py:705/810 and 1125
# + reset(), set_deficit_mm() clamped to d_max, deficit property
```

**Gap vs. model:** the Water-Balance Reference Model is retiring `DrynessIndexSensor._deficit`
as ET state (kept only for interim VWC), so this duplication is a *known transitional* gap.
**Direction:** extract a `WaterBalanceModel` value object, held by composition (preferred) or a
mixin — consistent with house style (`_ZoneTextSensor` already bases ~13 subclasses).

**Update 2026-08-16 — half closed.** `DrynessIndexSensor` now holds a `WaterBalanceModel` and
calls `step()`; its `_deficit` is a view onto the model rather than a second store. The recurrence
it used to spell out is gone from the entity layer. The zone side is *not* closed: each zone still
integrates the broadcast rate against its own Kc in its own loop. The seam that would close it is
one model instance per zone — the design already says so ("a per-zone instance is built with that
zone's Kc") — and it is not free, because per-zone instances change what a restart has to restore.

### E2 — 🟢 Numeric zone projections without a shared base
**As-is:** `ZoneDeficitSensor`, `ZoneRainSensor`, `ZoneSessionWaterSensor`,
`ZoneYearlyWaterSensor`, `ZoneDurationSensor` inherit `SensorEntity` directly while the text
projections share `_ZoneTextSensor`. Low impact. **Direction:** an analogous
`_ZoneNumericSensor` base.

### E3 — 🟡 α is a per-model parameter presented as a global one
**Found:** 2026-08-16, promoting the object-model RFC to Accepted. Recorded here rather than in the
RFC because it is a gap between the model and the code, which is what this document is for.
**As-is:** ownership at *runtime* is now correct — α reaches `ETModel` through `build_model` and
nothing else reads it for physics. What has not moved is the *presentation*: α is still a top-level
config key rendered in the sensors form beside temperature and rain, so a site running
Penman-Monteith or a soil probe is shown an "ET sensitivity" box that does nothing for it.
**Why it matters:** the box invites tuning a number that has no effect on the model being run, and
a user who tunes it and sees nothing change learns the wrong thing about the system. It is the same
class of defect as a stale claim in a docstring — the interface asserts a relationship that is not
there.
**Direction:** the field belongs with the method it parameterises. Either it is shown only when the
simple tier is selected, or the ET parameters move behind the method choice entirely. Both are
blocked by the same property that shaped the dropdown: the form does not react to what you type, so
this is a step boundary, not a conditional field.
---

## Priority register

| # | Anomaly | Target object | Severity | First-cut effort |
|---|---|---|---|---|
| B1 | Notifier injected, used 1/12; 3 raw sites; 7 dead kinds | ValveNotifier | 🔴 | S |
| A1 | Controller writes 13 zone privates; deficit formula ×4 | Zone | 🔴 | L |
| C1 | No Protocol; zone/dryness contract implicit | Driver / Zone / System | 🟠 | M |
| A2 | `volume_preset` bypasses the operator + duplicate precheck | ZoneDriver | 🟠 | M |
| D1 | Delivery mode = if/elif; command vs manual duplicated | ZoneDriver + DeliveryResult | 🟠 | L |
| E1 | FAO-56 duplicated across System and Zone | WaterBalanceModel | 🟠 | M |
| D2 | `if is_rate` ×4 | FlowReader | 🟡 | S |
| E2 | Numeric zone projections without a base | (projection base) | 🟢 | XS |

**Common root:** A1, C1 and E1 are the same fault seen from three angles — the domain
(deficit, water, balance) has no home of its own and is smeared across the controller.
A first-class `Zone` / `WaterBalance` with a declared interface resolves all three together.
The blueprint for doing it is already in-tree: `ValveFsm` / `ValveOperator`.

## How to use this with the Domain Object Model

1. Pick an anomaly; open the **target object** in the Domain Object Model to confirm which
   responsibility it should own.
2. Verify the anomaly still holds against current code (line numbers above).
3. Correct by moving the responsibility to the target object — not by local cleanup.
4. When an anomaly is closed, update its row here and, if it changes the code mapping, the
   "Mapping to current code" table in the Domain Object Model.

The recommended sequence is **B1 → C1 → A1 → (A2, D1, E1)**: B1 is isolated and unlocks dead
infrastructure; C1 declares the seams that make A1 safe; A1 then re-homes the deficit, after
which the driver/strategy anomalies (A2, D1) and the model duplication (E1) refactor against a
stable Zone contract.
