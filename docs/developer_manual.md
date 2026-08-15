# NeverDry — Developer Manual

## Table of contents

1. [Architecture overview](#1-architecture-overview)
2. [Core formulas and their location](#2-core-formulas-and-their-location)
3. [Crop coefficient (Kc) system](#3-crop-coefficient-kc-system)
4. [Module reference](#4-module-reference)
5. [Service registration](#5-service-registration)
6. [Config flow](#6-config-flow)
7. [Testing](#7-testing)
8. [Adding a new ET tier](#8-adding-a-new-et-tier)
9. [Versioning and releases](#9-versioning-and-releases)
10. [Config entry migration](#10-config-entry-migration)
11. [Security CI](#11-security-ci)
12. [Activity log and diagnostics](#12-activity-log-and-diagnostics)

---

## 1. Architecture overview

```
custom_components/never_dry/
├── __init__.py        → Integration setup (YAML + config entry), activity log setup
├── const.py           → All constants, defaults, system types, plant families
├── sensor.py          → compute_kc(), ETSensor, DrynessIndexSensor, IrrigationZoneSensor
│                        + per-zone stat/linked sensors (deficit, rain, water totals, Kc, …)
├── controller.py      → IrrigationController (irrigation cycle, services, monitoring mode)
├── button.py          → Per-zone buttons: Irrigate, Mark irrigated, Stop, Reset valve
├── valve_operator.py  → ValveOperator: open/close with confirmation, delivery modes, watchdog
├── valve_fsm.py       → Valve finite-state machine (idle → … → maintenance)
├── valve_latency.py   → ValveLatencyTracker: adaptive timeout (rolling mean + 3σ)
├── valve_notifier.py  → User notifications on valve failures
├── unit_convert.py    → Metric/imperial conversions at the I/O boundaries
├── diagnostics.py     → HA "Download diagnostics" support
├── config_flow.py     → UI setup wizard + options flow
├── manifest.json      → Integration manifest
├── services.yaml      → HA service definitions
├── strings.json       → UI strings
├── translations/      → en.json, it.json
├── brand/             → Local brand assets (icon/logo)
└── www/               → never-dry-zone-card.js (bundled Lovelace card, auto-registered)
```

**Data flow:**

```
Temperature sensor ──→ ETSensor (ET_h)
                            │
                            ▼
Rain sensor ─────────→ DrynessIndexSensor (reference deficit, Kc=1.0)
VWC sensor (optional) ─┘    │
                             │ broadcasts (dt_h, et_h, rain) via listener pattern
                             ▼
                      IrrigationZoneSensor × N
                      Each zone tracks its own deficit:
                        D_zone += ET_h × Kc(doy, family) × Δt - rain
                             │
                             ▼
                      IrrigationController (valve open/close, services)
```

`DrynessIndexSensor` is the "reference" sensor at Kc=1.0. Each zone sensor registers as a listener and maintains its own deficit scaled by a crop coefficient Kc. The Kc varies seasonally based on the plant family assigned to the zone, with automatic hemisphere detection from `hass.config.latitude`.

For the conceptual domain model behind this layout (System / Zone / Scheduler / ZoneDriver / MasterDriver) and where upcoming features belong, see [`design_domain_object_model.md`](design_domain_object_model.md).

## 2. Core formulas and their location

All formulas live in `sensor.py`.

### 2.1 Hourly evapotranspiration (linear model)

```
ET_h = max(0, α · (T - T_base) / 24)   [mm/h]
```

| Item | Value |
|------|-------|
| **Class** | `ETSensor` |
| **Method** | `_on_temp_change()` |
| **Parameters** | `alpha` (default 0.22 mm/°C/day), `t_base` (default 9.0°C) |
| **Trigger** | `async_track_state_change_event` on temperature sensor |

### 2.2 Precipitation delta computation

```
ΔP = f(rain_sensor_type, rain_now, rain_last)
```

The raw rain sensor value is **never** subtracted directly from the deficit. Instead, a **delta** (increment since last reading) is computed to avoid double-counting.

| Rain sensor type | Delta logic |
|-----------------|-------------|
| **`event`** (default) | Value IS the delta (mm per event, e.g., tipping bucket). A new value different from the previous one is treated as a new rain event. Same value = no new rain (delta = 0). |
| **`daily_total`** | Cumulative mm since midnight. Delta = `rain_now - rain_last`. If `rain_now < rain_last` (midnight rollover), delta = `rain_now` (new accumulation from zero). |

| Item | Value |
|------|-------|
| **Class** | `DrynessIndexSensor` |
| **Method** | `_compute_rain_delta()` |
| **State** | `_last_rain` (float, tracks previous reading) |
| **Config** | `rain_sensor_type` (default: `"event"`) |

**Why this matters**: Without delta computation, a cumulative rain sensor reporting "5.0 mm today" would subtract 5.0 mm on every temperature change event — draining the deficit to zero in minutes. With delta logic, only the actual new rain since the last reading is subtracted.

### 2.3 Reference deficit accumulation (ET model, Kc=1.0)

```
D_ref(t) = clamp( D_ref(t-1) + ET_h · Δt - ΔP,  0,  D_max )
```

| Item | Value |
|------|-------|
| **Class** | `DrynessIndexSensor` |
| **Method** | `_on_sensor_change()` (inline) / `_update_from_model()` (standalone) |
| **Integration** | Forward Euler, variable Δt (event-driven) |
| **Parameters** | `alpha`, `t_base`, `d_max` (default 100.0 mm) |
| **Rain** | Uses `ΔP` from `_compute_rain_delta()`, not raw sensor value |

### 2.4 Per-zone deficit accumulation (with Kc)

```
D_zone(t) = clamp( D_zone(t-1) + ET_h · Kc_eff(doy, family, exposure) · Δt - ΔP,  0,  D_max )
```

| Item | Value |
|------|-------|
| **Class** | `IrrigationZoneSensor` |
| **Method** | `_on_et_update()` |
| **Kc source** | `compute_kc()` module-level function |
| **Parameters** | `plant_family`, `kc` (manual override), `exposure` / `microclimate_factor`, `hass.config.latitude` |
| **Rain** | Receives `ΔP` (rain delta) from `DrynessIndexSensor` broadcast |

Each zone accumulates independently. Rain delta reduces all zone deficits equally. Only the irrigated zone's deficit resets after irrigation.

### 2.5 Crop coefficient computation

```
Kc = compute_kc(day_of_year, plant_family, manual_kc, latitude, microclimate_factor)
   = base(day_of_year, plant_family, manual_kc) · microclimate_factor
```

| Item | Value |
|------|-------|
| **Function** | `compute_kc()` (module-level in `sensor.py`) |
| **Base priority** | `manual_kc > plant_family seasonal profile > DEFAULT_KC (1.0)` |
| **Interpolation** | Linear between 4 seasonal anchors (days 15, 105, 196, 288), in `_seasonal_kc()` |
| **Hemisphere** | Southern (latitude < 0): day shifted by 182 days |
| **Plant families** | Defined in `const.py` `PLANT_FAMILIES` dict (10 families) |

#### Site exposure (microclimate factor, k_mc)

Landscape coefficient method (Costello, Matheny & Clark 2000): `K_L = k_s · k_d · k_mc`.
The plant family supplies `k_s`; the zone's site exposure supplies `k_mc`.

| Item | Value |
|------|-------|
| **Function** | `resolve_microclimate_factor(exposure, custom_factor)` (pure, in `sensor.py`) |
| **Config keys** | `exposure` (preset key), `microclimate_factor` (number, `exposure == "custom"` only) |
| **Presets** | `const.py` `EXPOSURES` dict — 0.60 (deep shade) … 1.20 (reflected heat), plus `custom` |
| **Applied to** | The base Kc **including** a manual override — exposure describes the site, not the planting |
| **Bounds** | `[MICROCLIMATE_FACTOR_MIN, MICROCLIMATE_FACTOR_MAX]` = `[0.1, 1.5]`, clamped |
| **Fallback** | Unset / unknown / non-numeric → `DEFAULT_MICROCLIMATE_FACTOR` (1.0), never 0 |
| **Resolved** | Once in `IrrigationZoneSensor.__init__` (config is static per reload) |

The floor above zero is load-bearing: at `k_mc = 0` the deficit never accrues, so
reactive irrigation, the monitoring notification and `_check_deficit_anomaly()`
would all go quiet with nothing in the UI to explain it. The config flow rejects
`custom` without a factor (`microclimate_factor_required`) for the same reason —
silently resolving to 1.0 would look like the exposure had never been set.

### 2.6 Deficit from VWC (direct measurement)

```
D = max(0, (FC - VWC) · root_depth · 1000)   [mm]
```

| Item | Value |
|------|-------|
| **Class** | `DrynessIndexSensor` |
| **Method** | `_update_from_vwc()` |
| **Zone behavior** | In VWC mode, zones compute `D_zone = D_ref × Kc` |

### 2.7 Irrigation volume per zone

```
V = D_zone · A / η   [L]
```

| Item | Value |
|------|-------|
| **Class** | `IrrigationZoneSensor` |
| **Property** | `volume_liters` |
| **Uses** | `_zone_deficit` (per-zone, not shared) |

### 2.8 Expected irrigation duration per zone

```
t = V / Q · 60   [s]
```

| Item | Value |
|------|-------|
| **Class** | `IrrigationZoneSensor` |
| **Property** | `duration_s` |
| **Parameters** | `flow_rate_lpm` (guard flow), live flow-meter rate |

`Q` is resolved through a source chain, so the estimate is meaningful for
every delivery mode:

1. **Live flow-meter rate** — `flow_meter` mode with a rate sensor
   (L/min, L/h, m³/h, gal/min, gal/h via `flow_utils.read_flow_rate_lpm`)
   reading > 0. Since `volume_liters` shrinks in real time during a
   session, the value reads as the estimated *remaining* time while
   irrigating.
2. **Configured guard flow rate** (`flow_rate_lpm`) — `flow_meter` at
   rest or with a cumulative-volume meter, `volume_preset`, and
   `estimated_flow`.
3. **0** — no source available; there is nothing to derive a bound from,
   so the configured `delivery_timeout` is all that guards the valve
   (a once-per-zone warning is logged).

The related safety timeout is `delivery_timeout = min(configured ceiling,
DELIVERY_DURATION_MARGIN × guard-flow duration)`. Two different questions
used to share this number — *how long should the job take* is a prediction
about the work, *how long before something is wrong* is a bound on failure
— and combining them with `max()` made the configured value a floor: a
zone with five minutes of work was guarded with the one-hour default, and
a meter that stopped counting kept the valve open for the whole hour
(GH #173). The configured value is now a cap, which the user can tighten
but never loosen; when it bites, the zone logs one warning instead of
quietly stopping short.

Two derived layers sit above it, each `SAFETY_LAYER_SPREAD` (×1.25) above
the previous one and each capped by the same configured value:
`watchdog_timeout` (`ValveOperator._watchdog`) and `hw_max_duration_s`
(the on-device timer). The spacing is what lets a layer catch the failure
of the one below it rather than race it. Without a guard flow there is no
room under the cap and all three collapse onto the configured value.

All three deliberately use the guard-flow estimate only — never the live
rate — so a momentary high meter reading cannot tighten the watchdog. The
operator receives the value as a callable re-evaluated at every valve open
(watchdog sleep and hardware max-duration write), not as a setup-time
snapshot.

### 2.9 Resolution orders

**Efficiency**: `explicit value > system_type default > global default (0.85)`

**Kc**: `manual kc > plant_family seasonal Kc(doy) > DEFAULT_KC (1.0)`

## 3. Crop coefficient (Kc) system

### Plant families (defined in `const.py`)

| Family key | Label | Kc winter | Kc spring | Kc summer | Kc autumn |
|-----------|-------|-----------|-----------|-----------|-----------|
| `lawn` | Lawn / Turf grass | 0.45 | 0.85 | 1.00 | 0.70 |
| `vegetables` | Vegetables (seasonal) | 0.30 | 0.70 | 1.10 | 0.50 |
| `fruit_trees` | Fruit trees (deciduous) | 0.35 | 0.70 | 0.95 | 0.55 |
| `ornamental_shrubs` | Ornamental shrubs | 0.40 | 0.65 | 0.80 | 0.55 |
| `herbs` | Herbs (Mediterranean) | 0.30 | 0.55 | 0.70 | 0.40 |
| `citrus` | Citrus / Evergreen fruit | 0.60 | 0.65 | 0.70 | 0.65 |
| `roses` | Roses | 0.35 | 0.75 | 0.95 | 0.55 |
| `succulents` | Succulents / Cacti | 0.15 | 0.25 | 0.35 | 0.20 |
| `native_ground_cover` | Native ground cover | 0.25 | 0.45 | 0.55 | 0.35 |
| `mixed_garden` | Mixed garden (default) | 0.40 | 0.70 | 0.90 | 0.55 |

Seasonal anchors (northern hemisphere): day 15 (mid-Jan), 105 (mid-Apr), 196 (mid-Jul), 288 (mid-Oct).

### Listener pattern

`DrynessIndexSensor` maintains a `_zone_listeners` list. Each `IrrigationZoneSensor` registers via `register_zone_listener()` at construction. When the base sensor updates, it broadcasts `(dt_h, et_h, rain)` to all listeners.

### Per-zone reset logic

- `irrigate_zone`: resets only the irrigated zone's deficit
- `irrigate_all`: resets all zone deficits + reference deficit
- `reset` service: resets everything

## 4. Module reference

### const.py

All configuration keys (`CONF_*`), service names (`SERVICE_*`), system types, plant families, anchor days, and default values. Single source of truth for magic strings.

### sensor.py

| Element | Type | Purpose |
|---------|------|---------|
| `compute_kc()` | Function | Pure function: effective Kc from day, family, override, latitude, microclimate factor |
| `resolve_microclimate_factor()` | Function | Pure function: site-exposure preset (or custom value) → k_mc |
| `ETSensor` | Class (1 instance) | Instantaneous ET rate [mm/h] |
| `DrynessIndexSensor` | Class (1 instance) | Reference deficit [mm] at Kc=1.0, RestoreEntity |
| `IrrigationZoneSensor` | Class (N instances) | Per-zone deficit, volume [L], duration [s], RestoreEntity |

### controller.py

`IrrigationController` holds references to the `DrynessIndexSensor` and all `IrrigationZoneSensor` instances.

**Key behaviors:**
- Sequential valve control with configurable inter-zone delay (default 30s)
- Per-zone deficit reset after irrigation (not global)
- Stop-check every 1 second during irrigation
- Monitoring mode: 6-hour periodic check with per-zone deficit thresholds
- Error safety: all valves closed on any exception

#### Irrigation triggers and the External Session Monitor

`IrrigationZoneSensor.is_irrigating`, `_last_irrigated`, `_last_volume_delivered`, and `_zone_deficit` can be mutated through **four** entry points. Three of them share the commanded path (`_irrigate_zones` → `_deliver_water`); the fourth (manual valve open) goes through a dedicated reactive monitor.

| # | Trigger | Entry point | Source string | `is_irrigating` toggled | Flow meter integrated |
|---|---|---|---|---|---|
| 1 | External switch open (physical button on the valve, ZHA, HA switch) | `_on_valve_state_change` (callback on `switch` state changes) + `_external_session_monitor` (asyncio task) | `"manual"` | yes (on open / on close) | yes (cumulative or rate) |
| 2 | "Irrigate" button / `irrigate_zone` service / `irrigate_all` service | `_handle_irrigate_zone` / `_handle_irrigate_all` → `_irrigate_zones` → `_deliver_water` | `"button"` | yes (inside `_deliver_*` modes) | yes (in `flow_meter` and `flow_rate` modes) |
| 3 | Scheduler (Mode A reactive, Mode B scheduled) | `_make_reactive_handler` / `_make_scheduled_handler` → `_irrigate_zones` → `_deliver_water` | `"reactive"` / `"scheduled"` | yes | yes |
| 4 | `mark_irrigated` service / "Mark irrigated" button | `_handle_mark_irrigated` → `reset_deficit("mark_irrigated")` | `"mark_irrigated"` | **no** (no physical irrigation through the tracked valve) | no |

The source string column applies to both the zone's `last_irrigation_source` attribute and the `source` field of the `never_dry_irrigation_complete` HA event — they are kept in sync so an automation can filter on either. Trigger 4 sets the attribute but emits no event. The legacy fallback string `"automatic"` is only used if `_irrigate_zones` is called without a preceding `_current_source` assignment (defensive default; not reachable from production paths).

#### Session accounting matrix (start × stop)

Who opened the valve and who closed it are independent axes: every combination must account the water actually delivered (duration and liters) and reduce the deficit proportionally. **`mark_irrigated` is the only path that unconditionally zeroes the deficit.**

| Start | Stop | Volume accounted | Deficit effect |
|---|---|---|---|
| NeverDry (button / service / scheduler) | NeverDry — volume target reached, estimated duration elapsed, or `delivery_timeout` | measured (flow meter) or planned volume × elapsed fraction | full delivery → 0 via `reset_deficit`; partial → `max(0, start − delivered × η / area)` in `_settle_irrigated_zones` |
| NeverDry | External close — HA switch, Zigbee app, physical button, hardware self-close | elapsed fraction, detected by the delivery loop (`_wait_with_stop_check` / `_valve_closed_externally`); the valve-state listener event is suppressed | proportional reduction (never a full reset) |
| NeverDry | `never_dry.stop` / `never_dry.stop_zone` | elapsed fraction | proportional reduction |
| External open (HA switch, Zigbee app, physical button) | User closes it (any path) | flow meter (cumulative diff or rate × duration); no meter, invalid baseline, or zero reading → estimate `flow_rate × elapsed` | proportional reduction; nothing measurable (no meter **and** no `flow_rate`) → deficit left unchanged + warning |
| External open | Auto-close monitor — volume target, estimated duration, or `delivery_timeout` | same as the row above | proportional reduction — reaches 0 only if the full target volume was actually delivered |
| `mark_irrigated` button / service | — | derived from the current deficit (`volume_liters`) | **full reset to 0 — the only unconditional zeroing path** |

**External-vs-commanded discrimination** lives in `_on_valve_state_change`. The callback fires for every state change on a tracked valve entity; the gating is:

1. If a `ValveOperator` is registered for the valve and its FSM state is **not** `IDLE`, the controller is driving the valve — return.
2. Otherwise, if there is no operator and the legacy `_running` flag is `True`, another commanded cycle is in progress — return.
3. Otherwise, the transition is external.

For an external `off → on` transition:
- Record the flow meter baseline (cumulative reading or `time.monotonic()` for rate sensors) in `_manual_valve_open`.
- Call `zone.set_irrigating(True)` and `zone.async_write_ha_state()` so UI and automations see the same "currently irrigating" attribute they would during a commanded cycle.
- Schedule the auto-close monitor task and store it in `_manual_safety_tasks`.

For an external `on → off` transition:
- Cancel the monitor task (the OFF transition is either the user closing or the monitor's own `switch.turn_off` completing).
- Call `zone.set_irrigating(False)`.
- Compute the delivered volume from the flow meter (cumulative diff or rate × duration). If there is no flow meter, no valid baseline, or the meter measured zero, estimate `flow_rate × elapsed` from the configured guard flow rate and the tracked session duration. Reduce the deficit proportionally (`delivered × η / area`). The deficit is **never fully reset** on a valve close: with no flow meter *and* no `flow_rate` it is left unchanged and a warning suggests `mark_irrigated`.
- Stamp `_last_irrigated`, `_last_volume_delivered`, credit the session/total/yearly water counters, set `_last_irrigation_source = "manual"`, and fire `never_dry_irrigation_complete` with `source: "manual"`.

**`_external_session_monitor(entity_id, zone_name)`** is the auto-close brain. Started from the open detection, it must terminate the manual session at the **minimum** of:

1. **Volume target reached.** When the zone has a `flow_meter_sensor`, the monitor polls every `FLOW_METER_POLL_INTERVAL_S` seconds. For cumulative sensors it tracks `current - initial`; for rate sensors it integrates `rate × dt` (units L/min, L/h, m³/h handled explicitly). It exits as soon as `delivered >= volume_target`.
2. **Estimated duration elapsed.** Without a flow meter but with a configured `flow_rate` (L/min), the monitor sleeps for `min(volume_liters / flow_rate × 60, delivery_timeout)`.
3. **Safety timeout.** `delivery_timeout` is always honoured as the upper bound. No measurement, no estimate, no target → fall back to a pure sleep.

After waking up, the monitor checks the switch is still `"on"` and sends `switch.turn_off`. The resulting `on → off` state change is picked up by `_on_valve_state_change`, which finalises the session. If the user closes the valve first the monitor task is cancelled and never sends the service call.

**Why two layers instead of one.** Keeping detection (`_on_valve_state_change`, a sync `@callback`) separate from the auto-close (an async task) avoids re-entrancy: the callback returns immediately so HA's event loop is not blocked, and the monitor can `await asyncio.sleep` safely. The same shape is used by `_deliver_flow_meter` / `_deliver_flow_rate` for commanded cycles.

### config_flow.py

| Class | Purpose |
|-------|---------|
| `NeverDryConfigFlow` | Multi-step setup: sensors → zone → add another → create entry |
| `NeverDryOptionsFlow` | Edit model params or add zones after setup |

## 5. Service registration

Services are registered in `IrrigationController.register_services()`.

| Service | Handler | Behavior |
|---------|---------|----------|
| `never_dry.reset` | `_handle_reset` | Resets reference + all zone deficits |
| `never_dry.irrigate_zone` | `_handle_irrigate_zone` | Single zone: open → wait → close → reset zone deficit |
| `never_dry.irrigate_all` | `_handle_irrigate_all` | All zones sequentially, then reset all deficits |
| `never_dry.stop` | `_handle_stop` | Close all valves, abort cycle (no deficit reset) |
| `never_dry.stop_zone` | `_handle_stop_zone` | Stop irrigation for a single zone and close its valve (no deficit reset) |
| `never_dry.mark_irrigated` | `_handle_mark_irrigated` | Resets deficit without opening any valve (used when the user watered with a different tool — hose, separate sprinkler, unmetered rain) |
| `never_dry.reset_valve` | `_handle_reset_valve` | Reset the valve FSM from `maintenance` back to `idle` |
| `never_dry.set_deficit` | `_handle_set_deficit` | Set the deficit of one zone (or all zones) to an arbitrary mm value |

## 6. Config flow

### Zone fields

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Zone display name |
| `valve` | No | Valve or switch entity controlling the valve (omit for monitoring mode) |
| `area_m2` | Yes | Irrigated area [m²] |
| `system_type` | Yes | Irrigation system → sets default efficiency |
| `efficiency` | No | Override efficiency [0.1–1.0] |
| `plant_family` | No | Plant family → sets seasonal Kc profile |
| `kc` | No | Override Kc [0.1–2.0] |
| `flow_rate_lpm` | For `estimated_flow` | Guard flow rate [L/min]. Required for `estimated_flow`; recommended for `flow_meter`/`volume_preset`, where it drives the expected-duration estimate and the safety-timeout scaling (deprecation warning in the zone checker; becomes required at v1.0) |
| `threshold` | No | Mode A trigger threshold [mm] (default 20) |
| `delivery_mode` | No | Delivery method: `estimated_flow` (timer), `flow_meter` (sensor-monitored), `volume_preset` (smart valve with volume dosing) |
| `flow_meter_sensor` | No | Flow measurement sensor entity (required if `delivery_mode=flow_meter`) |
| `volume_entity` | No | Number entity for volume commands (required if `delivery_mode=volume_preset`) |
| `delivery_timeout` | No | Safety timeout [s] for `flow_meter` and `volume_preset` modes (default 3600) |
| `irrigation_mode` | No | Scheduling mode: `manual`, `reactive` (threshold-based), `scheduled` (time-based) |
| `irrigation_time` | No | Daily irrigation time (HH:MM) for `scheduled` mode |

## 7. Testing

```bash
cd sw_artifacts
python3 -m pytest tests/ -v
```

| File | Coverage |
|------|----------|
| `test_et_sensor.py` | ET formula, custom params, edge cases, attributes |
| `test_never_dry_sensor.py` | Reference deficit accumulation, reset, VWC mode, invalid inputs |
| `test_volume_duration.py` | Per-zone volume/duration, zone attributes, multi-zone independence |
| `test_controller.py` | Valve control, sequential irrigation, emergency stop, monitoring, system types |
| `test_kc.py` | `compute_kc()` (anchors, interpolation, hemisphere, override, microclimate factor), `resolve_microclimate_factor()`, per-zone deficit tracking |
| `test_zone_exposure.py` | Exposure preset table, `custom`-without-factor guard in every zone form |

Async controller tests require `pytest-asyncio` (skipped if not installed).

### 7.1 E2E smoke tests (pre-release, against live HA)

One-shot integration tests that hit a real Home Assistant instance via the REST API.
Run them manually before cutting a release — they are **not** executed in CI.

**Setup:**

```bash
cd sw_artifacts
cp tests/e2e/.env.example tests/e2e/.env
# edit tests/e2e/.env with your values
```

`.env` fields:

| Variable | Example | Description |
|----------|---------|-------------|
| `HA_URL` | `http://homeassistant.local:8123` | Base URL of your HA instance |
| `HA_TOKEN` | `eyJ0eXAi...` | Long-lived access token (HA → Profile → Security) |
| `ZONE_NAME` | `Giardino_Ortensia` | Exact zone name as configured in NeverDry |

The `.env` file is git-ignored and never committed.

**Run:**

```bash
# All tests except valve control (safe to run anytime)
python tests/e2e/smoke.py --no-valves

# Full suite including irrigation cycle (opens real valves for ~3s)
python tests/e2e/smoke.py
```

**Tests included:**

| Test | Valve HW | What it checks |
|------|----------|----------------|
| `ha_reachable` | — | HA REST API responds |
| `integration_loaded` | — | Config entry present and state=loaded |
| `entities_present` | — | neverdry entities exist in state machine |
| `et_sensor_valid` | — | ET sensor has a numeric, non-negative value |
| `zone_entities_present` | — | Entities for the configured zone exist |
| `irrigate_zone_and_stop` | yes | `irrigate_zone` changes sensor; `stop` completes |
| `reset_deficit` | — | `reset` service completes without error |
| `config_reload` | — | Config entry reloads and comes back loaded |

### 7.2 Irrigation end-trigger coverage matrix

An irrigation session can be terminated by **eight distinct triggers**. Every
pair of triggers is a potential race (one fires while the other is armed), and
each race must leave the valve closed and the deficit settled exactly once.
This matrix maps each pair to the test(s) that cover it — empty cells are
known coverage gaps. It is the reference for the valve driver abstraction
(AI-123), where the safety layers change from *command-close* to
*verify-close* and every one of these races must be revisited per driver.

**Triggers:**

| Code | Trigger | Where it fires |
|------|---------|----------------|
| `TARGET` | Volume target reached (measured, integrated, or device-native) | `_deliver_flow_meter` / `_deliver_flow_rate` loops; smart-valve self-close (`volume_preset`) |
| `EST` | Estimated duration elapsed (guard-flow timer) | `_deliver_estimated_flow`; manual-open session monitor |
| `TIMEOUT` | `delivery_timeout` safety limit | all delivery loops; manual safety-close |
| `STOP` | Global emergency stop (`never_dry.stop`) | `_should_abort` in every loop |
| `STOPZ` | Per-zone stop (`never_dry.stop_zone`) | `_stop_zone` flag in every loop |
| `EXT` | External close: hardware auto-close, user manual off, on-device fail-safe / max-duration timer | `_valve_closed_externally`; valve state listener |
| `WDOG` | ValveOperator software watchdog (`max_open_duration_s`) | `ValveOperator._watchdog` force `turn_off` |
| `UNLOAD` | Config entry unload / HA restart (`controller.async_stop`) | task cancellation + settle in `finally` |

**Matrix** — diagonal = trigger alone; cell (row, col) = the two triggers
interacting (symmetric: lower triangle mirrors the upper). Test names without
prefix live in `tests/test_delivery_modes.py`; `[C]` = `test_controller.py`,
`[CO]` = `test_controller_with_operator.py`, `[H]` = `test_controller_handlers.py`,
`[V]` = `test_valve_operator.py`, `[M]` = `test_end_trigger_matrix.py`
(race-matrix tests: each asserts the deficit decrement, the last-session
volume/running-time coherence, and single-settle).

| | TARGET | EST | TIMEOUT | STOP | STOPZ | EXT | WDOG | UNLOAD |
|---|---|---|---|---|---|---|---|---|
| **TARGET** | `test_closes_at_target_volume` · `test_measured_flow_wins_over_estimate` · `test_meter_reset_adjusts_baseline` · `test_sends_volume_to_number_entity` | n/a¹ | `test_flow_meter_timeout_zero_flow_credits_estimate` · `test_flow_rate_timeout_zero_flow_credits_estimate` · `test_timeout_with_dead_flow_meter_settles_deficit` · `test_timeout_forces_close` | `test_stop_during_flow_meter` · `test_stop_during_flow_rate` · `test_stop_during_preset` · `[CO] test_volume_preset_stop_during_run` · `[CO] test_irrigate_zones_clears_snapshot_on_abort` | `test_stop_zone_ends_flow_meter` | `test_external_close_ends_flow_rate` | `[V] test_watchdog_cancelled_on_normal_close` | `[M] test_unload_mid_flow_meter_settles_measured_partial` |
| **EST** | | `test_opens_waits_closes` · `test_dispatches_estimated_flow` · `[C] test_estimated_duration_used_when_no_flow_meter` | covered implicitly² | `[C] test_stop_interrupts_cycle` · `[C] test_no_event_on_stop` | `[M] test_stop_zone_mid_estimated_flow_credits_elapsed_fraction` | `[C] test_manual_close_cancels_monitor_task` · `[C] test_manual_close_records_session_duration` | `[M] test_watchdog_forced_close_mid_estimated_flow_credits_elapsed_fraction`⁵ | `[M] test_unload_mid_estimated_flow_credits_elapsed_fraction` |
| **TIMEOUT** | | | `[C] test_no_target_falls_back_to_timeout` · `test_zero_flow_without_flow_rate_cannot_estimate` | `[M] test_stop_just_before_timeout_settles_once` | `[M] test_stop_zone_just_before_timeout_settles_once` | `[M] test_external_close_before_timeout_settles_estimate` | `[M] test_watchdog_close_races_delivery_timeout_single_settle`⁵ | `[M] test_unload_races_delivery_timeout_single_settle` |
| **STOP** | | | | `[C] test_stop_closes_all_valves` · `[CO] test_emergency_stop_closes_in_parallel` · `[C] test_stop_sets_running_false` · `[C] test_stop_is_never_throttled` | `[M] test_global_stop_and_stop_zone_together_settle_once` | `[M] test_global_stop_with_valve_already_closed_externally` | `[M] test_global_stop_races_watchdog_force_close`⁵ | `[M] test_global_stop_then_unload_settles_once` |
| **STOPZ** | | | | | `[H] TestHandleStopZone::test_closes_valve_and_clears_state` | `[M] test_stop_zone_with_valve_already_closed_externally` | `[M] test_stop_zone_races_watchdog_force_close`⁵ | `[M] test_stop_zone_then_unload_settles_once` |
| **EXT** | | | | | | `[C] test_manual_close_no_meter_never_full_resets` · `[C] test_manual_close_no_meter_credits_flow_rate_estimate` · `[C] test_flow_meter_compensates_deficit` · `[C] test_manual_close_fires_event` | `[M] test_external_close_then_late_watchdog_off_no_double_settle`⁵ | `[M] test_external_close_then_unload_no_double_settle` |
| **WDOG** | | | | | | | `[V] test_watchdog_fires_and_calls_turn_off` · `[V] test_watchdog_fires_critical_notification` · `[V] test_watchdog_not_started_when_valve_not_open` | `[V] test_watchdog_cancelled_on_unload` |
| **UNLOAD** | | | | | | | | `[V] test_async_unload_releases_subscriptions` · `[M] test_unload_mid_flow_meter_settles_measured_partial` (controller side) |

Notes:

1. `TARGET` and `EST` are never armed together for the same zone: a delivery
   mode either aims at a volume target or sleeps for the estimated duration.
2. `EST × TIMEOUT`: the manual-session monitor sleeps for
   `min(estimated duration, delivery_timeout)`; the estimated-duration test
   exercises the `min()` but no test forces the timeout side to win.
3. `TIMEOUT × WDOG`: both derive from `delivery_timeout`; the `[M]` test
   verifies that firing both in the same window is harmless (idempotent
   close, single settle).
4. `controller.async_stop()` mid-delivery (entry reload / HA restart) is
   covered by the `[M] *_unload_*` tests; the operator-side unload by `[V]`.
5. In `[M]` tests the watchdog side is *simulated*: the forced
   `switch.turn_off` is represented by the valve state flipping to `off`
   mid-loop, which is exactly how the controller perceives it. The
   watchdog's own firing logic is covered in `test_valve_operator.py`. The
   production seam is exercised end to end by
   `[CO] test_watchdog_real_operator_mid_estimated_flow_settles_once`: a
   real `ValveOperator` wired into `_irrigate_zones`, its watchdog task
   actually firing and the state echo feeding both the FSM and the
   controller listener. With the driver abstraction this test becomes the
   per-driver regression harness for the WDOG cells.

All previously-empty cells are now covered by `test_end_trigger_matrix.py`:
every `[M]` test asserts the same contract — deficit decremented by exactly
the credited volume, last-session history (volume + running time) internally
coherent, and settle executed exactly once even when two triggers fire in the
same window. The hardware max-duration *arming* is covered separately by the
`test_hw_max_duration_*` family in `test_valve_operator.py` (write-on-open,
idempotency, MQTT fallback); the resulting close surfaces as `EXT`.

**Constants used by the `[M]` race-matrix tests:**

| Constant | Value | Defined in | Description |
|----------|-------|-----------|-------------|
| `FLOW_METER_POLL_INTERVAL_S` | 2 s | `const.py` (product) | Poll cadence of the metered delivery loops; every race lands on a multiple of this tick |
| `DEFAULT_DELIVERY_TIMEOUT_S` | 3600 s | `const.py` (product) | Production safety-timeout floor; the tests override it per zone to keep loops short |
| `TIMEOUT_S` | 4 s (= 2 polls) | `test_end_trigger_matrix.py` | Per-zone `delivery_timeout` used in the tests: two poll windows, so a race can land before, inside, or at expiry |
| `AREA` | 20 m² | `test_end_trigger_matrix.py` | Zone area; with `EFF` it converts credited liters into the expected deficit decrement (`mm = L × EFF / AREA`) |
| `EFF` | 0.90 | `test_end_trigger_matrix.py` | Zone distribution efficiency |
| `FLOW` | 8 L/min | `test_end_trigger_matrix.py` | Guard flow rate: drives the estimated duration, the guard-flow fallback credit (`L = FLOW × elapsed / 60`) and the timeout scaling |
| zone deficits | 0.02–0.05 mm | per test | Deliberately tiny so the guard-scaled `delivery_timeout` stays at the configured 4 s floor (loops end in seconds, compatible with the guard-duration timeout scaling) |
| `VALVE`, `METER` | entity ids | `test_end_trigger_matrix.py` | Scripted through the `_Env` helper: mutable valve state (`on`/`off`) simulates external/watchdog closes; `meter_step` makes the meter progress (measured credit) or stay frozen (guard-flow fallback credit) |

## 8. Adding a new ET tier

To add a new ET calculation method (e.g., Hargreaves-Samani):

1. Add new config keys in `const.py` (e.g., `CONF_T_MAX_SENSOR`, `CONF_T_MIN_SENSOR`)
2. Add a new method in `DrynessIndexSensor` (e.g., `_update_from_hargreaves()`)
3. Add selection logic in `_on_sensor_change()` to choose the appropriate method
4. The broadcast to zone listeners remains the same — zones only need `(dt_h, et_h, rain)`
5. Update `config_flow.py` to expose the new sensor fields
6. Update `strings.json` and `translations/en.json` with UI labels
7. Add tests in `test_never_dry_sensor.py`

## 9. Versioning and releases

### Branching model

Two long-lived branches (adopted 2026-07-16):

| Branch | Role | Rules |
|--------|------|-------|
| `main` | **Production.** What HACS users install: every release tag (`vX.Y.Z`) is cut from here. | Protected. No direct pushes; changes land only via reviewed PRs from `develop` (or hotfix branches). Must always be releasable. |
| `develop` | **Integration & testing.** Where feature/fix branches merge first and where field testing on the test HA instance happens. | Feature branches (`feature/*`, `fix/*`, `docs/*`) branch off `develop` and merge back into it. Deploy to the test HA instance from here. |

Flow:

```
feature/x ──┐
fix/y ──────┼──► develop ──(field-verified, PR)──► main ──(tag vX.Y.Z)──► HACS release
docs/z ─────┘
```

- **Merge into `develop`** as soon as a branch is green (CI + local suite); `develop` is allowed to hold work that is not yet field-verified.
- **PR `develop` → `main`** only when the accumulated changes have been verified on the test installation. Releases stay event-driven (milestones, HACS deadlines) or monthly — never one release per bugfix.
- **Hotfixes**: branch from `main`, fix, PR back to `main`, then merge `main` back into `develop` to keep the branches converged.
- **Testers without a dev setup** cannot point HACS at a branch: HACS installs GitHub *releases* from `main` only. To give testers early builds, publish a **pre-release** (e.g. `v0.11.0-beta.1`) — users who enable beta versions in HACS ("Redownload" → show beta) receive it; everyone else stays on the latest stable. Manual installation (copying `custom_components/never_dry/` from a branch checkout) remains possible for developers.

### Version scheme

NeverDry follows **semantic versioning** (SemVer): `MAJOR.MINOR.PATCH`.

| Bump | When |
|------|------|
| **PATCH** (0.1.0 → 0.1.1) | Bug fixes, documentation updates |
| **MINOR** (0.1.0 → 0.2.0) | New features, new config keys, new sensor attributes |
| **MAJOR** (0.x → 1.0.0) | Breaking changes (removed config keys, changed behavior) |

### Single source of truth

The version lives in **one place**: `manifest.json` → `"version"`.

### Release workflow

Releases are automated via GitHub Actions (`.github/workflows/release.yml`):

1. **Bump the version** using the provided script:
   ```bash
   ./scripts/bump_version.sh 0.2.0
   ```
   This:
   - Validates semver format
   - Checks the working tree is clean
   - Updates `manifest.json`
   - Creates a commit (`release: bump version to 0.2.0`)
   - Creates an annotated git tag `v0.2.0`

2. **Push to trigger the release**:
   ```bash
   git push origin main && git push origin v0.2.0
   ```

3. **GitHub Actions automatically**:
   - Runs the full test suite
   - Verifies `manifest.json` version matches the tag
   - Packages `custom_components/never_dry/` into `never_dry.zip`
   - Creates a GitHub Release with auto-generated release notes

4. **HACS** detects the new release and notifies users of the available update.

### Pre-release checklist

- [ ] All unit tests pass (`python3 -m pytest tests/ -v`)
- [ ] E2E smoke tests pass (`python tests/e2e/smoke.py --no-valves`, then full run if valves available)
- [ ] No uncommitted changes
- [ ] `HACS` validation passes locally or in CI
- [ ] Changelog / release notes drafted (GitHub auto-generates from PR titles)

## 10. Config entry migration

### Overview

Home Assistant calls `async_migrate_entry()` (in `__init__.py`) automatically when a config entry's stored version is **lower** than `ConfigFlow.VERSION`. This allows safe schema upgrades without requiring users to remove and re-add the integration.

### How it works

1. `CONFIG_VERSION` in `const.py` is the **single source of truth** for the config schema version
2. `NeverDryConfigFlow.VERSION` references `CONFIG_VERSION`
3. When HA loads an entry with `entry.version < CONFIG_VERSION`, it calls `async_migrate_entry()`

### Adding a migration

When you change the config entry schema (add, rename, or remove keys):

1. **Increment `CONFIG_VERSION`** in `const.py`:
   ```python
   CONFIG_VERSION = 2  # was 1
   ```

2. **Add a migration block** in `async_migrate_entry()` (`__init__.py`):
   ```python
   if entry.version == 1:
       new_data = {**entry.data}
       # Example: add a new key with a default value
       new_data.setdefault("new_key", "default_value")
       # Example: rename a key
       # new_data["new_name"] = new_data.pop("old_name", default)
       hass.config_entries.async_update_entry(
           entry, data=new_data, version=2
       )
   ```

3. **Chain migrations** for users who skip versions:
   ```python
   if entry.version == 1:
       # migrate 1 → 2
       ...
   if entry.version == 2:
       # migrate 2 → 3
       ...
   ```
   Each block advances the version by one, so a user on v1 upgrading to v3 runs both migrations sequentially.

4. **Add tests** for each migration path.

### Important notes

- Migrations must be **idempotent** — running the same migration twice must not corrupt data
- Always provide **sensible defaults** for new keys so existing installations don't break
- Never remove data that might be needed by a rollback — instead, deprecate and ignore
- Log the migration at `_LOGGER.info` level for user visibility

## 11. Security CI

The integration is protected by a three-layer security pipeline (`.github/workflows/security.yml`) that runs on every push and PR to `main`.

### Layer 1: Bandit Static Analysis

[Bandit](https://bandit.readthedocs.io/) is a Python static analysis tool that finds common security issues:
- Hardcoded passwords and secrets
- Use of dangerous functions (`eval`, `exec`, `subprocess`, etc.)
- Insecure cryptographic practices
- SQL injection patterns

Bandit runs with `--severity-level medium --confidence-level medium` to filter noise. The report is uploaded as a CI artifact.

### Layer 2: Forbidden Pattern Guard

A custom shell-based check that **hard-fails** on patterns that must never appear in integration code:

| Pattern | Risk | Severity |
|---------|------|----------|
| `eval()` / `exec()` | Arbitrary code execution | **BLOCK** |
| `subprocess` / `os.system()` / `os.popen()` | Shell injection | **BLOCK** |
| `pickle` / `marshal` / `shelve` | Unsafe deserialization | **BLOCK** |
| `__import__()` | Dynamic code loading | **BLOCK** |
| `compile()` | Code compilation (review) | WARN |
| `importlib.import_module()` | Dynamic import (review) | WARN |
| `open()` | File access (review) | WARN |
| `requests` / `urllib` | SSRF risk (review) | WARN |
| `from_string` / `Environment()` | Template injection (review) | WARN |

**BLOCK** patterns fail the CI. **WARN** patterns produce annotations but don't fail.

### Layer 3: CodeQL Analysis

GitHub's [CodeQL](https://codeql.github.com/) runs semantic analysis with `security-and-quality` queries. Results appear in the repository's **Security** → **Code scanning alerts** tab.

### If a check fails

1. **Bandit finding**: Read the finding ID (e.g., `B102`), check if it's a true positive. If safe, add `# nosec B102` with a comment explaining why.
2. **Forbidden pattern**: This is almost always a true positive. Refactor to avoid the dangerous function. If absolutely necessary, discuss in the PR.
3. **CodeQL alert**: Review in the GitHub Security tab. Dismiss with a reason if it's a false positive.

### Running locally

```bash
# Bandit
pip install bandit
bandit -r custom_components/never_dry/ --severity-level medium --confidence-level medium

# Forbidden patterns (quick check)
grep -rn 'eval\|exec\|subprocess\|os\.system\|pickle\|__import__' custom_components/never_dry/ --include='*.py'
# Should return nothing
```

---

## 12. Activity log and diagnostics

### 12.1 Dedicated activity log file

When the integration loads, `async_setup_entry` in `__init__.py` attaches a
`RotatingFileHandler` to the `custom_components.never_dry` Python logger namespace.
Every `_LOGGER.*()` call in every module — `controller.py`, `sensor.py`,
`valve_operator.py`, `valve_fsm.py` — flows there automatically because all modules
use `logging.getLogger(__name__)`, which inherits from the namespace.

**File location:** `<ha_config_dir>/never_dry_activity.log`
**Rotation:** 5 MB per file, 2 backups (up to ~15 MB total)
**Level:** `DEBUG` — captures everything, including decision-point traces

The handler is torn down cleanly in `async_unload_entry` so it does not accumulate
on reload.

### 12.2 Key log markers

The following structured tokens appear in the activity log and are easy to grep for:

| Token | Level | When |
|---|---|---|
| `Scheduled check fired:` | INFO | Scheduled handler triggered by `async_track_time_change` |
| `no irrigation needed` | INFO | Threshold not met at scheduled time |
| `Scheduled irrigation triggered:` | INFO | Threshold met, cycle starting |
| `Scheduled irrigation for '…' skipped` | WARNING | Cycle already running at trigger time |
| `Reactive check:` | INFO | Reactive handler saw deficit ≥ threshold but skipped (running) |
| `Reactive irrigation triggered:` | INFO | Reactive handler launched a cycle |
| `Attempting valve open:` | INFO | `_open_valve` about to send the service call |
| `Starting irrigation:` | INFO | Cycle begun — includes `mode`, `volume`, `deficit`, `timeout` |
| `needs 0L irrigation — skipping` | INFO | Volume is 0 — includes `deficit`, `area`, `efficiency` |
| `SESSION_RESULT` | INFO | End-of-session structured line (stable format, grep-friendly) |
| `flow_meter timeout` / `flow_rate timeout` | WARNING | Delivery timed out before target volume reached |

**Useful one-liners for field diagnosis:**

```bash
# All events for today
grep "$(date +%Y-%m-%d)" /config/never_dry_activity.log

# Why did it fire (or not fire)?
grep -E "Scheduled check|triggered|skipped|no irrigation" /config/never_dry_activity.log

# All completed irrigation sessions
grep "SESSION_RESULT" /config/never_dry_activity.log

# Valve open/close events
grep -E "Attempting valve|Valve open failed|Valve close" /config/never_dry_activity.log

# Timeouts and errors
grep -E "timeout|ERROR|WARNING" /config/never_dry_activity.log
```

### 12.3 HA diagnostics download

`diagnostics.py` implements the standard HA diagnostics platform. A
**Download diagnostics** button appears automatically in the integration UI under
**Settings → Devices & Services → NeverDry → ⋮**.

The downloaded JSON bundle contains:

| Field | Content |
|---|---|
| `config_data` | Config entry data (secrets redacted) |
| `entity_states` | Snapshot of all NeverDry entity states and attributes |
| `activity_log.tail` | Last 500 lines of `never_dry_activity.log` |
| `activity_log.path` | Absolute path to the log file on the HA host |
| `activity_log.total_lines` | Total line count at download time |

This bundle is designed to be attached to a bug report or a field-test session
without exposing any credentials.

### 12.4 Enabling DEBUG in HA logger (optional)

By default, HA logs at INFO for custom integrations. The activity log file always
captures DEBUG regardless of the HA logger setting. To also see DEBUG in the main
HA log (useful during development), add to `configuration.yaml`:

```yaml
logger:
  default: warning
  logs:
    custom_components.never_dry: debug
```

Restart or reload the integration after the change.
