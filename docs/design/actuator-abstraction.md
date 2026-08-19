# Actuator Abstraction & Orchestration — Design Note

A **proposed** direction for **where actuator control and irrigation
orchestration belong in the layering**, and the discipline to follow when
extending them. It complements `valve-state-machine.md` (per-valve FSM /
`ValveOperator`) and `controller-reliability.md` (controller hardening).

**Status: partly implemented (2026-08-17).** Steps 1 and 2 of the sequencing
below are **shipped**: `ValveCommandAdapter` exists in `driver.py`, every command
and state site in `controller.py` routes through it (twelve of them), and the
config-flow selector offers `["switch", "valve"]` on both the create and the edit
form. Steps 3–5 remain a proposal. The layering question in §2 — one unified
scheduler or incremental controller capabilities — is **not** settled by this;
what shipped is the floor, which was always the uncontroversial part.
Lifecycle: `Draft → Proposed (open for comment, "RFC") → Accepted ("ADR")`.
Promote to *Proposed* when opened on GH #74 for community input; promote to
*Accepted* once the decision is settled. Implementation is separately gated.

Related: GH #74 (the actuator-abstraction proposal — primary discussion thread),
GH #94 (valve entities), GH #95 (master pump).

---

## 1. Context — what triggered this

Two community requests and one prior proposal converged in the same week:

- **GH #94** — a user with an Orbit B-hyve timer cannot select their valve:
  it is exposed as a `valve.*` entity, but the config flow only lists
  `switch.*` entities. **Resolved 2026-08-17** — see the status note above.
- **GH #95** — a user with a rainwater-tank pump wants a **master pump**:
  ON whenever any zone runs, OFF (with a short linger) when none do.
- **GH #74** — Filip proposed, in general terms, abstracting *how NeverDry
  drives the actuator* (including a possible Hydrawise adapter).

The question raised: do these converge into a single **scheduler** that
manages pump + soaking + valve abstraction?

---

## 2. Proposed direction — separate the layers; do not merge them

The three requests do **not** live at the same layer. Conflating them —
in particular folding the valve abstraction into a scheduler — repeats the
exact layering mistake the codebase already avoids (the per-valve FSM is
deliberately domain-agnostic; the domain knowledge must sit *below* it,
not be embedded in a higher component).

The correct mental model is a **stack**, not a merge:

```
   Scheduler / Orchestrator          ← cross-zone timeline: soak, master pump,
        │                              ordering/queue  (FUTURE concept, the deferred scheduler)
        │
   IrrigationController               ← sequential irrigation (exists today);
        │   ├── ValveOperator (FSM)      soak & master pump added here incrementally
        │   └── per-zone delivery modes
        │
   ValveCommandAdapter     ← actuator driver: switch.* vs valve.*
        │                              command + state. The shared FLOOR.
        │                              NOT part of the scheduler.
        │
   Home Assistant services            ← switch.turn_on/off | valve.open/close_valve
```

Key consequences:

1. **The actuator abstraction is the floor, not the ceiling.** The
   `ValveCommandAdapter` encapsulates switch-vs-valve domain
   knowledge (command mapping + state interpretation incl. `opening`/
   `closing`). It must sit *under* every control regime — both the
   FSM-driven `ValveOperator` and the self-driven `volume_preset` mode —
   so the domain lives in **one** place. It is an enabler for #94, and is
   the first concrete, scoped step of the broader abstraction Filip raised
   in #74 (switch/valve today, other actuators e.g. Hydrawise later).

2. **Orchestration (soak, master pump, ordering) is a high, cross-zone
   concern** — at a *configuration* altitude comparable to global model
   parameters, but a **different concern**: it is execution/orchestration,
   **not** the scientific model. The model decides *how much* water and
   *whether* to irrigate; orchestration decides *how* the delivery unfolds
   in time and which shared resources engage. Keep them in distinct
   modules; do not couple pump logic into the model code.

