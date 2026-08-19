"""Architectural invariants, enforced here rather than in review.

The domain model — `Zone`, the water-balance models, `Environment`,
`Scheduler`, and the `Driver` hierarchy — was written before it was wired, so
for a while two implementations of the same rules existed side by side. That
is survivable while it is *known*; it stops being survivable when a fix lands
in the copy that does not run, which is what happened with the already-open
valve confirmation (it went into `driver.py`, and production kept the bug).

These tests hold the three properties that keep the migration honest:

1. the domain modules stay **pure** — no Home Assistant, no I/O;
2. they never depend **upward** on the layer that consumes them;
3. which of them are wired is a **declared fact**, checked against reality,
   so "inert scaffold" cannot quietly stop being true — as it already did
   once, in `zone.py`'s own docstring, for two releases.

Wiring an entity therefore means moving one name in `WIRED` below and
watching what breaks. That is the intended workflow, not an obstacle to it.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

PACKAGE = pathlib.Path(__file__).resolve().parent.parent / "custom_components" / "never_dry"

#: Modules that carry domain rules and must remain free of Home Assistant.
#: `driver.py` is deliberately absent: it is an *actuator*, HA-aware by design
#: (see its module docstring), so purity does not apply to it.
PURE_DOMAIN_MODULES = ("zone", "water_balance_model", "environment", "scheduler")

#: The Home-Assistant-coupled layer. The domain must never import it.
HA_LAYER_MODULES = (
    "sensor",
    "controller",
    "valve_operator",
    "config_flow",
    "diagnostics",
    "button",
    "driver",
    # Both hold Home Assistant state on behalf of the domain rules they feed:
    # `session_flow` persists what deliveries measured, `reachability_watch`
    # collects the silences that `environment.judge_fleet` weighs. Listed here
    # so the domain cannot import them — the omission would have let a pure
    # module reach the runtime through a module nobody had classified.
    "session_flow",
    "reachability_watch",
)

#: Domain modules the integration actually imports today. Move a name here in
#: the same commit that wires it — the test below fails in both directions, so
#: neither a silent wiring nor a stale claim can pass.
WIRED = {"zone", "water_balance_model", "environment", "scheduler", "driver"}

#: The counterpart: written, tested, and reached by nothing but the tests.
#: Empty since the driver was wired — kept because the next scaffold will need
#: it, and because an empty set is the honest way to say "nothing pending".
INERT: set[str] = set()

#: Phrase the scaffolds use to announce they are not wired. A module that says
#: this while being imported is the exact drift these tests exist to catch.
INERT_CLAIM = "Nothing imports this module yet"


# ── Helpers ───────────────────────────────────────────────────────────


def _imported_names(module: str) -> set[str]:
    """Every module name imported by ``module``, absolute and relative alike."""
    tree = ast.parse((PACKAGE / f"{module}.py").read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            # level > 0 is a relative import: `from .zone import Zone` has
            # module == "zone". A bare `from . import x` has module None.
            names.add(node.module or "")
            if node.level:
                names.update(alias.name for alias in node.names)
    return names


def _integration_modules() -> list[str]:
    """Every module of the package, domain and HA layer together."""
    return sorted(p.stem for p in PACKAGE.glob("*.py") if p.stem != "__init__")


def _importers_of(target: str) -> set[str]:
    """Which modules of the package import ``target``."""
    return {
        module
        for module in _integration_modules()
        if module != target and any(name == target or name.endswith(f".{target}") for name in _imported_names(module))
    }


# ── Purity ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("module", PURE_DOMAIN_MODULES)
def test_domain_module_is_pure(module):
    """No Home Assistant import: the rules must be testable without a runtime.

    This is what lets the water balance and the scheduling rules be exercised
    directly, and it is the property most easily lost — one convenience import
    of `homeassistant.util.dt` is enough.
    """
    ha_imports = sorted(name for name in _imported_names(module) if name.split(".")[0] == "homeassistant")
    assert not ha_imports, f"{module}.py imports Home Assistant: {ha_imports}"


@pytest.mark.parametrize("module", PURE_DOMAIN_MODULES)
def test_domain_module_does_not_depend_upward(module):
    """The domain does not import the layer that consumes it.

    A cycle here would mean the rules can no longer be read, or moved, without
    the entity layer coming along.
    """
    imported = _imported_names(module)
    upward = sorted(name for name in HA_LAYER_MODULES if name in imported)
    assert not upward, f"{module}.py imports the HA layer: {upward}"


# ── Wiring status as a declared fact ──────────────────────────────────


def test_declared_wiring_covers_every_domain_module():
    """`WIRED` and `INERT` must together describe the whole domain, exactly once."""
    declared = WIRED | INERT
    known = set(PURE_DOMAIN_MODULES) | {"driver"}
    assert not (WIRED & INERT), f"declared both wired and inert: {sorted(WIRED & INERT)}"
    assert declared == known, (
        f"undeclared domain modules: {sorted(known - declared)}; unknown names declared: {sorted(declared - known)}"
    )


@pytest.mark.parametrize("module", sorted(WIRED))
def test_module_declared_wired_is_imported_by_the_integration(module):
    """A module declared wired must be reached from production, not only tests."""
    importers = _importers_of(module)
    assert importers, f"{module}.py is declared WIRED but nothing in the package imports it"


@pytest.mark.parametrize("module", sorted(INERT))
def test_module_declared_inert_is_imported_by_nothing(module):
    """The other direction: wiring something without saying so also fails.

    This is the half that matters. A scaffold that quietly becomes load-bearing
    is how the second source of truth is born.
    """
    importers = _importers_of(module)
    assert not importers, (
        f"{module}.py is declared INERT but is imported by {sorted(importers)} — "
        f"move it to WIRED in the same commit that wires it"
    )


# ── One formula, one home ─────────────────────────────────────────────
#
# Wiring a domain object is not finished when the object is called: it is
# finished when the copy it replaced is gone. Only a test can hold that, and its
# absence is why three copies of the crop coefficient and two of the settle
# bookkeeping survived for months.
#
# The list grows as each wiring completes — it is a ledger of what has actually
# been consolidated, not an aspiration. Still outstanding, deliberately: the
# seasonal Kc curve, which needs the plant-family table to move with it.

SINGLE_HOME_FORMULAS = (
    (r"alpha\s*\*\s*\(", "water_balance_model", "the ET rate"),
    (r"field_cap\w*\s*-\s*vwc", "water_balance_model", "the VWC deficit"),
    (r"efficiency\s*/\s*self\s*\.\s*area_m2", "zone", "crediting delivered water"),
    (r"as_liters\s*\(.*\)\s*/\s*self\s*\.\s*efficiency", "zone", "the deficit as litres"),
)


def _executable_source(module: str) -> str:
    """Module source with comments and string literals removed.

    A formula named in a docstring is documentation, not a second copy — and
    every one of these formulas is *described* in prose somewhere on purpose.
    Only code counts, so the tokens that are not code are dropped.
    """
    import io
    import tokenize

    source = (PACKAGE / f"{module}.py").read_bytes()
    pieces: list[str] = []
    for tok in tokenize.tokenize(io.BytesIO(source).readline):
        if tok.type in (tokenize.STRING, tokenize.COMMENT):
            continue
        pieces.append(tok.string)
    return " ".join(pieces)


@pytest.mark.parametrize(("pattern", "home", "what"), SINGLE_HOME_FORMULAS)
def test_formula_has_a_single_home(pattern, home, what):
    """The formula may appear in exactly one module's executable code."""
    import re

    found = sorted(m for m in _integration_modules() if re.search(pattern, _executable_source(m)))
    assert found == [home], f"{what} should live only in {home}.py, found in {found}"


