# Contributing to NeverDry

Thanks for your interest in NeverDry — a Home Assistant custom integration for
ET-based smart irrigation. Contributions of all kinds are welcome: bug reports,
fixes, features, documentation, and help verifying the science.

<!-- CI status — for contributors (user-facing trust badges live in README) -->
[![Tests](https://github.com/never-dry/NeverDry/actions/workflows/tests.yml/badge.svg)](https://github.com/never-dry/NeverDry/actions/workflows/tests.yml)
[![codecov](https://codecov.io/gh/never-dry/NeverDry/graph/badge.svg)](https://codecov.io/gh/never-dry/NeverDry)
[![HACS Validation](https://github.com/never-dry/NeverDry/actions/workflows/hacs.yml/badge.svg)](https://github.com/never-dry/NeverDry/actions/workflows/hacs.yml)
[![Lint](https://github.com/never-dry/NeverDry/actions/workflows/lint.yml/badge.svg)](https://github.com/never-dry/NeverDry/actions/workflows/lint.yml)
[![Security](https://github.com/never-dry/NeverDry/actions/workflows/security.yml/badge.svg)](https://github.com/never-dry/NeverDry/actions/workflows/security.yml)
[![Release](https://github.com/never-dry/NeverDry/actions/workflows/release.yml/badge.svg)](https://github.com/never-dry/NeverDry/actions/workflows/release.yml)

## Ways to contribute

- **Report a bug or request a feature** — open a GitHub issue with clear steps /
  context. For irrigation behaviour, include your delivery mode and valve type.
- **Report your valve** — the [valve register](docs/valve-compatibility.md) exists
  because two valves of the same brand, and even two units of the same model on
  different firmware, expose different things. There is a
  [form for it](.github/ISSUE_TEMPLATE/valve_report.yml), and a report of hardware
  that *does not* work is as useful as one that does.
- **Send a pull request** — see the workflow below.
- **Improve the docs** — user/developer manuals live in `docs/`; the engineering
  design notes live in the project documentation set (see *Understanding the
  architecture* below).
- **Help verify the science** — the bibliography behind the model is being
  reviewed claim-by-claim against primary sources. The method is documented and
  reproducible; picking up a few references is a great first contribution.

## Development setup

NeverDry targets **Python 3.11 / 3.12** and runs inside Home Assistant.

```bash
# clone your fork, then from the repo root:
# (the project pins dependencies via uv.lock; uv or pip both work)
pip install -r requirements_test.txt          # pytest + pytest-asyncio
pip install pytest-cov ruff bandit            # tooling used by CI
# Home Assistant must be importable to run the test suite.
```

The integration code lives in `custom_components/never_dry/`; tests in `tests/`.

## Before you commit — run what CI runs

CI will reject a PR that fails any of these, so run them locally first. They
mirror `.github/workflows/lint.yml` and `tests.yml`:

```bash
# 1. Lint + format (CI runs BOTH check and format --check)
ruff check  custom_components/never_dry/ tests/
ruff format custom_components/never_dry/ tests/      # then re-run with --check

# 2. Tests (must keep coverage >= 75%)
python -m pytest tests/ -v

# 3. Security scan
bandit -r custom_components/never_dry/ --severity-level medium --confidence-level medium
```

A pre-commit config is provided (`ruff --fix`, `ruff-format`, `bandit`):

```bash
pip install pre-commit && pre-commit install
```

> ⚠️ **Never run `ruff format` on `manifest.json` or other JSON files.** It adds
> trailing commas and corrupts the JSON, which breaks the integration and CI.
> The format command above is scoped to Python paths on purpose.

CI also validates the integration with **hassfest** and **HACS**; keep
`manifest.json` and `hacs.json` valid.

## Branch & PR workflow

- **Branch off `main`** with a descriptive name (e.g. `fix/valve-close-leak`,
  `feat/valve-entity-support`). Do **not** commit directly to `main`.
- Keep commits focused; write **commit messages and PR descriptions in English**.
- Open a PR against `main`. Describe *what* and *why*, and link any related issue
  (`Closes #123`). Make sure all CI checks pass.
- For new behaviour, add or update tests. For changes to irrigation/valve safety,
  a **non-regression test** of existing behaviour is expected.

## Code style

- Enforced by **ruff** (config in `pyproject.toml`): line length 120, target
  `py311`, rule sets `E,W,F,I,B,S,UP,SIM,RUF`.
- Match the style and altitude of the surrounding code; prefer clarity over
  cleverness.

## The boundary: entities in, entities out

Before proposing anything that touches hardware, know where this project stops.

> **NeverDry consumes Home Assistant entities. It does not speak to your hardware.**

No MQTT publishing, no Zigbee awareness, no parsing of a device's JSON, no vendor
cloud APIs. A pull request that adds any of those will be turned down on principle,
not on quality — the reasoning is written out in
[`docs/hardware-interface.md`](docs/hardware-interface.md), and it comes down to
this: the moment the integration speaks one ecosystem, every user on the others is
served by a code path nobody tests, on hardware nobody here owns.

The consequence for contributors is practical rather than restrictive. When a device
hides a value where no entity exists — a Zigbee2MQTT composite is the usual case —
the fix is **a documented recipe, not a feature**. Recipes are cheap, correctable by
anyone, and they work for people who do not use this integration at all. Adding one
to `hardware-interface.md`, with the cautions you discovered while making it work,
is a first-class contribution.

And its corollary, which decides *what to write instead*:

> **Prefer the mechanism that already exists in the layer that owns the problem.
> Document it. Do not reinvent it.**

Zigbee2MQTT converters and ZHA quirks already interpose a read/write layer between a
device and Home Assistant, and core Home Assistant already has template and MQTT
entities and blueprints. Each is maintained and reviewed by more people than this
project has. A proposal that adds a new mechanism should say which of those was tried
and why it did not reach.

Two consequences worth stating, because they have already come up:

- **A capability the hardware has does not oblige us to reach it.** Volume dosing is
  writable on every valve tested here and reachable on none, because the target sits
  inside a composite. That is documented as a recipe, deliberately.
- **Firmware trivia belongs in a document, not in code.** Which firmware renames
  which entity, whether an amount is litres or gallons this month: put it where
  anyone can correct it without a release.

## Understanding the architecture (start here)

To make a meaningful change, read the design notes — start with the index:

1. [`docs/design/README.md`](docs/design/README.md) — design & engineering notes
   in reading order (architecture → direction → science → testing).
2. `docs/developer_manual.md` — architecture overview, formulas, module map.
3. `docs/ha_integration_guide.md` — Home Assistant integration patterns.
4. [`docs/hardware-interface.md`](docs/hardware-interface.md) — the interface
   contract: the three entity shapes the integration expects, and why it expects
   nothing else.

The actuator-abstraction direction
([`docs/design/actuator-abstraction.md`](docs/design/actuator-abstraction.md)) is
**partly implemented**: the command adapter and `valve.*` support shipped, the
orchestration questions are still open on #74 — see *How we record design
decisions* below.

## How we record design decisions

Significant or cross-cutting changes are captured as a single **design note**
whose `Status` field moves through a lifecycle. *RFC* and *ADR* are not separate
file types — they are **phases of the same document**:

```
Draft        → internal proposal, not yet circulated for comment
  → Proposed → open for comment (the "RFC" phase — usually on a GitHub issue)
  → Accepted → the decision is settled (this is the "ADR")
```

Plus terminal states: `Rejected` / `Withdrawn` / `Deferred`, and later
`Superseded` / `Deprecated`. A note is **never** marked `Accepted` while the
decision is still open for input.

**What this means for you:** for a large or architectural change, open or comment
on the relevant design note *before* sending a big PR — it avoids rework. For
example, the actuator-abstraction direction
([`docs/design/actuator-abstraction.md`](docs/design/actuator-abstraction.md),
currently `Draft`) grew out of [@fpytloun](https://github.com/fpytloun)'s proposal
in **#74** — community input like that is exactly how the bigger decisions get
shaped, and it's much appreciated. Small, self-contained fixes can go straight to
a PR.

## Roles

NeverDry is maintained by one person. These roles exist to remove friction, not to
build a hierarchy, and none of them is a claim on your time.

**Contributor** — anyone who has had a commit merged, reported a bug, tested a
pre-release, or shaped a design note. This is not granted and there is nothing to
apply for: if you have done any of that, you already are one.

**Triage** — an invited role (GitHub's `triage` permission). It allows labelling,
assigning, closing and reopening issues, and being formally requested as a
reviewer. It carries **no write access**: you cannot push or merge anything. It is
offered to people already doing this work informally — mostly so that asking you
for a review stops requiring a hand-written ping.

**Write** — push and merge rights. Offered to contributors with a track record of
merged changes who would rather carry work through themselves than hand it over.

### What an invitation means

- It is an offer, not an assignment. No expected workload, no rota, no
  response-time expectation.
- Declining costs nothing and changes nothing about how your contributions are
  received.
- It is revocable from both sides, at any time, without it being a statement.
- The invitation will always say what specifically prompted it. A role handed out
  by volume rather than by contribution is worth nothing to the person receiving
  it.

### How it happens

You get asked first, in a thread where you are already active. A GitHub invitation
is only sent after you say yes.

## Reporting security issues

Please **do not** open public issues for security vulnerabilities. See
[`SECURITY.md`](SECURITY.md) for private disclosure.

## License

By contributing, you agree that your contributions are licensed under the
project's [LICENSE](LICENSE).
