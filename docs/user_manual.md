# NeverDry — User Manual

## Table of contents

1. [Introduction](#1-introduction)
2. [How it works](#2-how-it-works)
3. [Requirements](#3-requirements)
4. [Installation](#4-installation)
5. [Configuration (UI setup wizard)](#5-configuration-ui-setup-wizard)
6. [Understanding the sensors](#6-understanding-the-sensors)
7. [Irrigation logic — how it all works](#7-irrigation-logic--how-it-all-works)
8. [Setting up automations](#8-setting-up-automations)
9. [Monitoring mode (no valves)](#9-monitoring-mode-no-valves)
10. [Editing settings after setup](#10-editing-settings-after-setup)
11. [Updating the integration](#11-updating-the-integration)
12. [Calibration guide](#12-calibration-guide)
13. [Dashboard examples](#13-dashboard-examples)
14. [Troubleshooting](#14-troubleshooting)
15. [FAQ](#15-faq)

---

## 1. Introduction

NeverDry is a Home Assistant custom integration that tells you **when** and **how long** to irrigate your garden. Instead of fixed timers, it tracks a real-time soil water deficit — the amount of water your soil has lost through evaporation since the last rain or irrigation.

**What makes it different from a simple timer:**
- Automatically skips irrigation on rainy days
- Irrigates more in hot weather, less in cool weather
- Calculates the exact volume needed per zone
- Works with multiple independent irrigation zones
- Directly controls smart valves (or works in monitoring-only mode)

## 2. How it works

The system continuously tracks a simple water balance:

```
Water lost (evapotranspiration) − Water gained (rain) = Deficit
```

When the deficit is high, your soil is dry and needs water. When it rains, the deficit drops. After irrigation, it resets to zero.

### The key idea

**1 mm of deficit = 1 liter per square meter of water needed.**

So if your deficit is 10 mm and your garden is 45 m², you need 450 liters (adjusted for your irrigation system's efficiency).

### Evapotranspiration (ET)

The integration estimates how much water your soil loses each hour based on temperature:

- **Below 9°C** (configurable): no water loss — plants are dormant
- **Above 9°C**: water loss increases linearly with temperature
- **Hot summer day (35°C)**: approximately 0.24 mm/h → 5.7 mm/day
- **Cool spring day (15°C)**: approximately 0.06 mm/h → 1.3 mm/day

### Precipitation handling

NeverDry tracks rain as a **delta** (increment since the last reading), not as a raw sensor value. This prevents double-counting and works correctly regardless of how often other sensors update.

Two rain sensor types are supported:

| Type | How it works | Examples |
|------|-------------|----------|
| **Event-based** (default) | Each sensor state change represents a single rain event. The value is the amount of rain in that event (e.g., 0.2 mm per tip). If the value doesn't change, no new rain is counted. | Tipping bucket (Ecowitt, Netatmo), DIY pulse counter, ESPHome rain gauge |
| **Daily total** | The sensor reports cumulative mm since midnight. NeverDry computes the difference from the last reading. At midnight rollover (value drops), the new value is treated as fresh accumulation. | Weather station daily rain, OpenWeatherMap precipitation, Met.no |

**Choosing the right type matters**: If you select "event-based" but your sensor actually reports a daily total, the deficit will decrease too aggressively (the full total is subtracted on every change). If you select "daily total" but your sensor reports per-event, only the first event will register correctly.

### Per-zone water demand

Different plants need different amounts of water. NeverDry assigns a **crop coefficient (Kc)** to each zone based on the plant family. The Kc scales the evapotranspiration for that zone:

- **Lawn (Kc ≈ 1.0 in summer)**: loses water at the reference rate
- **Succulents (Kc ≈ 0.35 in summer)**: lose water 3× slower than lawn
- **Vegetables (Kc ≈ 1.10 in summer)**: lose water slightly faster (high transpiration)

The Kc varies seasonally — plants need less water in winter than in summer. NeverDry interpolates between 4 seasonal values automatically and adjusts for your hemisphere based on your Home Assistant location.

Two zones can hold the same plants and still dry out at different rates — one loses the sun behind the house at 14:00, the other sits against a south-facing wall. That is what **site exposure** is for: a per-zone factor that multiplies the Kc, so the zone keeps its seasonal shape instead of being pinned to one number. See [Site exposure](#site-exposure) below.

### Two scheduling modes

| Mode | When it triggers | Best for |
|------|-----------------|----------|
| **Mode A** (threshold) | When deficit exceeds a per-zone limit (e.g., 20 mm) | Daytime safety net, drought-tolerant plants |
| **Mode B** (nightly) | Every night at a fixed time (e.g., 23:00) | Primary scheduler, sensitive plants, pots |

You can use one mode, the other, or both together.

## 3. Requirements

### Minimum

- **Home Assistant** 2024.1.0 or newer
- **Temperature sensor** — any outdoor temperature sensor [°C]
- **Rain sensor** — tipping bucket (mm per event) or weather station (daily total mm)

### Recommended

- **Smart valve(s)** — one per irrigation zone (e.g., Shelly, Sonoff, Zigbee valve)
- **Rain gauge** with mm/pulse output (e.g., Ecowitt, Netatmo, DIY tipping bucket)

### Optional (improved accuracy)

- **VWC (volumetric water content) sensor** — bypasses the ET model entirely with direct soil moisture measurement
- T_max / T_min sensors — enables Hargreaves-Samani formula (~1 mm/day accuracy)

## 4. Installation

### Option A: Manual installation

1. Download or clone the repository
2. Copy the `custom_components/never_dry/` folder to your Home Assistant config directory:
   ```
   /config/custom_components/never_dry/
   ```
3. Restart Home Assistant
4. Go to **Settings → Devices & Services → Add Integration**
5. Search for **NeverDry** and follow the setup wizard (see Section 5)

### Option B: Via HACS

1. In Home Assistant, go to **HACS → Integrations**
2. Search for **NeverDry** and click **Install**
3. Restart Home Assistant
4. Go to **Settings → Devices & Services → Add Integration**
5. Search for **NeverDry** and follow the setup wizard (see Section 5)

## 5. Configuration (UI setup wizard)

The integration is configured entirely through the Home Assistant UI — no YAML editing required.

### Step 1: Sensors and ET model

When you add the integration, the first screen asks for:

| Field | Required | Description |
|-------|----------|-------------|
| **Temperature sensor** | Yes | Outdoor temperature entity (°C). Only entities with `device_class: temperature` are shown. |
| **Rain sensor** | Yes | Precipitation entity (mm) |
| **Rain sensor type** | No | How the sensor reports rain: **Event-based** (mm per event, default — tipping bucket) or **Daily total** (cumulative mm since midnight — weather station) |
| **Alpha (α)** | No | ET coefficient (default: 0.22 mm/°C/day). Higher = more evaporation estimated. |
| **Base temperature (T_base)** | No | Temperature below which ET = 0 (default: 9.0°C) |
| **Max deficit (D_max)** | No | Upper deficit clamp (default: 100.0 mm). Prevents runaway values during sensor outages. |
| **VWC sensor** | No | Optional soil moisture sensor (volumetric water content). If provided, the deficit is calculated directly from soil moisture instead of the ET model. |

### Step 2: Add irrigation zones

For each zone, the wizard asks:

| Field | Required | Description |
|-------|----------|-------------|
| **Zone name** | Yes | Display name (e.g., "Vegetable Garden") |
| **Valve** | No | The `switch` entity that controls this zone's valve. Leave empty for monitoring mode. |
| **Area (m²)** | Yes | Irrigated area in square meters |
| **System type** | Yes | Irrigation method — sets the efficiency. Pick *Custom* to enter your own. |
| **Efficiency override** | For *Custom* | Your own efficiency (0.1–1.0). Used **only** when the system type is *Custom*, and required in that case. |
| **Plant family** | No | Type of plants in this zone — sets a seasonal crop coefficient (Kc) that adjusts water demand throughout the year. See table below. |
| **Custom Kc** | For *Custom* | One fixed Kc (0.1–2.0). Used **only** when the plant family is *Custom*, and required in that case. |
| **Site exposure** | No | How much sun and wind this zone gets compared to an open site. Multiplies the Kc. Default *Full sun, open* (×1.00) changes nothing. See table below. |
| **Custom microclimate factor** | For *Custom* | Your own exposure factor (0.1–1.5). Used **only** when site exposure is *Custom*, and required in that case. |
| **Guard flow rate (L/min)** | For `estimated_flow` | Measured valve flow rate. Required for `estimated_flow`; strongly recommended for `flow_meter` and `volume_preset` too — it drives the expected-duration estimate and lets the safety timeout scale with large deficits (it will become required in a future release). Measure with a bucket and stopwatch. |
| **Threshold (mm)** | No | Deficit threshold for Mode A triggering (default: 20.0 mm) |

**System type defaults:**

| System type | Default efficiency |
|-------------|-------------------|
| Drip irrigation | 0.92 |
| Micro-sprinklers | 0.80 |
| Pop-up sprinklers | 0.68 |
| Manual / hose | 0.55 |

**Plant family Kc profiles** (seasonal, auto-adjusted for hemisphere):

| Family | Winter | Spring | Summer | Autumn |
|--------|--------|--------|--------|--------|
| Lawn / Turf grass | 0.45 | 0.85 | 1.00 | 0.70 |
| Vegetables (seasonal) | 0.30 | 0.70 | 1.10 | 0.50 |
| Fruit trees (deciduous) | 0.35 | 0.70 | 0.95 | 0.55 |
| Ornamental shrubs | 0.40 | 0.65 | 0.80 | 0.55 |
| Herbs (Mediterranean) | 0.30 | 0.55 | 0.70 | 0.40 |
| Citrus / Evergreen fruit | 0.60 | 0.65 | 0.70 | 0.65 |
| Roses | 0.35 | 0.75 | 0.95 | 0.55 |
| Succulents / Cacti | 0.15 | 0.25 | 0.35 | 0.20 |
| Native ground cover | 0.25 | 0.45 | 0.55 | 0.35 |
| Mixed garden (default) | 0.40 | 0.70 | 0.90 | 0.55 |

The Kc values are interpolated linearly between seasons. The hemisphere is auto-detected from your Home Assistant location settings — in the southern hemisphere, the seasonal profile is automatically flipped.

#### Site exposure

The plant family says *what* grows in the zone. Site exposure says *where* the zone is: shaded, open, windy, or up against hot paving. It is applied as a multiplier on top of the Kc:

```
Kc_effective = Kc (plant family or custom override) × exposure factor
```

| Exposure | Factor | Pick it when |
|----------|--------|--------------|
| Deep / all-day shade | 0.60 | The zone never gets direct sun (north side, under dense canopy) |
| Morning sun, afternoon shade | 0.75 | Sun until roughly midday, then shaded by a building or trees |
| Morning shade, afternoon sun | 0.85 | Shaded early, then exposed through the hottest hours |
| **Full sun, open (default)** | **1.00** | Open lawn or bed with no shading and no unusual wind |
| Windy / exposed | 1.15 | Rooftop, ridge, or a channelled wind that dries the zone out fast |
| Reflected heat (paving, south-facing wall) | 1.20 | Bordered by paving, gravel, or a wall that radiates heat back |
| Custom | your value, 0.1–1.5 | You have measured or calculated your own factor |

### One rule for the three custom values

System type, plant family and site exposure work the same way: **the dropdown
decides.** Each has a *Custom* entry, and only that entry uses the value you
type in the box beside it. Choose a normal preset and the box is ignored — the
form tells you so when you save, rather than quietly using one or the other.

Choosing *Custom* and leaving the box empty is refused: it would mean nothing.

Zones configured before this rule keep watering exactly as they did. On upgrade,
any zone whose value was already in charge is marked *Custom* automatically.

**Why not just lower the Kc?** Because a fixed Kc freezes the zone at one season's value. For an east-facing lawn that loses the sun at 14:00, a manual `Kc = 0.70` is about right in August and drifts badly from there:

| Date | Seasonal Kc (lawn) | With exposure ×0.75 | Frozen Kc 0.70 |
|------|--------------------|---------------------|----------------|
| Aug 5 | 0.93 | 0.70 | correct |
| Sep 15 | 0.80 | 0.60 | +17% |
| Oct 15 | 0.70 | 0.53 | +33% |
| Nov 15 | 0.62 | 0.46 | +52% |

The error peaks in autumn, exactly when over-watering shaded turf invites disease.

The presets come from the landscape coefficient method (`K_L = k_s × k_d × k_mc`, Costello, Matheny & Clark 2000): the plant family provides the species factor `k_s`, exposure provides the microclimate factor `k_mc`. They are expert-judgment starting points, not measurements — adjust them for your garden and report back what works.

**Notes:**
- The factor applies to a **custom Kc as well**, since exposure describes the site rather than the planting. If you set `Kc = 0.80` and *Deep shade*, the zone runs at 0.48. The two multiply out, so a high custom Kc with an above-1.0 exposure can exceed the 0.1–2.0 range of the Kc field itself (2.0 × 1.20 = 2.4). That is not capped, on purpose: both numbers are your explicit choice.
- Values **above 1.0** are intentional. A zone against hot paving genuinely exceeds reference ET.
- The zone's **Kc sensor** shows the effective value, with `kc_base`, `exposure`, and `microclimate_factor` as attributes so you can see how it was composed.
- Leaving exposure at *Full sun, open* is a no-op — existing zones behave exactly as before.

### Step 3: Add more zones or finish

After each zone, you are asked whether to add another zone or complete setup. You can add as many zones as you need.

> **A zone added later starts at zero deficit.** A brand-new zone begins as if freshly watered and builds its deficit up from there as the weather dries it out — it does not copy the current deficit of your existing zones. This is intentional: it avoids a new zone spuriously reporting "irrigation due" the moment you add it. If you *want* all your zones to share the same starting point, click **"Mark irrigated"** on each zone — this resets a zone's deficit to zero without opening the valve, aligning them all.

### Step 4: Verify

After setup, check that these entities exist in **Settings → Devices & Services → Entities**:

- `sensor.et_hourly_estimate` — should show a value in mm/h
- `sensor.never_dry` — should show 0.0 mm (starts fresh)
- `sensor.irrigation_<zone_name>` — one per zone, showing 0.0 L

## 6. Understanding the sensors

### ET Hourly Estimate (`sensor.et_hourly_estimate`)

Shows the current rate of water loss from the soil in mm/h.

- **0.00**: temperature is below base (plants dormant, no water loss)
- **0.05–0.10**: cool day, low water loss
- **0.15–0.25**: hot day, significant water loss
- **> 0.25**: very hot day, high water loss

### NeverDry (`sensor.never_dry`)

The reference sensor. Shows cumulative water deficit in mm at Kc=1.0 (the "raw" deficit before plant-specific adjustment). Each zone tracks its own deficit scaled by its crop coefficient.

- **0 mm**: soil is at field capacity (just rained or irrigated)
- **5–15 mm**: soil is drying but most plants are fine
- **15–25 mm**: time to irrigate sensitive plants and pots
- **25–40 mm**: most plants need water
- **> 40 mm**: severe deficit, risk of plant stress

The deficit is clamped at `D_max` (default 100 mm) to prevent runaway accumulation.

### Irrigation Zone (`sensor.irrigation_<zone_name>`)

Shows the volume of water needed for this specific zone in liters.

**Attributes** (visible in the entity detail):

| Attribute | Meaning |
|-----------|---------|
| `zone_name` | Zone display name |
| `volume_liters` | Water needed [L] |
| `duration_s` | Expected valve run time [seconds] — from the live flow-meter rate while irrigating (reads as remaining time), otherwise from the configured guard flow rate; 0 if neither is available |
| `deficit_mm` | This zone's current deficit [mm] (per-zone, not shared) |
| `plant_family` | Plant family key (e.g., "lawn", "vegetables") |
| `kc` | Current **effective** crop coefficient (seasonal, after site exposure) |
| `kc_base` | Crop coefficient before site exposure (plant family curve or manual override) |
| `kc_override` | Manual Kc override value, if set |
| `exposure` | Site exposure key (e.g., "morning_sun"), or `null` if unset |
| `microclimate_factor` | Resolved exposure multiplier applied to `kc_base` |
| `valve` | Associated valve entity |
| `system_type` | Irrigation system type |
| `area_m2` | Zone area |
| `efficiency` | Distribution efficiency |
| `flow_rate_lpm` | Valve flow rate |
| `threshold_mm` | Mode A trigger threshold |
| `irrigating` | `true` if this zone is currently being irrigated |

### Water totals: Rain Yearly and Irrigated Yearly

Each zone reports two yearly totals in **liters**, kept as two clean, separate
figures rather than one mixed number:

- **Rain Yearly [L]** — the rain this zone received this year. Rain is a system
  quantity measured once in millimeters, then projected onto each zone by its
  area: `Rain Yearly [L] = accumulated rain [mm] × zone area [m²]` (1 mm over
  1 m² = 1 liter).
- **Irrigated Yearly [L]** — the water **you** delivered by irrigation this year
  (irrigation only, rain excluded). This is the meaningful *consumption* figure
  and feeds the Home Assistant Energy dashboard's water tracking. To get the
  grand total of water the zone received, add the two.

**How the accumulation works.** The rain millimeters are accumulated by NeverDry
from your configured rain sensor as rain falls, and the total **resets on 1
January** (calendar-year accumulation). Because NeverDry can only count rain from
the moment it is installed, the **first year is partial**: it accumulates from
the installation date up to 31 December, and only from the following 1 January
onward does it cover a full calendar year. Irrigation is counted the same way —
per calendar year — so a freshly installed system starts both totals building
up from the install date.

If you want these totals to reflect rain that fell *before* NeverDry was
installed, that historical rain cannot be reconstructed from a live sensor; the
totals are only guaranteed correct from the install date forward.

**Resetting the yearly totals.** Both totals reset on their own on 1 January,
but you can clear them at any time from the **NeverDry** hub device, which
carries two buttons:

- **Reset yearly rain** — zeroes the system-wide yearly rain total (shared by
  every zone's *Rain Yearly [L]*). Use it if the figure is wrong — for example
  after changing rain sensor type — instead of waiting for the new year.
- **Reset yearly water** — zeroes *Irrigated Yearly [L]* for every zone at once.
  Each zone's lifetime total is preserved; only the year-to-date figure resets.

Both are **state-only**: they restart the counters from zero but leave your
historical charts (Energy dashboard statistics) untouched. Note that the yearly
rain total is kept as a saved value that survives a restart *and a plain
reinstall* — so if a wrong figure won't go away, this button is the way to clear
it (reinstalling the integration on its own will not).

## 7. Irrigation logic — how it all works

This diagram shows the complete irrigation decision flow, from weather data to valve control.

```
                    ┌─────────────────┐
                    │  Weather Input  │
                    │  (Temperature,  │
                    │   Rain, Wind*)  │
                    └────────┬────────┘
                             │
                    ┌────────▼────────┐
                    │   ET Estimate   │
                    │  ET_h = α(T-Tb) │
                    │    / 24 [mm/h]  │
                    └────────┬────────┘
                             │
              ┌──────────────▼──────────────┐
              │    Dryness Index (global)    │
              │  D(t) = D(t-1) + ET - Rain  │
              │       [mm, Kc=1.0]          │
              └──────────────┬──────────────┘
                             │
              ┌──────────────▼──────────────┐
              │  Per-Zone Deficit (x Kc)    │
              │  D_zone = D_zone + ET*Kc*Δt │
              │          - Rain             │
              └──────────────┬──────────────┘
                             │
    ┌──────────┬──────────┬──────────┬──────────────┐
    │          │          │          │              │
┌───▼───┐ ┌────▼────┐ ┌───▼────┐ ┌───▼──────┐ ┌─────▼──────┐
│Schedul│ │ Button  │ │External│ │ Mark     │ │ Reactive   │
│ (HH:MM│ │"Irrigate│ │ open   │ │irrigated │ │ (Mode A)   │
│  )    │ │   "     │ │(physic)│ │(no valve)│ │            │
└───┬───┘ └────┬────┘ └───┬────┘ └────┬─────┘ └─────┬──────┘
    │ deficit  │           │           │             │ deficit
    │ ≥ thr?   │           │           │             │ ≥ thr?
    │          │           │           │             │
    │   ┌──────┼───────────┼───────────┼─────────────┘
    │   │      │           │           │
┌───▼───▼──────▼──┐  ┌─────▼─────┐  ┌──▼─────────────┐
│ Open valve via  │  │ External  │  │ Reset deficit  │
│ ValveOperator   │  │ session   │  │ (no valve)     │
│ (FSM verified)  │  │ monitor   │  │ source=mark_   │
│                 │  │ starts    │  │ irrigated      │
│ set_irrigating  │  │           │  └────────────────┘
│ = True          │  │ is_irrig= │
└────────┬────────┘  │ True      │
         │           │ baseline= │
┌────────▼────────┐  │ flow read │
│ Monitor deliver │  └─────┬─────┘
│  • estimated    │        │
│  • flow_meter   │  ┌─────▼──────────────────────┐
│  • flow_rate    │  │ Wait for min(              │
│  • volume_preset│  │  volume_target reached,    │
└────────┬────────┘  │  estimated duration,       │
         │           │  delivery_timeout          │
┌────────▼────────┐  │ )                          │
│ Close valve     │  │ → switch.turn_off          │
│ (target reached │  └─────┬──────────────────────┘
│  / stop /       │        │
│  timeout)       │        │ OR user closes manually
└────────┬────────┘        │
         │                 │
┌────────▼─────────────────▼───────────┐
│ on→off detected → finalise session   │
│  set_irrigating = False              │
│  last_irrigated = now                │
│  last_volume_delivered = measured    │
│  last_irrigation_source = button /   │
│    scheduled / reactive / manual     │
│  deficit: -delivered mm × η          │
│  (zeroed only if target fully met;   │
│   full reset only by mark_irrigated) │
│  fire never_dry_irrigation_complete  │
│  (event source = same string as      │
│   last_irrigation_source)            │
└──────────────────────────────────────┘
```

### Key concepts

| Concept | Description |
|---------|-------------|
| **Dryness Index** | Global reference deficit (Kc=1.0). Only decreases with rain, never with irrigation. |
| **Zone Deficit** | Per-zone deficit scaled by crop coefficient Kc. Decreases with rain AND irrigation. |
| **Threshold** | Minimum deficit (mm) to trigger automatic irrigation. Below this, the zone is "wet enough". |
| **Irrigation Time** | Daily time (HH:MM) when NeverDry checks each zone and irrigates if deficit >= threshold. |
| **Delivery Mode** | How the valve delivers water: timer, flow meter (cumulative), or flow rate (L/h integration). |
| **Partial Irrigation** | If timeout or stop, deficit is reduced proportionally to the volume actually delivered. |
| **Manual Detection** | If someone opens the valve manually (app, button), NeverDry detects it and adjusts the deficit. |

### 7.1 The four irrigation triggers

A zone's irrigated state (`is_irrigating`, `last_irrigated`, `last_volume_delivered`, deficit) can be modified in exactly four ways. NeverDry handles **all four** with the same final accounting — the only thing that changes is *who* opens and closes the valve.

| # | Trigger | Who opens the valve | Who closes the valve | NeverDry valve involved? | Flow meter used (if configured)? |
|---|---|---|---|---|---|
| 1 | **Physical button on the valve** / Zigbee remote / HA switch toggled by hand | The user (outside NeverDry) | NeverDry (auto-close) or the user, whichever happens first | Yes | Yes |
| 2 | **"Irrigate" button** on the zone device in HA | NeverDry (via `irrigate_zone` service) | NeverDry | Yes | Yes |
| 3 | **Automation / scheduler** (Mode A reactive, Mode B scheduled, manual `irrigate_zone`/`irrigate_all` service from an automation) | NeverDry | NeverDry | Yes | Yes |
| 4 | **"Mark irrigated" button** / `mark_irrigated` service | Nobody — the user has already watered with a *different* tool (a hose, a separate sprinkler, rain not detected by the sensor) | Nobody | **No** — the water did not pass through NeverDry's valve | **No** — the flow meter would not have seen anything anyway |

#### Trigger 1 — Physical button on the valve (or any external open)

This covers any case where the valve switch turns **on** without NeverDry having asked for it: pressing the button on the Sonoff SWV, opening the valve from the Zigbee app, flipping the HA switch directly, an automation that bypasses NeverDry and calls `switch.turn_on` on the valve entity.

What happens:

1. NeverDry sees the switch transition `off → on`. If a NeverDry cycle is already running on that valve it is ignored (the FSM is not idle); otherwise the manual session starts.
2. `is_irrigating` is set to `True` on the zone, so the UI and any automation listening on the attribute reacts as if a commanded cycle were running.
3. A baseline is recorded for the flow meter (current cumulative reading, or open timestamp for rate sensors).
4. An **auto-close monitor** starts in the background. It will close the valve via `switch.turn_off` at the **minimum** of:
   - **Volume needed** — if the zone has a flow meter, the monitor polls it and closes as soon as the delivered volume covers the current deficit-driven target (`volume_liters`). Without a flow meter but with a configured `flow_rate`, the monitor sleeps for the estimated duration `volume / flow_rate`.
   - **Safety timeout** (`delivery_timeout`, default 1 hour) — always honoured as the upper bound, so a forgotten-open valve cannot run indefinitely. When the zone has a guard flow rate configured, the timeout automatically grows to `1.1 ×` the guard-flow duration estimate, so a large deficit is never cut short by the default floor.
5. When the switch goes `on → off` (either because the user closed it, or because the monitor closed it):
   - `is_irrigating` flips back to `False`.
   - `last_irrigated` is stamped with the current time.
   - `last_irrigation_source` is set to `"manual"`.
   - The deficit is reduced by the delivered volume. With a flow meter the reduction is exact; without one (or with a zero reading) the volume is estimated as `flow_rate × elapsed time`. The deficit is never fully reset by closing the valve — only **Mark irrigated** does that. If the zone has neither a flow meter nor a `flow_rate`, the deficit is left unchanged and a warning is logged.
   - `last_volume_delivered` is updated and `never_dry_irrigation_complete` is fired on the HA event bus with `source: manual`.

> **No deficit → manual open still works.** If you open the valve when the zone has no deficit, NeverDry still tracks the session and updates `last_irrigated`; the deficit just cannot go below zero. The monitor falls back to the safety timeout because there is no volume target to aim for.

#### Trigger 2 — "Irrigate" button on the device page

The per-zone **Irrigate** button calls the `never_dry.irrigate_zone` service. NeverDry computes the target from the current deficit, opens the valve through the ValveOperator (with its OPEN/CLOSE verification FSM and retry policy), monitors delivery according to the configured `delivery_mode`, and closes the valve when the target is reached, the user presses **Stop**, or the timeout fires.

`is_irrigating` is `True` for the duration of the cycle, `last_irrigation_source` is `"button"`, and the deficit is fully reset on success or proportionally reduced on partial delivery. `last_volume_delivered` reflects the measured (flow meter) or estimated (timer) volume.

#### Trigger 3 — Automation / scheduler

Same code path as trigger 2, only the source differs:

- **Mode A (reactive)** — fires when the deficit crosses the threshold. `last_irrigation_source = "reactive"`.
- **Mode B (scheduled)** — fires at the configured daily time **regardless of the threshold**: a schedule means "top this zone back up at this hour", and gating it on the reactive threshold would silently turn every scheduled run into a reactive one. The dose is whatever deficit has accumulated, so the zone is topped up even when it sits below the threshold; the only zone skipped is one with nothing to refill (deficit ≤ 0). The threshold stays a *reactive*-mode concept. `last_irrigation_source = "scheduled"`.
- **Custom automation** — your own automation calling `never_dry.irrigate_zone` or `never_dry.irrigate_all`. Source = `"button"` (same as the manual button — the service entry point is shared).

All of them go through `ValveOperator` and `_deliver_water`. Volume tracking, partial irrigation, and the `never_dry_irrigation_complete` event behave identically to trigger 2.

#### Trigger 4 — "Mark irrigated" button

Use this **only** when you watered the zone *without* using NeverDry's valve: a hand-held hose, a separate non-integrated sprinkler, an unmetered rain event, your neighbour helping out. The button calls the `never_dry.mark_irrigated` service, which:

- Does **not** open or close any valve.
- Does **not** read any flow meter.
- Resets the zone deficit to zero and stamps `last_irrigated` with `source = "mark_irrigated"`.
- Does **not** touch `is_irrigating` (no actual NeverDry-tracked irrigation is happening).

Its purpose is to keep NeverDry from over-watering on top of irrigation it has no other way of knowing about.

### 7.2 Open and close — what NeverDry does in each case

| Step | Trigger 1 (manual) | Trigger 2/3 (button / automation) | Trigger 4 (mark_irrigated) |
|---|---|---|---|
| **On open** | Detected via switch state change. `is_irrigating=True`, flow meter baseline saved, auto-close monitor started. | NeverDry commands `switch.turn_on` via ValveOperator. FSM verifies the open. `is_irrigating=True`. | Nothing — no valve is opened. |
| **During delivery** | Monitor polls the flow meter (or sleeps for the estimated duration) tracking how much water has flowed. | `_deliver_water` runs the chosen delivery mode (`estimated_flow`, `flow_meter`, `volume_preset`). | Nothing. |
| **On close** | Whichever of (volume target reached, estimated duration elapsed, safety timeout) fires first → NeverDry calls `switch.turn_off`. The user can also close manually at any time. | Target reached, user pressed Stop, or `delivery_timeout` fires → NeverDry calls `switch.turn_off`. | Nothing. |
| **After close** | `is_irrigating=False`, `last_irrigated=now`, `last_irrigation_source="manual"`, deficit reduced by the measured volume (or by the `flow_rate × elapsed` estimate without a meter; never fully reset), `never_dry_irrigation_complete` event fired with `source: "manual"`. | `is_irrigating=False`, `last_irrigated=now`, `last_irrigation_source` and the event `source` both set to `"button"`/`"reactive"`/`"scheduled"` depending on the trigger, deficit reset (full delivery) or reduced (partial). | Deficit reset to zero, `last_irrigated=now`, `last_irrigation_source="mark_irrigated"`. No event. |

### 7.3 Why the auto-close on manual open?

Imagine pressing the button on the valve and then forgetting about it. Without an auto-close, the valve stays open until the battery dies — which on a Sonoff SWV is days of continuous flow.

NeverDry's auto-close uses the *same* `delivery_timeout` you already configured for commanded cycles (default 1 hour, editable per zone in the options flow). On top of that, if the zone has a flow meter or a calibrated `flow_rate`, NeverDry will close the valve *earlier* — as soon as the deficit-driven volume is reached — saving water exactly like a scheduled cycle would. The user-initiated open still benefits from the same closed-loop control.

If you want to bypass the auto-close (for example to flush a line at the start of the season), close the valve from the same physical button before the timeout. Manual close always wins over the monitor.

---

## 8. Setting up automations

### Mode A: Threshold trigger

This automation starts irrigation when the deficit crosses a threshold. The integration handles valve control — just call the service.

```yaml
automation:
  - alias: "Irrigation threshold — Vegetable Garden"
    trigger:
      - platform: numeric_state
        entity_id: sensor.never_dry
        above: 15
    condition:
      - condition: time
        after: "06:00:00"
        before: "09:00:00"
    action:
      - service: never_dry.irrigate_zone
        data:
          zone_name: "Vegetable Garden"
```

**What happens when you call `irrigate_zone`:**
1. The controller opens the valve (e.g., `switch.valve_vegetables`)
2. Waits for the calculated duration (based on deficit, area, flow rate, efficiency)
3. Closes the valve automatically

**Tips for Mode A:**
- Set the time window to early morning (6:00–9:00) to reduce evaporation
- Each zone can have its own threshold
- If a cycle is already running, the request is ignored (no double-irrigation)

### Mode B: Nightly deficit-based

This automation runs every night and irrigates all zones sequentially.

```yaml
automation:
  - alias: "Nightly irrigation — all zones"
    trigger:
      - platform: time
        at: "23:00:00"
    condition:
      - condition: numeric_state
        entity_id: sensor.never_dry
        above: 1
    action:
      - service: never_dry.irrigate_all
```

**What happens when you call `irrigate_all`:**
1. Irrigates each zone sequentially
2. Each zone: open valve → wait for duration → close valve
3. 30-second pause between zones (configurable via `inter_zone_delay`)
4. After all zones complete, deficit is reset to zero
5. On rainy days, deficit is near 0 → condition skips → nothing happens

### Emergency stop

If you need to stop irrigation immediately:

```yaml
- service: never_dry.stop
```

This closes **all** valves instantly, regardless of which zone is active. Any in-progress irrigation cycle is aborted.

### Combining Mode A + B

Use Mode B as the primary nightly scheduler and Mode A as a daytime safety net for heat waves:

- Mode B runs every night at 23:00 with threshold > 1 mm
- Mode A triggers during the day only if deficit exceeds a high threshold (e.g., 30 mm)

### Services reference

| Service | Parameters | Description |
|---------|-----------|-------------|
| `never_dry.irrigate_zone` | `zone_name` (required) | Irrigate one zone (open valve → wait → close) |
| `never_dry.irrigate_all` | — | Irrigate all zones sequentially, then reset deficit |
| `never_dry.stop` | — | Emergency stop: close all valves immediately |
| `never_dry.reset` | — | Manually reset deficit to zero |

## 9. Monitoring mode (no valves)

If you don't have smart valves, the integration works in **monitoring mode**. This mode activates automatically when no zones have a valve configured.

- Sensors track deficit and calculate volumes as usual
- Every **6 hours**, if the deficit exceeds any zone's threshold, you receive a **persistent notification** in Home Assistant telling you how much water each zone needs
- You can then water manually based on the recommended volumes

This is useful for:
- Getting started before installing smart valves
- Zones with manual watering (hose, watering can)
- Understanding your garden's water needs

The notification looks like:

> **Irrigation needed**
>
> Soil water deficit is **18.5 mm**. Your garden needs watering:
> - **Vegetable Garden**: 411 L (51 min)
> - **Lawn**: 1321 L (88 min)
>
> No irrigation valves are configured — please water manually or configure valves in the integration settings.


## 10. Editing settings after setup

You can modify the integration settings at any time without removing and re-adding it.

Go to **Settings → Devices & Services → NeverDry → Configure**.

The options flow provides two actions:

| Option | What you can change |
|--------|-------------------|
| **Edit model parameters** | Alpha (α), base temperature (T_base), max deficit (D_max) |
| **Add zone** | Add a new irrigation zone with all its parameters |

Changes take effect immediately — no restart required.

## 11. Updating the integration

NeverDry follows semantic versioning (e.g., `0.1.0` → `0.2.0`). Updates are safe — your configuration and sensor history are preserved automatically.

### Via HACS (recommended)

1. Open **HACS** → **Integrations**
2. If an update is available, NeverDry will show an **"Update available"** badge
3. Click on NeverDry → **Update**
4. Restart Home Assistant when prompted

HACS checks for new releases automatically. You will see a notification in the Home Assistant sidebar when an update is available.

### Manual update

1. Download the latest release from [GitHub Releases](https://github.com/never-dry/NeverDry/releases)
2. Extract `never_dry.zip`
3. Replace the contents of `config/custom_components/never_dry/` with the new files
4. Restart Home Assistant

### What happens during an update

- **Sensor state is preserved** — deficit values, zone data, and history survive the update thanks to `RestoreEntity`
- **Configuration is migrated automatically** — if the new version changes the config schema, your settings are upgraded seamlessly (no need to remove and re-add the integration)
- **Automations continue to work** — service names and entity IDs remain stable across updates

### Version history

Check the [GitHub Releases](https://github.com/never-dry/NeverDry/releases) page for detailed release notes, including new features, bug fixes, and any breaking changes.

## 12. Calibration guide

### Week 1: Start with defaults

Use the default parameters (`alpha: 0.22`, `t_base: 9.0`) and observe.

### Week 2: Adjust alpha

| Observation | Action |
|-------------|--------|
| Plants wilt before irrigation triggers | Increase `alpha` (try 0.28) |
| Soil stays too wet, over-irrigating | Decrease `alpha` (try 0.18) |
| Irrigation seems about right | Keep current value |

You can change alpha via the options flow (see Section 9) without restarting.

### Week 3: Fine-tune threshold

| Plant type | Suggested threshold |
|-----------|-------------------|
| Pots and containers | 10–15 mm |
| Vegetable garden | 15–25 mm |
| Flower beds | 20–30 mm |
| Established lawn | 25–40 mm |
| Drought-tolerant plants | 35–50 mm |

### Using a VWC sensor

If you have a soil moisture sensor that reports volumetric water content (VWC), you can configure it in the setup wizard. When a VWC sensor is provided:
- The ET model is bypassed
- Deficit is calculated directly: `deficit = (field_capacity - VWC) × root_depth × 1000`
- Default field capacity: 0.30 (30%)
- Default root depth: 0.30 m

This gives the most accurate deficit estimate, especially in variable soil conditions.

### Seasonal adjustments

The model automatically adapts to seasons through temperature:
- **Summer**: higher temperatures → higher ET → more frequent irrigation
- **Winter**: lower temperatures → ET near zero → almost no irrigation

No manual seasonal adjustment is needed. If you find the model significantly over- or under-estimates in a specific season, adjust `alpha` by ±0.05 via the options flow.

## 13. Dashboard examples

### NeverDry Zone Card (recommended)

NeverDry bundles a custom Lovelace card that shows everything about **one zone** at a glance. It is installed with the integration and **auto-registered** — you do **not** need to add a Lovelace resource manually. After install (and a browser refresh) it appears in the **"Add card"** picker as *NeverDry Zone Card*; open the card editor, pick the zone, and save.

<p align="center">
  <img src="https://raw.githubusercontent.com/never-dry/NeverDry/main/docs/assets/zone-card.png" alt="NeverDry Zone Card" width="320">
</p>

The card groups the zone's entities by time horizon:

- **Status chips** — valve state, irrigating / maintenance, and the source of the last irrigation
- **Deficit vs threshold** — a bar showing how close the zone is to needing water
- **Next session** — planned volume and run duration
- **Last session** — last irrigated, duration, volume, and water delivered
- **Totals** — Rain Yearly and Irrigated Yearly (liters, per zone)
- **Parameters** — threshold, flow rate, area, Kc, efficiency, mode
- **Actions** — *Irrigate now*, *Mark as irrigated*, and *Reset valve* as real buttons

Labels and units follow your Home Assistant language and unit system automatically, and entity resolution is rename-safe. Add one card per zone for a complete dashboard.

> If the card does not appear after installation, hard-refresh your browser (Ctrl/Cmd+Shift+R) to clear the frontend cache.

If you prefer to build your own dashboard, the manual examples below use the underlying entities directly.

### Simple status card

```yaml
type: entities
title: Irrigation Status
entities:
  - entity: sensor.never_dry
    name: Soil Water Deficit
  - entity: sensor.et_hourly_estimate
    name: Current ET Rate
  - entity: sensor.irrigation_vegetable_garden
    name: Vegetables — Volume needed
  - entity: sensor.irrigation_lawn
    name: Lawn — Volume needed
```

### History graph

```yaml
type: history-graph
title: Deficit History (7 days)
hours_to_show: 168
entities:
  - entity: sensor.never_dry
    name: Deficit
  - entity: sensor.et_hourly_estimate
    name: ET Rate
```

### Conditional alert card

```yaml
type: conditional
conditions:
  - condition: numeric_state
    entity: sensor.never_dry
    above: 25
card:
  type: markdown
  content: >
    **Soil is getting dry!**
    Deficit: {{ states('sensor.never_dry') }} mm.
    Vegetables need {{ state_attr('sensor.irrigation_vegetable_garden', 'volume_liters') }} L.
```

## 14. Troubleshooting

### Reading the NeverDry activity log

NeverDry writes a dedicated log file to your HA configuration directory:

```
<ha_config_dir>/never_dry_activity.log
```

Every decision the integration makes — whether the threshold was met, whether the
valve was told to open, how long the timeout was, why a cycle was skipped — is
recorded there at INFO level. The file rotates at 5 MB and keeps 2 backups, so you
always have the recent history available.

**Quickest way to read it:** use the **Download diagnostics** button in the
integration card (**Settings → Devices & Services → NeverDry → ⋮ → Download
diagnostics**). The downloaded JSON includes the last 500 log lines plus a state
snapshot of all NeverDry entities — ready to attach to a bug report or share with
the community.

### Irrigation did not fire today

If you expected irrigation but nothing happened, open the activity log and search
for the scheduled time. You will find one of these outcomes:

| What you see | Meaning |
|---|---|
| No `Scheduled check fired:` entry | The time trigger never ran — check the zone's `irrigation_time` setting and reload the integration |
| `no irrigation needed` | The deficit was below the threshold at trigger time — check `deficit_mm` and `threshold_mm` in the log line |
| `Scheduled irrigation for '…' skipped` | Another zone was still irrigating — check the previous cycle's `SESSION_RESULT` for its duration |
| `needs 0L irrigation — skipping` | `volume_liters` computed to 0 — log line shows `deficit`, `area`, `efficiency`; check zone configuration |
| `Attempting valve open:` present, no `Completed irrigation:` | Valve failed to open — look for `ERROR` lines immediately after |

For reactive mode, search for `Reactive check:` to see if a skip due to a
concurrent cycle is logged, or `Reactive irrigation triggered:` to confirm it fired.

### Sensors show "unavailable"

- Check that the temperature and rain sensor entity IDs are correct in the integration settings
- Verify the source sensors are online and reporting values
- Check the Home Assistant logs: **Settings → System → Logs** → filter for `never_dry`

### Deficit never increases

- Is your temperature sensor reporting values above `t_base` (default 9°C)?
- Check `sensor.et_hourly_estimate` — if it shows 0.0, ET is not being calculated
- Verify the temperature sensor entity ID is correct in the integration config

### Deficit never decreases

- Is your rain sensor reporting values? Check its entity in HA
- Make sure you selected the correct **rain sensor type** in the setup wizard:
  - **Event-based**: for tipping buckets that report mm per event (e.g., 0.2mm per tip)
  - **Daily total**: for weather stations that report cumulative mm since midnight
- If using a cumulative sensor with "event" mode, the deficit will decrease too much on every temperature update

### Deficit grows unexpectedly large

- Check the `D_max` setting — this clamps the maximum deficit (default: 100 mm)
- If the rain sensor is offline, deficit will only accumulate. Fix the sensor and call `never_dry.reset` if needed.

### Volume shows 0 L

- Check that `area_m2` and `flow_rate_lpm` are configured for the zone
- Verify `efficiency` is not set to 0

### Irrigation runs too long / too short

- Measure your actual flow rate: run the valve for 1 minute into a bucket
- Check that `area_m2` is accurate
- Adjust `efficiency` via the options flow based on your irrigation type

### State resets after HA restart

- The integration uses `RestoreEntity` — state should survive restarts
- If state is lost, check that the entity's `unique_id` hasn't changed
- Check HA logs for restore errors

## 15. FAQ

**Q: Does it work without a rain sensor?**
A: Technically yes, but the deficit will only increase (never decrease from rain). You would need to manually call `never_dry.reset` after significant rain. A rain sensor is strongly recommended.

**Q: Can I use a weather integration instead of physical sensors?**
A: Yes. You can use any HA entity that provides temperature in °C and precipitation in mm. Weather integrations (OpenWeatherMap, Met.no, etc.) work, but physical sensors are more accurate for your specific location.

**Q: What happens during a power outage?**
A: The deficit state is persisted and restored when HA restarts. The time gap during the outage means some ET was not tracked, resulting in a slight underestimate. This is generally acceptable.

**Q: Can different zones have different deficit thresholds?**
A: Yes. Each zone has its own `threshold` parameter for Mode A. The underlying deficit is shared (same soil, same weather), but each zone can trigger at a different level.

**Q: How do I handle zones with different sun exposure?**
A: The current model uses a single deficit for all zones. For significantly different microclimates (e.g., full sun vs. deep shade), consider running two separate instances of the integration with different `alpha` values.

**Q: Can I add or remove zones after initial setup?**
A: You can add new zones via **Settings → Devices & Services → NeverDry → Configure → Add zone**. Removing zones currently requires removing and re-adding the integration.

**Q: What does D_max do?**
A: D_max (default 100 mm) is the maximum value the deficit can reach. It prevents the deficit from growing indefinitely during extended dry periods or sensor outages. In practice, values above 100 mm rarely occur in residential settings.

**Q: What is the VWC sensor option?**
A: If you have a soil moisture sensor that reports volumetric water content (as a fraction, e.g., 0.25 for 25%), the integration can calculate the deficit directly from that measurement instead of using the temperature-based ET model. This is more accurate but requires a suitable sensor.

**Q: How accurate is the ET estimate?**
A: With temperature only, the model explains 40–60% of the real ET variance in temperate climates. It's sufficient for residential use but not for professional agriculture. Adding T_max/T_min sensors (Hargreaves-Samani) improves accuracy to ~1 mm/day.

---

## Disclaimer

NeverDry is a **hobby project for residential use**. It is not certified for agricultural, commercial, or safety-critical applications. The authors accept no liability for crop damage, water waste, property damage, or any other loss resulting from the use of this software.

The ET model is a simplification of the FAO-56 standard and is **not a substitute for professional agronomic advice**. Crop coefficients (Kc) are approximate seasonal averages — actual water needs depend on soil type, microclimate, plant health, and many other factors.

**Always monitor your irrigation system** and verify that valves open and close correctly.

## Acknowledgments

This project was developed with the assistance of [Claude](https://claude.ai) by [Anthropic](https://anthropic.com).

## Scientific References

- Allen, R.G., Pereira, L.S., Raes, D., Smith, M. (1998). *Crop evapotranspiration.* [FAO Irrigation and Drainage Paper 56](https://www.fao.org/4/x0490e/x0490e00.htm).
- Hargreaves, G.H., Samani, Z.A. (1985). Reference crop evapotranspiration from temperature. *Applied Engineering in Agriculture*, 1(2), 96–99. [DOI: 10.13031/2013.26773](https://doi.org/10.13031/2013.26773)
- Penman, H.L. (1948). Natural evaporation from open water, bare soil and grass. *Proc. R. Soc. London A*, 193, 120–145. [DOI: 10.1098/rspa.1948.0037](https://doi.org/10.1098/rspa.1948.0037)
- Monteith, J.L. (1965). Evaporation and environment. *Symp. Soc. Exp. Biol.*, 19, 205–234. [Rothamsted Repository](https://repository.rothamsted.ac.uk/item/8v5v7/evaporation-and-environment)
- Fereres, E., Soriano, M.A. (2007). Deficit irrigation for reducing agricultural water use. *J. Exp. Bot.*, 58(2), 147–159. [DOI: 10.1093/jxb/erl165](https://doi.org/10.1093/jxb/erl165)
