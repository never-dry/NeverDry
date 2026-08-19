# Architectural guards — the properties CI holds for you

**Status:** Accepted (ADR), 2026-08-16
**Related:** [`../developer_manual.md`](../developer_manual.md) §8b (the operational
checklist), `tests/test_architecture.py`, `tests/test_translation_consistency.py`,
`tests/test_et_method_choice.py`

## Why these exist at all

Every guard in this document was written **after** the property it holds had
already been lost, in production, on a real garden. None of them is a style
preference, and none was added because a rule seemed nice: each is a fossil of a
specific failure.

That history matters when you are tempted to work around one. A guard that
blocks you is not asking you to write different code — it is asking you to say
out loud what you are doing, because the last time nobody said it, the
consequence took weeks to surface and reached the water.

The failures share a shape worth naming, because it will recur in forms none of
these guards cover:

> **The dangerous defect is not the one that crashes. It is the one where setup
> succeeds, the entities come back, and a number is quietly different.**

A crash is reported the same evening. A deficit that stops growing, a model that
silently degrades, a valve whose retry budget moved — nobody reports those,
because nothing looks wrong. The garden is simply watered differently.

## The guards

### Layering: purity and direction

`zone`, `water_balance_model`, `environment` and `scheduler` carry rules and must
import no Home Assistant, and must never import the entity layer that consumes
them.

**Lost as:** a fix for the already-open valve confirmation landed in the domain
copy while production kept the bug, because two implementations of the same
rules existed and only one ran.

**What it buys:** the rules can be exercised without a runtime, which is why the
water balance has arithmetic tests at all.

### Declared edges between domain modules

Every import *between* domain modules is listed in `ALLOWED_DOMAIN_EDGES` with a
reason. Checked in both directions: an edge nobody uses any more fails too.

**Why both directions:** a permission list that only grows stops describing the
code and starts excusing it.

**What it is really preventing:** spaghetti is never one bad import. It is a
dozen reasonable ones that nobody had to justify. Requiring the arrow to be
written down turns each into a decision with a diff, which a reviewer can see.

### No import cycles, anywhere

**Why it is worth a test:** Python tolerates most cycles as long as the import
order happens to work. They surface months later as an obscure `ImportError` on
somebody else's machine, in a configuration you cannot reproduce.

### `WIRED` / `INERT`, and "one formula, one home"

Which domain modules production imports is a declared fact, checked both ways;
and a formula listed in `SINGLE_HOME_FORMULAS` may appear in exactly one
module's executable code.

**Lost as:** `zone.py` announced itself as unwired for two releases *after*
`sensor.py` had delegated the whole zone state to it.

**The rule underneath:** wiring an object is not finished when it is called. It
is finished when the copy it replaced is gone — otherwise you have added
duplication and the feeling of having removed it.

### Form and runtime answer the same question

What the config flow accepts must be what `build_model` runs, for every
combination of site and method.

**Why it cannot be left to review:** the drift is silent by construction. Setup
succeeds, the model degrades to something the site supports, and the user
believes they are running a tier they are not.

### Every offered method actually runs

Each entry in the dropdown is driven through a real update, on a site equipped
for that method.

**Lost as:** two methods built correctly and raised on their first reading,
because the hub fed them the reading it knew how to make rather than the one
they consume. Construction was never the hard part.

**And then lost again, more quietly:** the first version of this guard filled
the diurnal window at hours 0..23 while the hub observes at the real current
hour, so the window was discarded and every tier exercised the warm-up fallback
— passing, while testing the same formula four times. **A test that cannot fail
is worse than no test**, because it is counted.

### One valve vocabulary, and a form that offers it

No production module outside `driver.py` may name a valve domain or one of its
services; and every domain the adapter can command must appear in the config-flow
selector, checked in both directions.

**Lost as:** GH #94 — a user with an Orbit B-hyve timer could not select their
valve, because the form listed switches only. The adapter that fixed it was
written first and deliberately left unwired, and that ordering is the point: had
the selector been widened first, the entity would have saved without an error
and never opened.

**Why the count is what matters:** `controller.py` alone held **twelve** sites
that assumed the switch domain, two of them on bypass paths and one on leak
recovery. Eleven fixed sites and one missed is not a partial fix, it is a broken
install for whoever lands on the twelfth — and nothing about it looks wrong until
that zone dries out.

**And the reverse direction, which is the newer half:** once the adapter handles
a domain, leaving it out of the form is a capability nobody can reach. Both
halves ship together or the feature is a claim, not a feature.

### Select options are lists

`SelectSelectorConfig(options=...)` must be list-shaped, checked statically.

**Lost as:** the options form could not open at all — voluptuous refuses a
tuple — while 1286 tests were green.

**The general lesson, and the most useful line in this document:** the suite
stubs Home Assistant, so it *models* it. Wherever the model is thinner than the
original, the difference is invisible until it runs. When you find such a place,
a static guard is still worth writing, and widening the stub is part of the fix.

## What this does not cover, and what to do about it

No guard here would have caught the defects that mattered most in the release
that produced them: a pyranometer reading treated as a daily energy, a deficit
discarded when the model was rebuilt, six entities frozen at their startup
value. Those were found by **deploying to a real garden and reading the
numbers**, which is why the project keeps a field instance and why the model
publishes what it was fed.

Two habits follow, and they are the point of this document:

1. **Publish the intermediate values.** A derived quantity that is quietly wrong
   looks exactly like one that is right. The daily radiation, the diurnal range,
   the wind brought to two metres — each is an entity, with history, because the
   way to judge a computed value is to watch it follow the weather for a week.
2. **Believe the instance over the suite.** When the two disagree, the suite is
   the one that is modelling something. Every time that has happened here, the
   instance was right.
