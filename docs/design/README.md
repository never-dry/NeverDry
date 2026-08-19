# NeverDry — Design & Engineering Notes

Developer-facing design notes for NeverDry: how it works, why it's built that
way, and how its claims are verified. Read these to make a meaningful change.
New contributors: see [`../../CONTRIBUTING.md`](../../CONTRIBUTING.md) first.

> These are working engineering documents, not user docs. End-user guides live in
> [`../user_manual.md`](../user_manual.md) and [`../developer_manual.md`](../developer_manual.md).

## Reading order

**1. Architecture — how it works**
- [`../hardware-interface.md`](../hardware-interface.md) — **Accepted (ADR)**: the
  boundary. NeverDry consumes Home Assistant entities and never speaks to hardware —
  no MQTT, no Zigbee, no vendor APIs. Read this first: it decides which proposals
  are in scope at all, and it explains why a capability the hardware has does not
  oblige the integration to reach it.
- [`valve-state-machine.md`](valve-state-machine.md) — per-valve finite state
  machine and the `ValveOperator` (open/close, verification, failure handling).
- [`controller-reliability.md`](controller-reliability.md) — the controller layer
  above the FSM: hardening applied and the invariants to preserve.
- [`valve-reachability.md`](valve-reachability.md) — **Accepted (ADR)**: noticing
  a valve that has stopped answering — a dead battery, or a device off the mesh —
  when every direct signal says it is fine. Judges each valve against its
  siblings rather than against a clock, with the estimators that were measured
  and rejected, and the field case of 2026-08-18 that confirmed it.
- [`flow-rate-provenance.md`](flow-rate-provenance.md) — **Accepted (ADR)**: the
  three flow rates (design, telemetered, historical), which one answers which
  question, and why a still meter may qualify an action but never refuse one.
  Read before touching flow verification, leak detection or delivery planning.
- [`unit-system.md`](unit-system.md) — metric-internal architecture (SI core,
  imperial only at the edges).
- [`dependency-management.md`](dependency-management.md) — **Accepted (ADR)**: why
  NeverDry uses pip + `manifest.json`, not `uv`.
- [`preset-and-override.md`](preset-and-override.md) — **Accepted (ADR)**: one rule
  for the three preset/override pairs (system type, plant family, exposure) —
  the dropdown decides — plus the migration that keeps existing zones watering
  the same way, and the three form sections.

**2. Direction (open for input)**
- [`actuator-abstraction.md`](actuator-abstraction.md) — **partly implemented**:
  the command adapter and `valve.*` support shipped 2026-08-17 (steps 1–2); the
  orchestration questions (soak, master pump, unified scheduler) are still open.
  Discussion: [#74](https://github.com/never-dry/NeverDry/issues/74).
- [`soil-moisture-model.md`](soil-moisture-model.md) — **Draft**: what a soil
  probe's reading is allowed to mean. Argues that "site-level" is not a physical
  category for soil, and documents a suspected defect in how the per-zone deficit
  is derived in VWC mode. Discussion:
  [#126](https://github.com/never-dry/NeverDry/issues/126).
  *Status: Draft → Proposed (RFC) → Accepted (ADR).*

**3. The science**
- [`scientific-model.md`](scientific-model.md) — the ET water-balance model,
  derivations, calibration, and the full bibliography.
- [`soil-sensors.md`](soil-sensors.md) — soil-moisture sensor reliability and the
  argument for the ET-based model over low-cost sensors.
- [`evidence-and-methodology.md`](evidence-and-methodology.md) — how the model's
  claims are verified against primary sources (reproducible protocol + evidence
  table). **Good first contribution:** help close the residual claim review.

**4. Testing**
- [`field-test-checklist.md`](field-test-checklist.md) — manual field-test suite
  for hardware validation.

## Document status convention

These notes use a single `Status` field with a lifecycle — *RFC* and *ADR* are
phases of the same document, not separate types:

```
Draft → Proposed (open for comment, "RFC") → Accepted ("ADR")
```

A note is never marked `Accepted` while the decision is still open for input.