# ── "Mirrors const.py" has to be true ─────────────────────────────────


def test_domain_enums_mirror_const():
    """A scaffold that says it mirrors ``const.py`` must actually mirror it.

    ``RainSensorType`` declared three values against the two the integration
    ships. The missing third was not an oversight on the shipped side: telling a
    midnight-reset total from a rolling window was removed deliberately, because
    guessing between them wiped deficits at 05:00 on one and dropped overnight
    rain on the other (GH #123). A scaffold still offering the choice would have
    reintroduced the bug the day it was wired.
    """
    from never_dry.const import (
        IRRIGATION_MODE_MANUAL,
        IRRIGATION_MODE_REACTIVE,
        IRRIGATION_MODE_SCHEDULED,
        RAIN_TYPE_DAILY_TOTAL,
        RAIN_TYPE_EVENT,
    )
    from never_dry.environment import RainSensorType
    from never_dry.zone import IrrigationMode

    assert {t.value for t in RainSensorType} == {RAIN_TYPE_EVENT, RAIN_TYPE_DAILY_TOTAL}
    assert {m.value for m in IrrigationMode} == {
        IRRIGATION_MODE_MANUAL,
        IRRIGATION_MODE_REACTIVE,
        IRRIGATION_MODE_SCHEDULED,
    }