3. **Do not build the scheduler now.** Soak (#74) and master pump (#95)
   both fit the *existing* sequential controller (already a primitive
   scheduler): soak = a parametric pause between segments; master pump =
   a shared-resource hook around the run loop. A full scheduler/queue is
   only justified by demand for **parallel zones** or **time-window
   scheduling**, which nobody has requested (Filip himself judged it
   overengineering for a 2–5 zone target). Recorded as deferred: the deferred scheduler.

---

## 3. Two control regimes already coexist (and that is fine)

`valve-state-machine.md` documents that `volume_preset` **bypasses**
`ValveOperator` on purpose: smart auto-close valves drive their own state
and do not fit the operator's "I command, you obey" semantics. This is a
legitimate **second control regime**, a sibling of the FSM — not a bug.

But it pays a price worth tracking:

- It re-hardcodes the `switch` domain (controller.py ~822/829/846) and the
  no-operator fallback does too (controller.py ~1196/1215). **Any actuator
  adapter must cover these paths too**, or a `valve.*` user in
  `volume_preset` gets a silent no-op (and, worse, a valve that never
  closes — a safety failure). This is the single high-risk point of the actuator adapter.
- It **re-implements** slices of safety the operator already provides
  (pre-check, timeout force-close, `== "off"` state read). If operator
  safety semantics change, `volume_preset` does not inherit them. Tracked
  as tech-debt the volume_preset safety-dedup item — independent of the valve work.

---

## 4. Backward-compatibility discipline (binds the controller work)

The actuator adapter is **low-risk**: additive, domain derived at
runtime from the entity_id prefix, **zero config migration** — existing
`switch.*` configs keep working unchanged. This is why it goes **first**.

The controller work (soak, master pump — the master-pump/soak work) is the riskier intervention,
and the main risk is **backward compatibility of the triggering logic**.
Rules:

1. **New fields are optional, and "absent" must reproduce today's behaviour
   exactly.** Master pump defaults to `None` → no behavioural change for
   existing users.
2. **Avoid config-entry schema migration if at all possible** — purely
   additive optional fields need no `async_migrate_entry`. If a version
   bump is unavoidable, the migration must be tested.
3. **Preserve existing triggering by construction, not by recovery.** A
   user with N sequential zones, no soak, no pump must see *identical*
   behaviour after upgrade until they change something. Write this as a
   **non-regression test** *before* touching the controller.

---

## 5. Severity vs blast-radius — why the order is valve-first

| | Valve adapter | Soak + master pump |
|---|---|---|
| Blast radius | Contained, additive, zero migration | Broad: cross-zone state, new sequencing |
| Severity if wrong | High — touches safety paths (watchdog force-close, leak recovery): bug = valve stuck open | Med/High — pump left on = dry-run/overpressure; soak ~benign |
| Backward-compat risk | Low — runtime domain derivation, no migration | High — new config, interacts with triggering |

The valve adapter is the safer **first** move because it is *bounded and
zero-migration*, **not** because it is risk-free — it touches safety, so a
bug there is severe but well-localised. Ship the adapter, then valve-entity support, first (unblocks
#94 with no risk to existing configs), then approach the controller with a
non-regression safety net.

---

## 6. Sequencing summary

1. ✅ **Actuator adapter** — `ValveCommandAdapter` (floor), routed through *all* command
   and state sites including both bypass paths and the safety paths. **Done.**
   The prediction in §4 held exactly: `controller.py` had twelve switch-assuming
   sites, and the two bypass paths plus leak recovery were among them.
2. ✅ **Valve-entity support** — config-flow selector extended to
   `["switch","valve"]` on **both** the create and the edit form; a valve that
   could be created and not edited would have been the same defect one screen
   later. Scope v1 = binary valves (`OPEN`/`CLOSE`) as planned; position-based
   still deferred, deliberately — a partially open valve changes what a litre
   means, and the balance is kept in litres. **Done**, closes #94.
3. **volume_preset safety-dedup** — extract shared safety helpers so `volume_preset` stops
   duplicating operator safety (independent; synergistic with the actuator adapter).
4. **Master pump + soak** — soak + master pump as incremental controller capabilities,
   under the backward-compat discipline above. Closes #95 via PR.
5. **Unified scheduler** — unified scheduler: deferred, gated on parallel-zone /
   complex-scheduling demand.

---

## Revision history

| Date | Change |
|---|---|
| 2026-06 | Draft proposed (open for comment). |
| 2026-08-17 | Steps 1–2 implemented; status changed from *Draft* to *partly implemented*. The two halves shipped a day apart on purpose — adapter first, selector second — so no release ever offered a domain it could not drive. Two static guards now hold the property (see `architectural-guards.md`). |
