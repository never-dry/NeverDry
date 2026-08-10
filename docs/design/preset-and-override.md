# Presets and Overrides — one rule for three settings

**Status:** Accepted (ADR), 2026-08-10.

Three zone settings share one shape: a dropdown of presets, plus a box for a
value the presets do not cover.

| Dropdown | Box | Where it resolves |
|---|---|---|
| system type | efficiency | `sensor.py`, zone setup |
| plant family | manual Kc | `compute_kc()` |
| exposure | microclimate factor | `resolve_microclimate_factor()` |

## The rule

**The dropdown decides.** Each preset table marks its custom entry with `None`
in the value field — `default_efficiency`, `kc_seasonal`, `factor` — and only
that entry reads the box. Behind a real preset the box is not used.

Within a pair it is always one or the other, never both, and nothing is ever
summed. Across pairs the results compose: `Kc = base × kmc`, where the plant
family pair produces the base and the exposure pair the factor.

The flow enforces the rule at the boundary:

- **Custom with an empty box is an error.** The combination means nothing: the
  zone would fall back to a neutral default and behave as if no choice had
  been made, which is the silent no-op the dropdown exists to prevent.
- **A preset with a value in the box is a warning, not an error.** The number
  may be a leftover from an earlier attempt. Refusing to save over it would
  trap the user the way [#165](https://github.com/never-dry/NeverDry/issues/165)
  did; instead the confirmation step names each ignored value, per pair.
- **A value with nothing selected warns too.** A Kc and no plant family reads
  as intent, but it would never be applied.

## Why it changed

Exposure worked this way from the start ([#146](https://github.com/never-dry/NeverDry/issues/146),
PR [#147](https://github.com/never-dry/NeverDry/pull/147)). The other two did the
opposite: a stored efficiency or manual Kc took charge on its own, while the
dropdown went on displaying a choice that no longer had any effect. A zone
could read "Drip irrigation" and water at 0.55, and nothing on screen said so.

Two rules in one form is why the question *"which of the two applies?"* had no
answer a user could learn. The fix is not more UI — a config flow step cannot
grey out one field based on another, because the schema is built server-side
when the form opens and does not react to typing. The fix is one rule, stated
in the form's own field descriptions.

## The migration, and why it must stay forever

Switching two pairs from "the box decides" to "the dropdown decides" would
silently change how existing zones water: a drip zone running at 0.55 would
jump to 0.92 and deliver less, with nobody having touched anything.

`async_migrate_entry` step **2 → 3** writes `custom` into the dropdown wherever
a value is already in charge:

| Stored zone | In charge before | Migration |
|---|---|---|
| `efficiency` present | the value | `system_type = custom` |
| `kc` present | the value | `plant_family = custom` |
| `exposure = custom` + factor | the factor | untouched, already correct |
| `exposure = preset` + factor | the preset — the factor was ignored | **untouched** |

The last row is the one that must *not* be migrated. Marking it custom would
switch on a factor the zone never used and change its watering — the exact harm
the migration exists to prevent. Those leftovers are the config flow's business:
it warns about them on the next save.

The value in charge never changes. The migration only writes down what was
already true.

**Do not delete this step in a later release.** Migrations are a cumulative
chain: someone upgrading from an old version still has to pass through 2 → 3.
Keeping it costs nothing — it only runs for entries still at version 2 — while
removing it silently breaks anyone who skipped a few releases.

## The form's three sections

The zone form is 17 fields. It is grouped into three collapsible sections, and
the split is the domain model's own rather than a visual convenience:

| Section | Question | Objects |
|---|---|---|
| ground and location | what is watered | `Zone`, `WaterBalanceModel` |
| valve and pipe | what waters it | `Driver` |
| scheduling | when it waters | `Scheduler` |

The zone name sits outside the sections: it identifies the zone rather than
configuring it.

Sections nest their fields in the submitted data (`user_input["valve_and_pipe"]
["efficiency"]`). **A stored zone stays flat**: `_flatten_sections()` undoes the
nesting at the boundary, so sensors, controller and migrations are untouched by
a presentation choice. It is deliberately tolerant of already-flat input, which
is what keeps the confirm step and every direct caller working.

This raises the Home Assistant floor to **2024.6**, the release that introduced
collapsible sections. `hacs.json` was bumped accordingly.

## Invariants to preserve

- A preset table entry with a `None` value **is** the custom marker. Both the
  resolvers and the flow guards key off it, never off the string `"custom"`.
- Resolvers stay total: unknown, unset or non-numeric gives a neutral default,
  never 0 (which freezes the deficit) and never an exception (which would abort
  setup for every zone in the entry).
- The override box must remain clearable — `suggested_value`, never `default=`,
  and a box, never a slider. See
  [#165](https://github.com/never-dry/NeverDry/issues/165): a slider has no empty
  state to submit, so an override set by accident becomes permanent.