@pytest.mark.parametrize(
    ("domain_module", "domain_name", "const_name"),
    [
        ("zone", "DEFAULT_EFFICIENCY", "DEFAULT_EFFICIENCY"),
        ("zone", "DEFAULT_THRESHOLD_MM", "DEFAULT_THRESHOLD"),
        ("zone", "DEFAULT_MICROCLIMATE_FACTOR", "DEFAULT_MICROCLIMATE_FACTOR"),
        ("water_balance_model", "DEFAULT_ALPHA", "DEFAULT_ALPHA"),
        ("water_balance_model", "DEFAULT_T_BASE", "DEFAULT_T_BASE"),
        ("water_balance_model", "DEFAULT_D_MAX", "DEFAULT_D_MAX"),
        ("water_balance_model", "DEFAULT_FIELD_CAPACITY", "DEFAULT_FIELD_CAPACITY"),
        ("water_balance_model", "DEFAULT_ROOT_DEPTH", "DEFAULT_ROOT_DEPTH"),
        ("water_balance_model", "DEFAULT_KC", "DEFAULT_KC"),
        ("environment", "DEFAULT_BACKFILL_DAYS", "DEFAULT_BACKFILL_DAYS"),
        ("scheduler", "DEFAULT_MIN_SERVICE_INTERVAL_S", "MIN_SERVICE_INTERVAL_S"),
    ],
)
def test_domain_defaults_mirror_const(domain_module, domain_name, const_name):
    """The pure modules keep their own copies so they stay importable alone.

    Copies drift. These are the ones that claim to be copies, checked against
    the originals — a one-line guard against a whole class of silent divergence.
    """
    import importlib

    from never_dry import const

    mod = importlib.import_module(f"never_dry.{domain_module}")
    assert getattr(mod, domain_name) == getattr(const, const_name), (
        f"{domain_module}.{domain_name} has drifted from const.{const_name}"
    )


@pytest.mark.parametrize("module", sorted(WIRED))
def test_a_wired_module_does_not_claim_to_be_inert(module):
    """Its docstring has to agree with its status.

    `zone.py` said "Nothing imports this module yet" for two releases while
    `sensor.py` delegated the whole zone state to it. The claim a reader meets
    first was the one that was wrong.
    """
    source = (PACKAGE / f"{module}.py").read_text(encoding="utf-8")
    docstring = ast.get_docstring(ast.parse(source)) or ""
    assert INERT_CLAIM not in docstring, f"{module}.py is wired but its docstring still says {INERT_CLAIM!r}"


# ── The shape of the graph, not only its layers ───────────────────────
#
# Purity and "no upward dependency" bound the domain from the outside. They say
# nothing about what the domain modules do to *each other*, and that is where a
# model turns to spaghetti: not in one bad import, but in a dozen reasonable
# ones nobody had to justify. Two properties keep the graph readable.
#
# The first is that it stays acyclic. The second is that every edge between
# domain modules is written down here — so adding one is a decision with a
# diff, and the reviewer sees the arrow rather than inferring it.

