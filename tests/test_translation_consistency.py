"""Consistency guard: every ``translation_key`` used by a SelectSelector in
``config_flow.py`` must have matching entries in ``translations/en.json``.

Home Assistant lets a SelectSelector replace inline option labels with a
``translation_key`` that resolves human-readable text from the translation
files (``selector.<key>.options.<value>``). If the key is referenced but the
translation file lacks the corresponding ``selector`` entries, the dropdown
silently shows the raw option values (e.g. ``estimated_flow``) instead of a
label — a UX regression that no other test catches.

This test parses ``config_flow.py`` statically (via ``ast``, no HA import) and
fails when a referenced ``translation_key`` is missing options in ``en.json``.
It is intentionally a no-op while the config flow uses inline labels; it
activates the moment someone migrates a selector to ``translation_key``.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

from never_dry import const

_COMPONENT = Path(__file__).resolve().parent.parent / "custom_components" / "never_dry"
_CONFIG_FLOW = _COMPONENT / "config_flow.py"
_EN_JSON = _COMPONENT / "translations" / "en.json"


def _resolve_options(node: ast.AST, assignments: dict[str, ast.AST] | None = None) -> set[str] | None:
    """Resolve a SelectSelectorConfig ``options=`` argument to its string values.

    Accepts the options written either **inline** (``options=[...]``,
    ``options=list(DICT.keys())``) or **via a local variable**
    (``dm_opts = [...]`` then ``options=dm_opts``). In the variable case the
    name is looked up in ``assignments`` (name -> assigned value node, collected
    from the module) and resolved recursively.

    Returns ``None`` when the expression cannot be resolved statically, so the
    caller can fail loudly rather than pass a false negative.
    """
    assignments = assignments or {}
    # options=dm_opts -> follow the variable assignment and resolve its value.
    if isinstance(node, ast.Name):
        # A bare name may be a constant from const (e.g. a list/dict)...
        if hasattr(const, node.id):
            resolved = getattr(const, node.id)
            if isinstance(resolved, dict):
                return set(resolved.keys())
            if isinstance(resolved, (list, tuple, set)):
                return set(resolved)
        # ...or a local/module variable assigned an inline expression.
        target = assignments.get(node.id)
        return _resolve_options(target, assignments) if target is not None else None
    # options=[CONST_A, CONST_B] or [SelectOptionDict(value=CONST, ...), ...]
    if isinstance(node, ast.List):
        values: set[str] = set()
        for elt in node.elts:
            if isinstance(elt, ast.Name):
                values.add(getattr(const, elt.id))
            elif isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                values.add(elt.value)
            elif isinstance(elt, ast.Call):
                # SelectOptionDict(value=..., label=...)
                value_kw = next((kw for kw in elt.keywords if kw.arg == "value"), None)
                if value_kw is None:
                    return None
                if isinstance(value_kw.value, ast.Name):
                    values.add(getattr(const, value_kw.value.id))
                elif isinstance(value_kw.value, ast.Constant):
                    values.add(value_kw.value.value)
                else:
                    return None
            else:
                return None
        return values
    # options=list(PLANT_FAMILIES.keys()) / list(PLANT_FAMILIES) etc.
    if isinstance(node, ast.Call):
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and hasattr(const, sub.id):
                resolved = getattr(const, sub.id)
                if isinstance(resolved, dict):
                    return set(resolved.keys())
                # options=list(ET_METHOD_OPTIONS): a flat sequence in const,
                # wrapped because Home Assistant validates this field as a
                # list and refuses the tuple it is stored as.
                if isinstance(resolved, (list, tuple, set)):
                    return set(resolved)
    # options=[SelectOptionDict(value=k, ...) for k, v in PLANT_FAMILIES.items()]
    if isinstance(node, ast.ListComp):
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and hasattr(const, sub.id):
                resolved = getattr(const, sub.id)
                if isinstance(resolved, dict):
                    return set(resolved.keys())
    return None


def _collect_assignments(tree: ast.AST) -> dict[str, ast.AST]:
    """Map ``name -> assigned value node`` for simple ``name = <expr>`` statements.

    Lets ``_resolve_options`` follow ``options=dm_opts`` back to ``dm_opts = [...]``.
    Module-wide, last assignment wins — enough for the option-list variables the
    config flow uses (each assigned once inside its step).
    """
    assignments: dict[str, ast.AST] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            assignments[node.targets[0].id] = node.value
    return assignments


def _collect_translation_keyed_selectors() -> list[tuple[str, set[str] | None]]:
    tree = ast.parse(_CONFIG_FLOW.read_text())
    assignments = _collect_assignments(tree)
    out: list[tuple[str, set[str] | None]] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "SelectSelectorConfig"
        ):
            continue
        tk_kw = next((kw for kw in node.keywords if kw.arg == "translation_key"), None)
        if tk_kw is None or not isinstance(tk_kw.value, ast.Constant):
            continue
        opt_kw = next((kw for kw in node.keywords if kw.arg == "options"), None)
        options = _resolve_options(opt_kw.value, assignments) if opt_kw is not None else None
        out.append((tk_kw.value.value, options))
    return out


def test_translation_keys_have_matching_options_in_en_json():
    """Each translation_key selector must have all its options translated."""
    selectors = _collect_translation_keyed_selectors()
    if not selectors:
        # Config flow uses inline labels — nothing to validate (guard is dormant).
        return

    en = json.loads(_EN_JSON.read_text())
    selector_section = en.get("selector", {})

    errors: list[str] = []
    for translation_key, options in selectors:
        entry = selector_section.get(translation_key)
        if entry is None:
            errors.append(f"translation_key '{translation_key}' missing from en.json 'selector' section")
            continue
        translated = set(entry.get("options", {}).keys())
        if options is None:
            errors.append(
                f"translation_key '{translation_key}': options could not be resolved statically — "
                "extend _resolve_options() in this test"
            )
            continue
        missing = options - translated
        if missing:
            errors.append(f"translation_key '{translation_key}' missing option labels in en.json: {sorted(missing)}")

    assert not errors, "Selector translation inconsistencies:\n  " + "\n  ".join(errors)


def _options_of(src: str) -> set[str] | None:
    """Resolve the ``options=`` of the first SelectSelectorConfig in a snippet."""
    tree = ast.parse(src)
    assignments = _collect_assignments(tree)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "SelectSelectorConfig"
        ):
            opt = next((kw for kw in node.keywords if kw.arg == "options"), None)
            return _resolve_options(opt.value, assignments) if opt is not None else None
    return None


class TestResolveOptionsFromVariable:
    """_resolve_options must follow options passed via a local variable."""

    def test_variable_holding_const_list(self):
        src = (
            "dm_opts = [DELIVERY_MODE_ESTIMATED_FLOW, DELIVERY_MODE_FLOW_METER]\n"
            "selector.SelectSelectorConfig(options=dm_opts, translation_key='delivery_mode')\n"
        )
        assert _options_of(src) == {const.DELIVERY_MODE_ESTIMATED_FLOW, const.DELIVERY_MODE_FLOW_METER}

    def test_variable_holding_dict_keys_call(self):
        src = (
            "pf_opts = list(PLANT_FAMILIES.keys())\n"
            "selector.SelectSelectorConfig(options=pf_opts, translation_key='plant_family')\n"
        )
        assert _options_of(src) == set(const.PLANT_FAMILIES.keys())

    def test_inline_list_still_resolves(self):
        src = "selector.SelectSelectorConfig(options=[DELIVERY_MODE_FLOW_METER], translation_key='x')\n"
        assert _options_of(src) == {const.DELIVERY_MODE_FLOW_METER}

    def test_unknown_variable_returns_none(self):
        src = "selector.SelectSelectorConfig(options=mystery, translation_key='x')\n"
        assert _options_of(src) is None


# ── Sectioned form fields: labels must be section-qualified ────────────────


_STRINGS = _COMPONENT / "strings.json"
_IT_JSON = _COMPONENT / "translations" / "it.json"


def _section_membership() -> dict[str, set[str]]:
    """Map each ``section()`` key in ``config_flow.py`` to the fields it holds.

    Parsed statically: a section appears as ``vol.Required(SECTION_X): section(
    vol.Schema({vol.Optional(CONF_Y): ...}))``, so the section constant is the
    dict key and the fields are the ``vol.Required``/``vol.Optional`` keys of
    the nested schema. Constants resolve through ``const`` (the ``CONF_*``) and
    through this module's own assignments (the ``SECTION_*``).
    """
    tree = ast.parse(_CONFIG_FLOW.read_text())
    section_values = {
        name: node.value
        for name, node in _collect_assignments(tree).items()
        if name.startswith("SECTION_") and isinstance(node, ast.Constant)
    }

    def field_names(call: ast.Call) -> set[str]:
        fields: set[str] = set()
        for inner in ast.walk(call):
            if not (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute)):
                continue
            if inner.func.attr not in ("Required", "Optional") or not inner.args:
                continue
            arg = inner.args[0]
            if isinstance(arg, ast.Name) and arg.id.startswith("CONF_"):
                fields.add(getattr(const, arg.id))
        return fields

    membership: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values, strict=False):
            is_section_call = isinstance(value, ast.Call) and getattr(value.func, "id", None) == "section"
            if not is_section_call or not isinstance(key, ast.Call) or not key.args:
                continue
            name = key.args[0]
            if not (isinstance(name, ast.Name) and name.id in section_values):
                continue
            membership.setdefault(section_values[name.id], set()).update(field_names(value))
    return membership


def test_sectioned_fields_are_labelled_under_their_section():
    """A field inside a ``section()`` needs its label at the section-qualified path.

    Home Assistant looks a sectioned field's label up under
    ``step.<id>.sections.<section>.data.<field>``. A label left at the step's own
    ``data`` is never read: the frontend falls back to the raw key, so the form
    shows ``area_m2`` and ``flow_rate_lpm`` — in *every* language, translated
    files included. Fields whose key happens to read like a word (``valve``,
    ``name``) hide the breakage, which is why it survived a release.

    This is the sectioned-form twin of the selector guard above: the schema is
    correct, the strings exist, and only the *path* between them is wrong — which
    no other test and no hassfest check looks at.
    """
    membership = _section_membership()
    assert membership, "no section() found in config_flow.py — this guard would pass vacuously"

    errors: list[str] = []
    for path in (_STRINGS, _EN_JSON, _IT_JSON):
        doc = json.loads(path.read_text())
        for scope in ("config", "options"):
            for step_id, step in doc.get(scope, {}).get("step", {}).items():
                declared = step.get("sections")
                if not declared:
                    continue
                step_data = set(step.get("data", {}))
                for section_key, fields in membership.items():
                    if section_key not in declared:
                        continue
                    labelled = set(declared[section_key].get("data", {}))
                    missing = fields - labelled
                    if missing:
                        errors.append(
                            f"{path.name} {scope}/{step_id}: section '{section_key}' "
                            f"missing labels for {sorted(missing)}"
                        )
                    stranded = fields & step_data
                    if stranded:
                        errors.append(
                            f"{path.name} {scope}/{step_id}: {sorted(stranded)} labelled at step level "
                            f"but rendered inside section '{section_key}' — the frontend will not find them"
                        )

    assert not errors, "Sectioned-form label inconsistencies:\n  " + "\n  ".join(errors)


def test_every_select_options_argument_is_a_list():
    """Home Assistant validates ``options`` as a list, and a tuple is refused outright.

    This has to be a static check because the suite stubs Home Assistant: the
    selectors are mocks here, so nothing validates their config and a form that
    cannot open in the real thing passes every test in this repository. It got
    through exactly that way — ``options`` was handed the tuple the identifiers
    are stored as in ``const``, and the options form raised on open while 1285
    tests stayed green.

    A local variable is followed to its assignment, because that is how most of
    these are written. What is refused is a value that reaches ``options`` as a
    tuple or a set, whether written there or imported from ``const``.
    """
    tree = ast.parse(_CONFIG_FLOW.read_text(encoding="utf-8"))
    assignments = _collect_assignments(tree)
    offenders: list[str] = []

    def _is_list_shaped(node: ast.AST, depth: int = 0) -> bool:
        if depth > 3:
            return False
        if isinstance(node, (ast.List, ast.ListComp)):
            return True
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "list":
            return True
        if isinstance(node, ast.Name):
            if hasattr(const, node.id):
                return isinstance(getattr(const, node.id), list)
            target = assignments.get(node.id)
            return _is_list_shaped(target, depth + 1) if target is not None else False
        return False

    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "SelectSelectorConfig"
        ):
            continue
        options = next((kw.value for kw in node.keywords if kw.arg == "options"), None)
        if options is None:
            continue
        if not _is_list_shaped(options):
            offenders.append(f"line {options.lineno}: options={ast.unparse(options)}")

    assert not offenders, "SelectSelectorConfig options must be a list (HA refuses a tuple):\n  " + "\n  ".join(
        offenders
    )
