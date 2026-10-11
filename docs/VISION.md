# NeverDry — Vision

*Foundational intent behind every design decision.*

> This Vision is a **living document**, open to the community. If you see it
> differently — or think it should say more — propose a change via an issue or
> pull request. The project is shaped in the open.

---

## What NeverDry is for

**Carry the expertise, so the user does not have to.**
Watering well takes agronomy, hydraulics and a little electrical engineering. NeverDry holds that knowledge in its model and asks only what a person can see: their plants, their valves, their garden. It is for people who know none of this and still expect more than a timer. Whoever wants to look under the bonnet finds every setting there, and every one of them is optional.

**Get running immediately with what you already have.**
NeverDry integrates into an existing Home Assistant setup without requiring new hardware. If you have a temperature sensor and a rain gauge, you can start today. Every additional sensor — weather station, soil moisture probe, flow meter — improves the model; none of them are prerequisites.

**Deliver exactly the water the soil needs — no more, no less.**
The system computes the cumulative soil water deficit and closes valves the moment that deficit is repaid. Plants receive what they need to thrive; excess water is never delivered. Conservation and health are the same goal.

**Make every irrigation decision visible and auditable.**
The system is never silent. Every action — irrigate, skip, postpone, abort — is logged and surfaced in the Home Assistant dashboard. If the garden was not watered, there is a reason, and that reason is readable. Invisible failures are not acceptable.

**Give the user complete freedom of control.**
Manual override, time-based schedule, and fully automatic deficit-driven mode coexist and can be combined zone by zone. Switching modes does not reset the water-balance model or erase history. The user is never forced into a single operating mode; the system adapts to how they want to interact with it on any given day.

**Act at the right moment, driven by plant biology — not by a clock.**
Irrigation happens when the soil deficit crosses the threshold the user has set, accounting for local climate, sun exposure, and plant type. There is no arbitrary fixed-time watering. The system reasons about when water is actually needed and acts accordingly.

**Notify immediately when a valve cannot be commanded.**
If a valve does not respond to an open or close command after the configured number of retries, the system raises a persistent notification and flags the zone as degraded. A stuck-open valve wastes water and can flood plants; a stuck-closed valve means the garden goes unwatered. Neither failure is acceptable silently. The user is always informed so they can act.

---

## The domains NeverDry covers

A timer knows one thing: the clock. NeverDry works across several domains at once, so the person watering does not have to.

| Domain | What NeverDry knows for you | What it asks you | Optional, under the bonnet |
|---|---|---|---|
| **Agronomy** | Evapotranspiration, crop coefficient, the soil's water reserve, the deficit, useful rain | Which plants, and where they stand | Manual crop coefficient, custom soil, soil moisture probe, evapotranspiration method |
| **Hydraulics** | Volume from flow and time, serial or parallel runs, the master valve or pump and its linger, shared water lines | The valve, and its flow rate (or how long you usually water) | A water network the zones draw from, a flow or line limit, a master, a water meter |
| **Electrical** (a little) | How many solenoids a board (transformer) can hold open, confirmed commands, a valve stuck open | Nothing | A valve coordinator the zones are counted on, and its limit |
| **Climate and place** | Outdoor, greenhouse or indoor; frost; sun exposure | Where the garden is | A second environment (a greenhouse with its own sensors), exposure, additional sensors |
| **Reliability** | Watchdog, learned latency, reachability, valve self-test | Nothing | Thresholds and timings |

The third column is the whole configuration of a simple garden. A terrace with seven pots, or four zones on one water line, is set up from that column alone and never meets the fourth.

---

## What this means in practice

| Principle | Consequence for design |
|---|---|
| Carried expertise | The form asks what a person can see; every domain setting is optional and defaults to a working value |
| Instant adoption | Zero required sensors beyond a temperature source; progressive enhancement only |
| Precision delivery | Deficit-based volume calculation; no fixed-duration watering without a model reason |
| Visible operation | All decisions logged; activity log downloadable; diagnostic button in UI |
| Freedom of control | Manual / scheduled / automatic modes composable, not mutually exclusive |
| Right moment | Deficit threshold triggers action; no cron-style fixed-time watering as default |
| Valve safety | Retry on command failure; persistent notification + zone degraded flag if unresolved |

---

*This document captures intent, not implementation. Architecture and algorithms
are in [`docs/design/`](design/README.md).*

---

## Revision history

| Date | Change |
|---|---|
| 2026-04 | Initial vision (v0.1.0) — present since the project's inception. |
| 2026-10 | Added the principle of carried expertise and the domains NeverDry covers: agronomy, hydraulics, electrical, climate and place, reliability. |