#: Every allowed import between domain modules, as ``importer -> imported``.
#: An edge missing from this map fails the test even if it is perfectly
#: sensible; that is the point — write it down, and say why in the commit.
ALLOWED_DOMAIN_EDGES: dict[str, set[str]] = {
    # The models are the vocabulary of the deficit, and the zone owns one.
    "zone": {"water_balance_model"},
    # A model declares what it needs in the same words the site declares what it
    # has, so the capability match is expressible at all. The reverse arrow must
    # never appear: the site knows nothing about the physics that reads it.
    "water_balance_model": {"environment"},
    # Scheduling decides *which zone* comes next, so it reads zones.
    "scheduler": {"zone"},
    "environment": set(),
}


def _domain_edges() -> dict[str, set[str]]:
    """The import graph restricted to domain modules, as it exists today."""
    domain = set(PURE_DOMAIN_MODULES)
    return {module: _imported_names(module) & domain for module in domain}


@pytest.mark.parametrize("module", PURE_DOMAIN_MODULES)
def test_domain_imports_are_declared(module):
    """A domain module may import only what this file says it may.

    The failure message is the useful part: it names the undeclared edge, so the
    fix is either to add it here with a reason, or to notice that the import was
    the shortcut it looked like.
    """
    actual = _domain_edges()[module]
    allowed = ALLOWED_DOMAIN_EDGES[module]
    undeclared = sorted(actual - allowed)
    assert not undeclared, (
        f"{module}.py imports {undeclared}, which ALLOWED_DOMAIN_EDGES does not permit. "
        f"Add the edge with a reason, or drop the import"
    )


def test_declared_edges_are_not_stale():
    """The other direction: an edge that no longer exists must not be claimed.

    A permission list that only ever grows stops describing the code and starts
    excusing it.
    """
    actual = _domain_edges()
    stale = {module: sorted(allowed - actual[module]) for module, allowed in ALLOWED_DOMAIN_EDGES.items()}
    stale = {module: names for module, names in stale.items() if names}
    assert not stale, f"declared edges nobody uses any more: {stale}"


def test_the_package_has_no_import_cycles():
    """No cycle anywhere in the package, domain and HA layer alike.

    A cycle is the point at which two modules stop being two things. It is also
    the failure that hides best: Python tolerates most of them at runtime as
    long as the import order happens to work, so it surfaces as an obscure
    ImportError months later, on someone else's machine.
    """
    modules = set(_integration_modules())
    graph = {m: _imported_names(m) & modules for m in modules}

    visiting: set[str] = set()
    done: set[str] = set()
    cycles: list[list[str]] = []

    def walk(node: str, path: list[str]) -> None:
        if node in done:
            return
        if node in visiting:
            cycles.append([*path[path.index(node) :], node])
            return
        visiting.add(node)
        for nxt in sorted(graph[node]):
            walk(nxt, [*path, node])
        visiting.discard(node)
        done.add(node)

    for module in sorted(modules):
        walk(module, [])

    assert not cycles, f"import cycles: {[' -> '.join(c) for c in cycles]}"


# ── Documentation that contradicts the code ───────────────────────────
#
# The guards above stop a claim rotting inside the package. Prose rots the same
# way and nothing catches it: `scientific-model.md` called two methods "planned,
# not implemented" while both were running, and the developer manual opened with
# "All formulas live in sensor.py" — the opposite of what the wiring was for.
#
# Neither was a lie when written, which is the point. A statement about the code
# has to be checked against the code, or it is only checked when a reader
# happens to notice.

DOCS = PACKAGE.parent.parent / "docs"


