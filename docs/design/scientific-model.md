# Scientific Model & References

The agronomic and mathematical foundation of NeverDry: the water-balance
model, its discretization, calibration and limitations, and the scientific
bibliography. The model equations are version-independent and remain current.

Companion docs: `developer_manual.md` (code-level formulas as implemented),
`soil-sensors.md` (sensor reliability & the ET-vs-VWC argument),
`unit-system.md` (metric-internal architecture).

Model status (2026-08-16): **all three ET tiers are implemented and selectable**,
plus a soil-probe model. Linear `α(T−T_base)`, Hargreaves-Samani and
Penman-Monteith FAO-56 all run; which one a site gets follows from the sensors
it declares (see §6.1). Two inputs the equations need are **derived rather than
required**: the daily temperature extremes, observed from the thermometer, and
the net radiation, computed from solar radiation — or estimated from the diurnal
range when there is no pyranometer.

---

## 0. Provenance & reproducibility

This section makes the document auditable: it states where each part comes from
and how a human can reproduce the bibliographic verification **from primary
sources**.

Primary authority for every claim is the cited reference in §7.

**Bibliographic links (§7).** Added 2026-06-30 by web research. Protocol,
reproducible by hand:

1. **Tools used:** the harness `WebSearch` (general web search) for discovery and
   `WebFetch` (direct page retrieval) for confirmation. No proprietary citation
   database was assumed.
2. **Query pattern per reference:** `<first-author surname> <year> <title fragment> <journal>`
   (e.g. `Saxton Rawls 2006 soil water characteristic estimates texture`).
3. **Acceptance rule (no fabrication):** a DOI/URL was recorded **only** if the
   resolved page's *title + authors + year + volume/pages* matched the citation.
   Any result that did not match was discarded; no DOI was invented or guessed.
4. **Verification sources actually reached:** doi.org and Crossref, plus the
   publisher domains — Wiley/AGU, Elsevier ScienceDirect, IEEE Xplore, Springer,
   Oxford Academic, ASCE Library / ASCE CEDB, ASABE eLibrary, MDPI, JSTOR,
   Annual Reviews, Royal Society — and `fao.org`, `metergroup.com`, PubMed for
   items without a DOI.
5. **No-DOI items:** FAO/ASCE reports, datasheets and web docs have no registered
   DOI; they link to the official landing page instead. One item (ref 23, Cobos &
   Chambers 2010) has no official page — only third-party mirrors — and is left
   unlinked rather than cite a mirror as authoritative.

**To reproduce independently:** run the query pattern (step 2) for any reference
on Crossref search, Google Scholar, or the publisher site; open the result; check
the four metadata fields (step 3) before trusting the DOI. Confirm the DOI from
the source; never invent it.

**Honest caveat:** the verbatim search queries issued during the session are not
preserved in the session log; the query pattern above is the reproducible
template that was followed, not a transcript of each individual lookup.

---

## 1. FAO-56 water balance

The model is a simplification of the standard FAO-56 water balance
(Allen et al., 1998 [1]) — the de facto standard for agronomic irrigation
management:

```
D(t) = D(t-1) + ET₀(t)·Kc - P(t) - I(t)
```

where `D(t)` = cumulative water deficit [mm]; `ET₀(t)` = reference
evapotranspiration [mm/day]; `Kc` = crop coefficient (FAO-56, Tab. 12);
`P(t)` = precipitation [mm]; `I(t)` = irrigation delivered [mm].

The implementation absorbs `Kc` into the parameter `α` and replaces `I(t)`
with an exogenous reset at the irrigation event, reducing the degrees of
freedom to **two calibratable parameters** (`α`, `T_base`).

## 2. Evapotranspiration estimation

ET-estimate quality is the main limiting factor. Options, by increasing
accuracy:

| Formula | Required inputs | RMSE vs PM | Applicability |
|---|---|---|---|
| Linear `α(T−T_b)` | `T_mean` | ~2–3 mm/day | Residential automation, temperate climate **(implemented)** |
| Hargreaves-Samani | `T_max`, `T_min`, lat. | ~1 mm/day | **Implemented.** Needs only a thermometer: the extremes are observed over a rolling 24 h, latitude comes from the site |
| Penman-Monteith FAO-56 | `T`, RH, `u₂`, `R_s` | <0.5 mm/day | **Implemented.** Requires humidity and wind; `R_s` improves it but is not required — estimated from the diurnal range when absent (eq. 50) |

### 6.1 What is required, what is derived

The distinction matters because it decides who can run which tier, and it is the
reason two of these need no instrument beyond a thermometer.

