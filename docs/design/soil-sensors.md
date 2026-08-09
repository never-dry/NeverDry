# Soil Moisture Sensors — Reliability, Selection & Integration

Why NeverDry defaults to an ET-based model rather than a soil-moisture sensor,
when a sensor is actually worth it, how to choose one, and how to wire it in.

Companion: `scientific-model.md` (the ET model & references),
`developer_manual.md` (the `vwc_sensor` config option and dual-mode logic in
`sensor.py`).

> This document covers the **hardware**: which probe, where to bury it, how to
> wire it. What a probe's reading is then allowed to *mean* — and why a
> system-level probe cannot stand in for the whole garden — is a separate,
> currently unsettled question: see [`soil-moisture-model.md`](soil-moisture-model.md)
> (**Draft**, discussion [#126](https://github.com/never-dry/NeverDry/issues/126)).

---

## 0. Provenance & reproducibility

**Where the claims come from (and how to reproduce them).** Every quantitative
claim traces to a primary source listed in `scientific-model.md §7`:
- Power specs / accuracy / lifespan of professional sensors → manufacturer
  datasheets, refs **[21] TEROS 12** and **[22] TEROS 21** (linked there).
- Sensor-science claims (dielectric measurement, calibration, influence volume,
  drift) → peer-reviewed refs **[24]–[28]** (Topp 1980, Evett & Parkin 2005,
  Robinson 2008, Vaz 2013, Adeyemi 2017).
- Failure-mode numbers (θ_FC/θ_PWP example, drift timeline) are worked examples
  derived from those sources, not field measurements from this project.

A human can reproduce the analysis by opening the datasheets and papers via the
DOI/URL links in `scientific-model.md §7` and reading the cited figures directly. The
link-verification protocol (search engines + keywords + acceptance rule) is
documented in `scientific-model.md §0` and applies to this document's sources
too.

---

Adopting a soil moisture sensor eliminates the main limitation of the default
model — indirect deficit estimation via ET from temperature. With direct VWC
(Volumetric Water Content) measurement the deficit becomes **observable rather
than inferred**, reducing model uncertainty to the instrumental accuracy of the
sensor (≈2–3 % VWC). NeverDry supports this via the optional `vwc_sensor`
(see §9). The catch is that *most cheap sensors make things worse, not better* —
this document explains why.

## 1. Measurement technologies

| Technology | Quantity | Accuracy | Power mode | Maintenance |
|---|---|---|---|---|
| FDR/Capacitive (dielectric) | VWC [m³/m³] | ±3 % typ; ±2 % soil-specific | Burst 25 ms (SDI-12) or continuous (analog) | None |
| TDR | VWC [m³/m³] | ±2 % typ | Burst on command | None |
| Electronic matric potential (TEROS 21) | kPa | ±2 kPa | Burst 25 ms (SDI-12) | None |
| Classic hydraulic tensiometer | kPa | ±1 kPa | None (external reader) | Periodic (refill) |
| GMS (WATERMARK) | kPa | ±5–10 kPa | Passive; excitation ≈100 ms | None; >5 years |

## 2. Professionalism criteria & sensor tiers

A sensor is **professional** here if it satisfies three criteria: (1) documented
lifespan ≥5 years buried without replacement; (2) calibration drift verifiable
via a documented standard; (3) standard industrial interface (SDI-12, Modbus RTU).

- **Tier 1 — Dielectric FDR (direct VWC). METER TEROS 12**: reinforced epoxy
  body, stainless-steel needles resistant to salts, vendor-stated lifespan up to
  ~10 years (marketing claim, not a datasheet/warranty spec). Sensor-to-sensor
  calibration variability <1 % VWC. Influence volume ~1 L (1010 mL, vs typical
  200 mL). Accuracy ±0.03 m³/m³ (±3 %) generic, ±0.01–0.02 (±1–2 %) soil-specific.
- **Tier 2 — Matric potential. METER TEROS 21**: measures matric potential in
  kPa — agronomically more relevant than VWC because it measures the force roots
  must exert to extract water, independent of soil texture. SDI-12, ships
  calibrated, range **−9 to −100 000 kPa** (accurate band −9 to −100 kPa; not
  well calibrated beyond −100 kPa). Traditional tensiometers with an accurate
  transducer cost ~900 USD; TEROS 21 is the reduced-cost version.
- **Tier 3 — Granular Matrix Sensor. IRROMETER WATERMARK 200SS**: passive,
  zero idle consumption, pulse excitation during reading (~2 mA for ~100 ms),
  lifespan >5 years buried. Measures tension in centibars/kPa. Not suited for
  sandy soils or potting substrates (response too slow).
- **Tier 4 — Linear analog (residential). Vegetronix VH400**: FDR oscillator,
  0–3 V linear output, **continuous** draw <13 mA (vendor spec; no sleep/burst
  mode). Best cost/accuracy for non-critical use. (No official lifespan figure
  is published.)

### 2.1 TEROS 12 power budget (solar deployments)

```
Current during measurement (25 ms):  typical 3.6 mA, max 16.0 mA
Sleep current:                        0.03 mA      Supply: 4–15 VDC

Energy @ 1 measurement/min, 5 V:
E_day = (3.6 mA × 25 ms + 0.03 mA × 59.975 s) × 1440 ≈ 2720 mA·s/day
      = 2720 / 3600 ≈ 0.76 mAh/day ≈ 3.8 mWh/day   (sleep dominates: 0.03 mA × 24 h ≈ 0.72 mAh)
→ Comfortably compatible with a 1 W solar panel + 10 Wh battery.
```

### 2.2 Comparative table

| Sensor | Tech. | Output | Lifespan | Sleep [mA] | Measurement | Price EUR |
|---|---|---|---|---|---|---|
| METER TEROS 12 | FDR 70 MHz | VWC+T+EC SDI-12 | 10 years | 0.03 | 3.6 mA×25 ms | ~250 |
| METER TEROS 21 | Mat. pot. | kPa SDI-12 | 10 years | ~0.03 | ~3 mA×25 ms | ~320 |
| METER TEROS 32 | Tens. | kPa SDI-12 | 10 years | ~0.03 | ~3 mA×25 ms | ~450 |
| IRROMETER WM | GMS | 0–2.8 V (kPa) | >5 years | 0 | 2 mA×100 ms | ~30 |
| Vegetronix VH400 | FDR | 0–3 V (VWC) | n/a | <13 cont. | <13 mA cont. | ~30 |
| Capacitive DIY | Cap. | 0–3.3 V | <1 year | 2–5 cont. | 2–5 mA cont. | ~4 |

> Lifespan figures for the METER row are vendor "up to ~10 years" marketing
> claims, not warranty specs; prices are approximate (not checked vs live
> listings). See `evidence-and-methodology.md §2` for the verification.

> **TCO:** the sensor alone is only part of the cost. An ESP32 (~10 EUR) with
> ESPHome can read up to 10 SDI-12 sensors on a single bus.
> **Residential recommendation:** WATERMARK 200SS + ESP32 ADC (max lifespan/cost).
> **Agronomic/research recommendation:** METER TEROS 12 via SDI-12.
> **Power note:** professional SDI-12 sensors sleep between measurements (25 ms
> burst); analog sensors (VH400, DIY) draw continuously — relevant for
> battery/solar sizing.

## 3. Failure modes of low-cost sensors

Cheap stations (3–10 EUR capacitive sensors, entry-level controllers) typically
produce two opposite symptoms: soil always dry despite regular irrigation, or
root rot from excess water. Both stem from the same structural causes.

**Mode 1 — Absent/generic calibration.** Low-cost sensors ship with a fixed
linear curve (e.g. 0–3.3 V → 0–100 % "relative moisture") that matches no real
soil. The ADC-voltage↔VWC relationship depends on texture, bulk density,
temperature, and EC.

> **Quantitative example:** clay loam, θ_FC ≈ 0.32 m³/m³, θ_PWP ≈ 0.20 m³/m³
> (Saxton-Rawls pedotransfer values; see `evidence-and-methodology.md §3`).
> Uncalibrated sensor reads (illustratively) 45 % at field capacity and 30 % at
> wilting point (compressed & shifted scale).
> Threshold at 50 % → always irrigates, even at FC → **root rot**.
> Threshold at 25 % → never irrigates, even at PWP → **desiccation**.

**Mode 2 — Electrochemical drift over time.** Copper/tin electrodes exposed to
moist soil corrode by electrolysis within weeks/months, shifting the response
curve unpredictably. The corrosion phenomenon is documented (teardown tests;
in saline/fertilised soils sensors can become unreliable within one growing
season); the **specific phase boundaries below are an illustrative timeline,
not a measured one** (see `evidence-and-methodology.md §4`, E1):

| Phase | Effect on reading | Automation behavior |
|---|---|---|
| Weeks 1–4 (new) | Approximate but stable | Nominal if threshold calibrated |
| Months 1–3 (initial oxidation) | Reads lower VWC than actual | Over-irrigates → root rot risk |
| Months 3–12 (advanced oxidation) | Nearly insensitive; always low | Irrigates continuously; degraded data |
| >12 months (short circuit) | Stuck at min or max | Irrigation stuck or continuous; unusable |

The WATERMARK 200SS avoids this (granular matrix isolates the electrodes); the
TEROS 12 uses epoxy-coated stainless needles (10-year nominal lifespan).

**Mode 3 — Insufficient influence volume.** Low-cost sensors sense 20–50 mL
around the electrodes. Soil is heterogeneous at the centimeter scale (air
pockets, roots, stones distort the reading). The TEROS 12 averages over 1 L
(50× larger).

**Mode 4 — Shallow placement.** Cheap stations install at 5–10 cm for access;
the active root zone is 15–40 cm. Surface moisture is dominated by evaporation,
not root-available water — the sensor measures evaporation, not water stress.

### 3.1 Practical reliability hierarchy

This is why the ET-based model stays the primary option: a simplified physical
model with stable parameters is often more reliable in production than a sensor
that drifts unpredictably.

| Option | Long-term reliability | Failure causes |
|---|---|---|
| ET-based model | High — no physical drift | Systematic ET-model error, but **stable over time** (does not degrade); temperature ET still correlates strongly with Penman-Monteith — see `evidence-and-methodology.md §3` (L4) |
| WATERMARK + ESP32 ADC | High — no electrochemical drift | Slow response in sandy soils; initial kPa calibration |
| METER TEROS 12/21 | Very high — in-field verifiable drift | High cost; needle damage if poorly installed |
| Low-cost capacitive | Low — drift in 3–12 months | All failure modes above; not recommended for permanent automation |

## 4. Spatial coverage: how many m² per sensor

A sensor measures a **point**, not an area. The right question is not "how many
m² does it cover" but how much spatial variability exists and how much is
acceptable to ignore for the irrigation decision.

### 4.1 Influence volume

| Sensor | Influence volume | Approx. geometry |
|---|---|---|
| METER TEROS 12 | ~1 L | Sphere ⌀ ≈ 12 cm |
| IRROMETER WATERMARK | ~0.5 L | Cylinder ⌀ ≈ 8 cm |
| Vegetronix VH400 | ~50–100 mL | Layer ≈ 3 cm around electrodes |
| Capacitive DIY | ~20–50 mL | Layer ≈ 1–2 cm |

### 4.2 Spatial representativeness

| Context | Indicative coverage | Conditions |
|---|---|---|
| Homogeneous soil, uniform drip | 20–50 m² | Uniform texture, slope <5 %, evenly spaced emitters |
| Variable soil, sprinkler | 5–15 m² | VWC variability 30–50 % at 1–2 m distance |
| Pots, identical substrate & exposure | 3–5 pots/sensor | Same volume and drainage |
| Pots, different substrate/exposure | 1 sensor/pot | Different exposures → radically different ET |

### 4.3 Practical sizing criterion

```
N_sensors = ⌈ A / 30 ⌉
```

> ⚠️ **Heuristic, not a standard.** This per-area rule has no published basis,
> and authoritative guidance (UF/IFAS HS1222) explicitly *rejects* fixed
> per-area formulas in favour of variability-based placement (variogram range /
> management zones, ≥1 sensor per irrigation zone). Use `⌈A/30⌉` only as a rough
> starting point. See `evidence-and-methodology.md §4` (E4).

For A = 45 m²: N = 2 sensors, placed in the zones of most different solar
exposure (e.g. full sun vs partial shade). The second sensor is primarily a
**spatial-variability check**:

- Both read similar (Δ < 5 kPa for WATERMARK, Δ < 0.05 m³/m³ for VWC) → soil is
  homogeneous, a single sensor is representative.
- They systematically diverge beyond those thresholds → the zones have distinct
  hydrological dynamics and need independent control instances (two
  `DrynessIndexSensor` with separate parameters and valves).

### 4.4 Optimal placement

- **Depth**: in the active root zone, 15–25 cm for horticultural crops, 20–35 cm
  for shrubs. Never at the surface (<10 cm): evaporation-dominated.
- **Distance from emitters**: 15–25 cm from a drip emitter — intercept the
  wetting front without sitting in the post-irrigation saturated zone.
- **Orientation**: TEROS 12 horizontal or at 45° (minimize preferential flow
  along the body); WATERMARK vertical or oblique.
- **Soil contact**: the critical factor. Air gaps inject air's low permittivity
  (ε≈1 vs water ε≈80) and make FDR sensors under-read — even small gaps cause
  large errors (METER). The specific "2 mm → 10–20 % VWC" magnitude is an
  illustrative estimate (see `evidence-and-methodology.md §4`, E6). Seal the hole with mud from the same soil.

## 5. Validity of the ET-based model for residential gardens

On ~50 m² with reasonably uniform soil and drip irrigation, the homogeneity
assumption is defensible and the ET-based model is an operationally sufficient
approximation. Three converging reasons:

1. **It integrates the main sources of variability.** Rainfall is measured in
   real-time and subtracted without estimation. ET has systematic error but is
   *stable* — it does not drift over time like a cheap capacitive sensor. The
   deficit accumulates correctly during dry spells and resets at each rain or
   irrigation event; the error is not cumulative.
2. **Well-designed drip reduces spatial variability.** Evenly spaced emitters
   with uniform flow keep VWC variability low over a homogeneous plot; the index
   describes the root-zone average well. (The specific "<15 % over 50 m²" figure
   is an illustrative estimate — measured field CVs are often ~16–21 %; see
   `evidence-and-methodology.md §4`, E2.)
3. **Residual error is tolerable.** Demand is estimated to within ~20–40 %
   (ET from T only) → a few liters more or less per cycle. Acceptable for
   residential use, not cash crops. Root-rot/desiccation risk stays low because
   the error resets each cycle rather than accumulating.

**Edge case — heterogeneous solar exposure.** The one condition that breaks the
model over 50 m² is drastically different exposure (half full-shade, half
full-sun): actual ET differs by 2–3× and a single index is unrepresentative.
The correct fix is to **split into two independent zones**, *not* to add a soil
sensor.

> **Validity summary (50 m²):** homogeneous soil + uniform drip + similar
> exposure ⇒ ET model valid. Heterogeneous exposure ⇒ split into independent
> zones. The systematic model error is stable over time; it does not degrade
> like a cheap capacitive sensor and needs no periodic calibration.

## 6. Direct VWC measurement model

With known VWC, the deficit is computed directly relative to substrate field
capacity, without estimating ET:

```
D(t) = (θ_FC − θ(t)) · z_r · 1000   [mm]
```

θ_FC = field capacity [m³/m³] (typ. 0.25–0.35 for garden soil); θ(t) = measured
VWC; z_r = root depth [m] (typ. 0.20–0.40 for horticultural crops).

| Parameter | ET-based (default) | VWC-based |
|---|---|---|
| Observed | T, P | VWC, P |
| Inferred | ET, deficit | Deficit (direct) |
| Calibrated | α, T_base, threshold | θ_FC, z_r |
| Main uncertainty | ET model (systematic; Hargreaves RMSE ~0.8–1 mm/day vs PM) | Sensor accuracy (±2–3 % VWC) |
| Weather sensitivity | High (wind, RH not observed) | None |
| Hardware cost | 0 EUR | 30–250 EUR + gateway |

## 7. ESPHome → MQTT → Home Assistant bridge

### 7.1 ESPHome — VH400 (analog)

```yaml
esphome:
  name: soil-moisture-node
esp32:
  board: esp32dev
mqtt:
  broker: 192.168.1.10
  topic_prefix: homeassistant/soil
sensor:
  - platform: adc
    pin: GPIO34
    name: Soil VWC
    unit_of_measurement: m3/m3
    accuracy_decimals: 3
    update_interval: 60s
    attenuation: 11db
    filters:
      - calibrate_linear:
          - 1.10 -> 0.000   # voltage in air
          - 2.30 -> 0.600   # voltage at saturation
      - clamp:
          min_value: 0.0
          max_value: 0.65
```

### 7.2 ESPHome — TEROS 12 / 5TM (SDI-12)

```yaml
external_components:
  - source: github://nrandell/esphome-sdi12
sdi12:
  uart_id: uart_bus
  update_interval: 60s
uart:
  id: uart_bus
  tx_pin: GPIO17
  rx_pin: GPIO16
  baud_rate: 1200
sensor:
  - platform: sdi12
    address: 0
    index: 0
    name: Soil VWC TEROS
    unit_of_measurement: m3/m3
  - platform: sdi12
    address: 0
    index: 1
    name: Soil Temperature
    unit_of_measurement: "C"
  - platform: sdi12
    address: 0
    index: 2
    name: Soil EC
    unit_of_measurement: dS/m
```

### 7.3 Home Assistant — MQTT sensor

```yaml
mqtt:
  sensor:
    - name: Soil VWC
      state_topic: homeassistant/soil/sensor/soil_vwc/state
      unit_of_measurement: m3/m3
      device_class: moisture
      state_class: measurement
      value_template: "{{ value | float | round(3) }}"
```

## 8. Integration into DrynessIndexSensor

The dual-mode logic lives in `sensor.py`: if `vwc_sensor` is set in the config,
NeverDry uses the **direct VWC model**; otherwise it falls back to the ET-based
model (backward compatible).

> In VWC-based mode the post-irrigation reset becomes superfluous — the deficit
> updates automatically as the sensor detects the VWC increase. The explicit
> reset remains available as a fallback if the sensor goes offline.

---

## Revision history

| Date | Change |
|---|---|
| 2026-04 | Initial — sensor analysis (v0.1.0). |
| 2026-06 | Restructured as a standalone note; claims verified against primary sources. |