def test_no_document_calls_a_runnable_method_planned():
    """A method that runs may not be described as planned, anywhere in docs/.

    Checked per paragraph, and per row inside a table: "planned" is a legitimate
    word about work that *is* planned, and only its appearance next to a method
    that already runs is the defect. The table granularity is not a detail — a
    status table legitimately says "not implemented" of one object while naming
    a running method in another row, and a guard that cries wolf there is a
    guard somebody eventually deletes.
    """
    import re

    from never_dry.water_balance_model import MODEL_CATALOGUE, RUNNABLE_INPUTS

    runnable = [m for m in MODEL_CATALOGUE if m.input_type in RUNNABLE_INPUTS]
    names = {
        "hargreaves": "Hargreaves",
        "penman_monteith": "Penman-Monteith",
        "vwc_system": "soil moisture probe",
    }
    stale: list[str] = []

    for path in sorted(DOCS.rglob("*.md")):
        blocks: list[str] = []
        for paragraph in re.split(r"\n\s*\n", path.read_text(encoding="utf-8")):
            # A markdown table is one paragraph but many independent statements.
            blocks.extend(paragraph.splitlines() if paragraph.lstrip().startswith("|") else [paragraph])

        for block in blocks:
            lowered = block.lower()
            if "planned" not in lowered and "not implemented" not in lowered:
                continue
            for model in runnable:
                name = names.get(model.method_id)
                if name and name.lower() in lowered:
                    stale.append(f"{path.relative_to(DOCS.parent)}: '{name}' described as planned")

    assert not stale, "documentation contradicts the code:\n  " + "\n  ".join(stale)


def test_the_developer_manual_does_not_place_the_formulas_in_the_entity_layer():
    """The claim that broke first, kept as a test because it broke silently.

    "All formulas live in sensor.py" was true for a year and became the exact
    opposite of the design without a single line of prose changing.
    """
    manual = (DOCS / "developer_manual.md").read_text(encoding="utf-8")
    assert "All formulas live in `sensor.py`" not in manual, (
        "the formulas live in water_balance_model.py — the manual is describing the code from before the wiring"
    )


def test_the_valve_selector_offers_both_domains_the_driver_can_drive():
    """What the form offers must match what the actuator can actually command.

    The two halves of `valve.*` support arrived a day apart: the adapter first,
    the selector second, and the gap was deliberate — offering a valve the
    command path could not open would have produced an entity that saves without
    error and never waters. This test is the other direction of the same care:
    once the adapter handles a domain, hiding it in the form is a capability
    nobody can reach.
    """
    import ast

    from never_dry.driver import EntityDomain

    drivable = {d.value for d in EntityDomain}
    tree = ast.parse((PACKAGE / "config_flow.py").read_text(encoding="utf-8"))

    offered: set[str] = set()
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "EntitySelectorConfig"
        ):
            continue
        for kw in node.keywords:
            if kw.arg != "domain":
                continue
            if isinstance(kw.value, ast.List):
                values = {e.value for e in kw.value.elts if isinstance(e, ast.Constant)}
                if values & drivable:
                    offered |= values

    assert drivable <= offered, (
        f"the driver can command {sorted(drivable)} but the valve selector only offers {sorted(offered)}"
    )


def test_only_the_adapter_names_a_valve_service():
    """`switch.turn_on` written anywhere else is a half-broken install.

    GH #94 is what this costs when it is missed: a `valve.*` entity that the
    form accepts, that saves without an error, and that never opens. The
    dangerous shape is not one wrong site — it is eleven fixed sites and one
    missed, because eleven-twelfths of a fix looks exactly like a fix until the
    zone in question is the one that stops watering.

    So the domain lives in `ValveCommandAdapter` and the rest of production asks
    it. Production only — the tests below name services deliberately, because
    asserting the right service reached HA is their job.
    """
    import ast

    ALLOWED = {"driver.py", "valve_operator.py"}  # the adapter, and the module it superseded
    SERVICES = {"turn_on", "turn_off", "open_valve", "close_valve"}

    offenders: list[str] = []
    for module in sorted(PACKAGE.glob("*.py")):
        if module.name in ALLOWED:
            continue
        tree = ast.parse(module.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "async_call"
                and node.args
            ):
                continue
            domain = node.args[0]
            service = node.args[1] if len(node.args) > 1 else None
            named_domain = isinstance(domain, ast.Constant) and domain.value in {"switch", "valve"}
            named_service = isinstance(service, ast.Constant) and service.value in SERVICES
            if named_domain or named_service:
                offenders.append(f"{module.name}:{node.lineno}")

    assert not offenders, (
        "these sites name a valve service directly instead of asking ValveCommandAdapter, "
        f"so they work for one entity domain and silently fail for the other: {offenders}"
    )