| Quantity | Source |
|---|---|
| Daily `T_max` / `T_min` | **Derived.** Observed from the configured thermometer over a rolling 24 h window in hourly buckets. Never asked for: the same entity in both fields would give a zero range and an evapotranspiration of exactly zero |
| Extraterrestrial radiation `R_a` | **Derived.** Latitude and day of year (FAO-56 eq. 21) — astronomy, not a sensor |
| Solar radiation `R_s` | **Measured** when a pyranometer is declared, accumulated over 24 h as *energy* (a pyranometer reports power, and scaling an instantaneous flux to a day understates it several-fold). **Estimated** from the diurnal range otherwise (eq. 50) |
| Net radiation `R_n` | **Derived**, always. Shortwave `(1−0.23)·R_s` minus the longwave term from `T_max`, `T_min`, vapour pressure and the `R_s/R_so` ratio (eq. 38–40). Measuring it directly needs a four-sensor net radiometer |
| Wind `u₂` | **Measured**, converted from the anemometer height to 2 m (eq. 47). Left unconverted, a mast reading inflates the aerodynamic term systematically |

A site's method is chosen by capability match — `declared sensors ⊇ required
sensors` — with one rule decided by measurement rather than by the sensor list:
if the observed diurnal range is implausibly small over a full day, the
thermometer is sheltered, and the tiers reading that range are withdrawn from
the *automatic* choice (never from an explicit one).

Temperature-only ET is less precise than Penman-Monteith but still tracks it
well: Hargreaves-type estimates reach RMSE ≈ 0.8–1 mm/day with R² ≈ 0.9 vs
Penman-Monteith (Droogers & Allen 2002 [11]). The even simpler linear
`α(T−T_base)` model used here is less accurate, but no specific error band for
it is established in the literature. Acceptable for residential automation,
insufficient for professional crop management. (An earlier "40–60 % of variance"
figure was not supported by the cited sources — see `evidence-and-methodology.md §3`, L4.)

## 3. Temporal discretization

Continuous model:

```
D(t) = max(0, ∫₀ᵗ [ET(τ) - P(τ)] dτ)
```

The implementation uses a **forward Euler approximation with variable Δt**
(event-driven, not fixed-step), integrating the increment at each sensor
state change:

```
D(t) = max(0, D(t₋₁) + ET_h·Δt_h - P_event)
ET_h = α(T - T_base)/24   [mm/h]
```

Real-time integration eliminates daily temporal aliasing and enables correct
handling of short, intense rain showers.

### 3.1 Precipitation delta

The raw rain sensor value is **never** subtracted directly from the deficit.
A **delta** (increment since last reading) is computed instead, to prevent
double-counting when non-rain events (e.g. temperature changes) trigger a
recalculation.

| Sensor type | Delta computation |
|---|---|
| `event` (default) | The sensor value **is** the delta (mm per event, tipping bucket). A new value ≠ previous = new rain event; same value on repeated reads = `ΔP = 0`. |
| `daily_total` | Cumulative mm since midnight. `ΔP = P_now − P_last`. If `P_now < P_last` (midnight rollover), then `ΔP = P_now` (fresh accumulation from zero). |

**Rationale:** without delta computation, a cumulative sensor reporting
"5.0 mm today" would subtract 5.0 mm on every temperature change event —
draining the deficit to zero in minutes. With delta logic, only the *actual
new rain since the last reading* is subtracted.

## 4. State equation (implemented)

Continuous update, event-driven, per-zone with crop coefficient:

```
ET_h(t)      = max(0, α·(T(t) - T_base)/24)                          [mm/h]
ΔP(t)        = rain_delta(sensor_type, P_now, P_last)                [mm]
D_zone(t)    = clip(D_zone(t₋₁) + ET_h·Kc·Δt_h - ΔP, 0, D_max)       [mm]
```

Each zone tracks its own deficit scaled by a crop coefficient `Kc` that
varies seasonally with the plant family. The reference sensor (`Kc=1.0`)
also tracks a global deficit for display. Irrigation reset:
`D_zone(t) ← 0` after the zone valve closes.

### 4.1 Volume and duration

The deficit in mm equals L/m² by the dimensional identity `1 mm = 1 L/m²`:

```
V = D · A · η⁻¹          [L]
t_irr = V / Q            [min]
```

`A` = area [m²], `η` = distribution efficiency, `Q` = flow rate [L/min].

| System type | Typical η |
|---|---|
| Drip irrigation | 0.90 – 0.95 |
| Micro-sprinklers | 0.75 – 0.85 |
| Pop-up sprinklers | 0.60 – 0.75 |
| Manual / hose | 0.50 – 0.70 |

### 4.2 Site exposure (microclimate factor)

The per-zone `Kc` above is the product of a planting term and a site term,
following the landscape coefficient method of Costello et al. (2000) [38]:

```
K_L      = k_s · k_d · k_mc
Kc_zone  = Kc(doy, plant_family | manual override) · k_mc
```

`k_s` (species) is what the plant-family seasonal profile represents; `k_mc`
(microclimate) is the zone's site exposure — shade, wind, or heat radiated
back from paving and walls. `k_d` (density) is not modelled: residential
zones are configured as whole irrigated areas, so canopy density is already
folded into the family profile.

The factor multiplies the planting term rather than replacing it, which is
the point of the parameter: a constant `Kc` freezes a shaded zone at one
season's value, and the resulting error is largest in autumn (a lawn shaded
from 14:00 drifts ~33% high by mid-October), precisely when over-watering
shaded turf promotes disease.

`k_mc` presets span 0.60 (deep shade) to 1.20 (reflected heat), with 1.00
for the open, reference-like condition. The source literature gives
0.5 – 0.9 for shaded and wind-sheltered sites and values above 1.0 for
paved or wall-adjacent sites, where advective heating pushes a planting past
reference ET. The specific preset values are **expert judgment, not
validated measurements** (see §6.2).

In VWC mode the zone deficit is `D_zone = D_ref · Kc_zone`, so `k_mc` rides
along with `Kc` there too. The reference probe sits in one spot, not in each
zone, so scaling it per zone is the existing premise of that mode rather than
something the factor introduces. `Kc_zone` combines multiplicatively with a
manual override, so a high override and an above-1.0 exposure do multiply out
(2.0 × 1.20 = 2.4); this is deliberately not capped, since capping would
silently contradict two explicit user choices.

## 5. Scheduling modes

| Aspect | Mode A — Threshold | Mode B — Daily deficit-based |
|---|---|---|
| Trigger | Reactive: index exceeds threshold | Proactive: fixed time (e.g. 23:00) |
| Frequency | Variable | Fixed (every night with `D > D_min`) |
| Volume | Proportional to deficit at trigger | Proportional to daily deficit |
| Rainy day | Explicit condition on rainfall | Automatic: `D ≈ 0` if it rains |
| Over-irrigation risk | Possible with too low a threshold | Minimal |
| Suited for | Tolerant crops, good retention | Sensitive crops, pots, open ground |
| Composition | Safety net for ET peaks | Primary nightly scheduler |

## 6. Calibration & known limitations

### 6.1 Parameter calibration

| Parameter | Strategy |
|---|---|
| `α`, `T_base` | Empirical: record apparent deficit + visual plant response; adjust `α` weekly. |
| threshold (Mode A) | 15–25 mm potted plants; 25–40 mm open ground. Irrelevant in pure Mode B. |
| `D_min` (Mode B) | Default 1 mm. Increase to 3–5 mm to reduce short cycles for tolerant crops. |
| scheduling_time (Mode B) | Default 23:00. Shift to 23:55 for precise midnight deficit. |
| `flow_rate_lpm` | Direct measurement: graduated container + stopwatch. |
| efficiency `η` | Direct measurement with uniformly distributed collectors. |

### 6.2 Known limitations

**ET model** — temperature-only ET is systematically less precise than
Penman-Monteith (it ignores wind and humidity): it overestimates on humid/cold
days and underestimates on windy/sunny days. The bias is *stable*, not
cumulative. (See `evidence-and-methodology.md §3`, L4, for the quantitative basis.) Instantaneous
temperature is not the Δt-window average;
for slow sensors (>15 min/reading) consider a moving average via HA's
`statistics` platform.

**Hydrological model** — does not include deep percolation, surface runoff,
or substrate field capacity. Volume estimate assumes uniform distribution
over area `A`; use a separate zone per distinct area.

**Microclimate factor** — the exposure presets (§4.2) are expert-judgment
values in the range the landscape coefficient method describes, not
site-measured coefficients, and they are *static*: a shade fraction is
itself seasonal, since a low autumn sun throws a longer, earlier shadow. A
template sensor driven by `sun.sun` would make the factor self-correcting;
that is deliberately left as a follow-up to the static presets.

**Rain sensor** — wrong `rain_sensor_type` causes large errors: `event` on a
cumulative sensor subtracts the full daily total each update; `daily_total`
on an event sensor misses all but the first of a run of equal values. Rain
sensors slower than the temperature sensor discover rain late; the extra ET
accumulated meanwhile is typically <0.05 mm (negligible).

**Mode A** — too low a threshold vs flow rate causes overly frequent on-off
cycling in summer. Set `threshold ≥ average daily ET`.

**Mode B** — `duration_s` is read at trigger (23:00); the last 60 min of ET
is excluded (error <0.1 mm). If HA restarts between trigger and completion,
the reset is skipped and the residual is re-irrigated the next night
(mitigate with a recovery automation on `homeassistant_started`). Late rain
(after 23:00) arrives mid-irrigation; avoiding it needs a real-time rain
sensor as an action condition.

---

## 7. References

38 citations. Each entry carries a
DOI link where one exists, otherwise the official landing-page URL; see §0 for
how these links were obtained and verified. Items without a registered DOI
(FAO/ASCE reports, datasheets, web docs) link to their canonical page instead.

### Water balance and irrigation
1. Allen R.G., Pereira L.S., Raes D., Smith M. (1998). *Crop evapotranspiration: guidelines for computing crop water requirements.* FAO Irrigation and Drainage Paper 56. Rome: FAO. ISBN 92-5-104219-5. — https://www.fao.org/4/x0490e/x0490e00.htm
2. Doorenbos J., Pruitt W.O. (1977). *Guidelines for predicting crop water requirements.* FAO Irrigation and Drainage Paper 24, 2nd ed. Rome: FAO. — https://www.fao.org/4/f2430e/f2430e.pdf
3. Jensen M.E., Burman R.D., Allen R.G. (eds.) (1990). *Evapotranspiration and Irrigation Water Requirements.* ASCE Manuals No. 70. New York: ASCE. — https://cedb.asce.org/CEDBsearch/record.jsp?dockey=0067841
4. Pereira L.S., Oweis T., Zairi A. (2002). Irrigation management under water scarcity. *Agricultural Water Management*, 57(3), 175–206. — https://doi.org/10.1016/S0378-3774(02)00075-6

### Evapotranspiration: models and validation
5. Penman H.L. (1948). Natural evaporation from open water, bare soil and grass. *Proc. R. Soc. London A*, 193(1032), 120–145. — https://doi.org/10.1098/rspa.1948.0037
6. Monteith J.L. (1965). Evaporation and environment. *Symp. Soc. Exp. Biol.*, 19, 205–234. — https://pubmed.ncbi.nlm.nih.gov/5321565/
7. Hargreaves G.H., Samani Z.A. (1985). Reference crop evapotranspiration from temperature. *Applied Engineering in Agriculture*, 1(2), 96–99. — https://doi.org/10.13031/2013.26773
8. Hargreaves G.H., Allen R.G. (2003). History and evaluation of Hargreaves evapotranspiration equation. *J. Irrig. Drain. Eng.*, 129(1), 53–63. — <https://doi.org/10.1061/(ASCE)0733-9437(2003)129:1(53)>
9. Thornthwaite C.W. (1948). An approach toward a rational classification of climate. *Geographical Review*, 38(1), 55–94. — https://doi.org/10.2307/210739
10. Trajkovic S. (2007). Hargreaves versus Penman-Monteith under humid conditions. *J. Irrig. Drain. Eng.*, 133(1), 38–42. — <https://doi.org/10.1061/(ASCE)0733-9437(2007)133:1(38)>
11. Droogers P., Allen R.G. (2002). Estimating reference evapotranspiration under inaccurate data conditions. *Irrigation and Drainage Systems*, 16(1), 33–45. — https://doi.org/10.1023/A:1015508322413

### Water deficit and crop stress
12. Steduto P., Hsiao T.C., Fereres E., Raes D. (2012). *Crop yield response to water.* FAO Irrigation and Drainage Paper 66. Rome: FAO. — https://www.fao.org/4/i2800e/i2800e00.htm
13. Hsiao T.C. (1973). Plant responses to water stress. *Annu. Rev. Plant Physiol.*, 24, 519–570. — https://doi.org/10.1146/annurev.pp.24.060173.002511
14. Fereres E., Soriano M.A. (2007). Deficit irrigation for reducing agricultural water use. *J. Exp. Bot.*, 58(2), 147–159. — https://doi.org/10.1093/jxb/erl165

### Soil water balance models
15. Ritchie J.T. (1972). Model for predicting evaporation from a row crop with incomplete cover. *Water Resour. Res.*, 8(5), 1204–1213. — https://doi.org/10.1029/WR008i005p01204
16. Saxton K.E., Rawls W.J. (2006). Soil water characteristic estimates by texture and organic matter. *Soil Sci. Soc. Am. J.*, 70(5), 1569–1578. — https://doi.org/10.2136/sssaj2005.0117
17. Raes D., Steduto P., Hsiao T.C., Fereres E. (2009). AquaCrop — The FAO crop model to simulate yield response to water (Part II: Main algorithms and software). *Agronomy J.*, 101(3), 438–447. — https://doi.org/10.2134/agronj2008.0140s

### Automation and IoT for irrigation
18. Srbinovska M. et al. (2015). Environmental parameters monitoring in precision agriculture using WSN. *J. Cleaner Production*, 88, 297–307. — https://doi.org/10.1016/j.jclepro.2014.04.036
19. Gutierrez J. et al. (2014). Automated irrigation system using a wireless sensor network and GPRS module. *IEEE Trans. Instrum. Meas.*, 63(1), 166–176. — https://doi.org/10.1109/TIM.2013.2276487
20. Goap A. et al. (2018). An IoT based smart irrigation management system using machine learning. *Comput. Electron. Agric.*, 155, 41–49. — https://doi.org/10.1016/j.compag.2018.09.040

### Soil moisture sensors
21. METER Group (2020). *TEROS 12 Soil Moisture, Temperature, and Electrical Conductivity Sensor — Operator Manual.* Pullman, WA: METER Group Inc. — https://metergroup.com/products/teros-12/
22. METER Group (2021). *TEROS 21 Matric Potential Sensor — Operator Manual.* Pullman, WA: METER Group Inc. — https://metergroup.com/products/teros-21/
23. Cobos D.R., Chambers C. (2010). Calibrating ECH2O soil moisture sensors. *Application Note.* Decagon Devices, Inc. — *(no official landing page; available via third-party mirrors only)*
24. Evett S.R., Parkin G.W. (2005). Advances in soil water content sensing. *Vadose Zone J.*, 4(4), 986–991. — https://doi.org/10.2136/vzj2005.0099
25. Vaz C.M.P. et al. (2013). Evaluation of standard calibration functions for eight electromagnetic soil moisture sensors. *Vadose Zone J.*, 12(2). — https://doi.org/10.2136/vzj2012.0160
26. Robinson D.A. et al. (2008). Soil moisture measurement for ecological and hydrological observatories: a review. *Vadose Zone J.*, 7(1), 358–389. — https://doi.org/10.2136/vzj2007.0143
27. Topp G.C., Davis J.L., Annan A.P. (1980). Electromagnetic determination of soil water content. *Water Resour. Res.*, 16(3), 574–582. — https://doi.org/10.1029/WR016i003p00574
28. Adeyemi O. et al. (2017). Advanced monitoring and management systems for improving sustainability in precision irrigation. *Sustainability*, 9(3), 353. — https://doi.org/10.3390/su9030353
29. ESPHome Documentation — ADC Sensor. https://esphome.io/components/sensor/adc.html
30. ESPHome SDI-12 external component (nrandell). https://github.com/nrandell/esphome-sdi12
31. Vegetronix Inc. VH400 Soil Moisture Sensor — Datasheet. https://www.vegetronix.com/Products/VH400

### Home Assistant — technical documentation
32. HA Developer Documentation — Architecture overview. https://developers.home-assistant.io/docs/architecture_index
33. HA Developer Documentation — Creating a custom component. https://developers.home-assistant.io/docs/creating_component_index
34. HA Developer Documentation — Entity base class and RestoreEntity. https://developers.home-assistant.io/docs/core/entity/
35. HA Developer Documentation — Config flow. https://developers.home-assistant.io/docs/config_entries_config_flow_handler
36. HA Developer Documentation — SensorEntity. https://developers.home-assistant.io/docs/core/entity/sensor
37. HACS Integration publishing guide. https://hacs.xyz/docs/publish/integration

### Landscape coefficients
38. Costello L.R., Matheny N.P., Clark J.R., Jones K.S. (2000). *A Guide to Estimating Irrigation Water Needs of Landscape Plantings in California: The Landscape Coefficient Method and WUCOLS III.* University of California Cooperative Extension & California Department of Water Resources. — https://cimis.water.ca.gov/Content/PDF/wucols00.pdf — **link verification caveat:** title/authors/year/publisher confirmed against corroborating institutional pages (UC/CalWEP), not against the PDF body, which the retrieval tooling could not render as text. Treat as landing-page-level verification (§0 step 5), pending a reader check of Part 1 §2 for the `K_L = k_s · k_d · k_mc` definition and the `k_mc` table.

---

## Revision history

| Date | Change |
|---|---|
| 2026-04 | Initial — model specification (v0.1.0). |
| 2026-06 | Restructured as a standalone note; references verified against primary sources. |
| 2026-08 | Added §4.2 site exposure (microclimate factor, #146), its limitation in §6.2, and ref 38. |
