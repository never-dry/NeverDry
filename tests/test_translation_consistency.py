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
_STRINGS = _COMPONENT / "strings.json"
# The shipped languages are *discovered*, never listed. A hard-coded tuple is exactly how
# a new translations/<lang>.json slips past every guard in this file on the day it lands:
# the file is present, the tests are green, and nobody is looking at it.
_LANG_DOCS = sorted(_COMPONENT.glob("translations/*.json"))
_ALL_DOCS = [_STRINGS, *_LANG_DOCS]


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
    for path in _ALL_DOCS:
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


# ── Error codes must resolve in the flow that raises them ─────────────
#
# An error code is not text: Home Assistant looks it up under
# ``<root>.error.<code>``, where the root is ``config`` for the setup wizard and
# ``options`` for the Settings dialogs. A code with no entry there is not a
# missing translation that falls back to English — the raw code is printed at
# the user, which is what "flow_rate_required" looked like in the field.
#
# The two roots drifted because the delivery-mode checks lived only in the setup
# step for a long time, so only ``config.error`` ever needed them. The moment
# the same helper started serving the options steps too (GH #196), every code it
# can raise had to resolve in both — and nothing was checking.


def _raised_error_codes() -> set[str]:
    """Every error code ``config_flow.py`` can hand to a form."""
    from never_dry import config_flow as cf

    tree = ast.parse(_CONFIG_FLOW.read_text())
    codes: set[str] = set()

    for node in ast.walk(tree):
        # _add_field_error(errors, FIELD, "code")
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_add_field_error"
            and len(node.args) == 3
            and isinstance(node.args[2], ast.Constant)
        ):
            codes.add(node.args[2].value)
        # errors["base"] = "code" / errors[FIELD] = "code"
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for target in node.targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "errors"
                    and isinstance(node.value.value, str)
                ):
                    codes.add(node.value.value)
        # errors={"base": "code"} passed straight to async_show_form
        if isinstance(node, ast.keyword) and node.arg == "errors" and isinstance(node.value, ast.Dict):
            for value in node.value.values:
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    codes.add(value.value)
        # The ET-method validator returns its code.
        if isinstance(node, ast.FunctionDef) and node.name == "_et_method_error":
            for inner in ast.walk(node):
                if isinstance(inner, ast.Return) and isinstance(inner.value, ast.Constant) and inner.value.value:
                    codes.add(inner.value.value)

    # The preset/override codes travel in a table rather than at the call site.
    codes.update(pair[4] for pair in cf.PRESET_OVERRIDE_PAIRS)
    return codes


def test_every_raised_error_code_resolves_in_both_flows():
    """A code with no entry is printed raw at the user, not translated late."""
    codes = _raised_error_codes()
    assert codes, "no error codes found — the extractor has drifted from the source"

    missing: list[str] = []
    for path in _ALL_DOCS:
        data = json.loads(path.read_text())
        for root in ("config", "options"):
            declared = data.get(root, {}).get("error", {})
            missing += [f"{path.name}: {root}.error.{code}" for code in sorted(codes) if code not in declared]

    assert not missing, "error codes with no entry to resolve to:\n  " + "\n  ".join(missing)


def test_the_two_flows_declare_the_same_error_catalogue():
    """One set of helpers serves both flows, so one catalogue serves both roots.

    Divergence here is how a code ends up raisable somewhere it cannot be
    read, which is invisible until a user is standing in front of it.
    """
    for path in _ALL_DOCS:
        data = json.loads(path.read_text())
        config = set(data.get("config", {}).get("error", {}))
        options = set(data.get("options", {}).get("error", {}))
        assert config == options, (
            f"{path.name}: config.error and options.error have drifted — "
            f"only in config: {sorted(config - options)}; only in options: {sorted(options - config)}"
        )


# ── Internal vocabulary must not reach the user ───────────────────────
#
# A value like ``estimated_flow`` is an identifier. It has a translated label
# precisely because the user is not supposed to see the key — and then the
# error text said "required for estimated_flow delivery mode", naming in the
# message the very thing the dropdown had just spelt out in words. A tester
# read it back on 0.12.0-beta.3 and asked why the form spoke two languages.
#
# The rule is derived rather than listed: every option that has a label under
# ``selector.<key>.options`` is by construction an internal identifier, so its
# raw form must not appear in any label, description or error.


def _internal_option_keys(data: dict) -> set[str]:
    """Option values that have a translated label, so are identifiers.

    Only the ones carrying an underscore. A key like ``custom`` or ``auto`` is
    an ordinary English word, and banning it would flag "Pick Custom to enter
    your own" — the label doing its job.
    """
    keys: set[str] = set()
    for group in data.get("selector", {}).values():
        keys |= {k for k in group.get("options", {}) if "_" in k}
    return keys


def _user_facing_strings(data: dict):
    """Every string the form puts in front of a person, with where it lives."""
    for root in ("config", "options"):
        section = data.get(root, {})
        for code, text in section.get("error", {}).items():
            yield f"{root}.error.{code}", text
        for step_name, step in section.get("step", {}).items():
            blocks = [("", step)] + [(f"{s}.", body) for s, body in step.get("sections", {}).items()]
            for prefix, block in blocks:
                for kind in ("data", "data_description"):
                    for field, text in block.get(kind, {}).items():
                        yield f"{root}.{step_name}.{prefix}{kind}.{field}", text


def test_no_user_facing_string_names_an_internal_key():
    offenders: list[str] = []
    for path in _ALL_DOCS:
        data = json.loads(path.read_text())
        keys = _internal_option_keys(data)
        assert keys, f"{path.name}: no translated options found — the extractor has drifted"
        for where, text in _user_facing_strings(data):
            for key in sorted(keys):
                if key in text:
                    offenders.append(f"{path.name}: {where} says '{key}'")
    assert not offenders, "these read an identifier out loud; use the label the user already sees:\n  " + "\n  ".join(
        offenders
    )


def test_a_label_is_a_name_not_a_paragraph():
    """A label names the field; the explanation belongs in data_description.

    The Italian file had the whole design-flow-rate description pasted into the
    label slot — 493 characters where the form expects two words — so the
    field announced itself with a paragraph while every neighbour had a name.
    """
    offenders: list[str] = []
    for path in _ALL_DOCS:
        data = json.loads(path.read_text())
        for where, text in _user_facing_strings(data):
            if ".data." in where and len(text) > 90:
                offenders.append(f"{path.name}: {where} is {len(text)} characters")
    assert not offenders, "labels that are paragraphs:\n  " + "\n  ".join(offenders)


# ── Every shipped language carries every key ──────────────────────────
#
# Home Assistant loads English first and then overlays the requested language with
# ``component_cache.update(flat)`` (``homeassistant/helpers/translation.py``). ``update()``
# replaces only the keys it carries, so a key missing from ``it.json`` does **not** print
# raw — it prints the *English* text. The dialog still looks finished.
#
# That is what these two guard: not a broken form, a **half-translated one that nobody
# reports** because nothing looks wrong. The raw key only ever surfaces when English is
# missing it too, which is the case the sectioned-label guard above already covers.
#
# ``strings.json`` is the declared source of truth, and it is the file most at risk: Home
# Assistant never reads it at runtime for a custom integration (``translation.py`` only ever
# builds ``f"{language}.json"`` under ``translations/``), and the copy into
# ``translations/en.json`` is manual because custom components have no build script. A file
# nobody reads and no tool checks rots — and this one had, silently: three labels of
# ``options.step.model_params`` absent and two of its strings left at the previous wording,
# while the same step's ``en``/``it`` were current.


def _leaf_paths(doc, prefix: str = "") -> dict[str, object]:
    """Flatten a translation document into ``dotted.path -> leaf value``."""
    flat: dict[str, object] = {}
    if isinstance(doc, dict):
        for key, value in doc.items():
            flat.update(_leaf_paths(value, f"{prefix}.{key}" if prefix else key))
    else:
        flat[prefix] = doc
    return flat


def test_strings_json_and_en_json_are_the_same_document():
    """The source of truth and the English shipped to users must not drift apart.

    Keys **and** values. Nothing at runtime reconciles these two files, and no build step
    generates one from the other, so the only thing that can keep ``strings.json`` honest
    is this assertion. Without it the source quietly becomes a document describing a GUI
    that no longer exists — which is the state it was found in.
    """
    source = _leaf_paths(json.loads(_STRINGS.read_text(encoding="utf-8")))
    english = _leaf_paths(json.loads(_EN_JSON.read_text(encoding="utf-8")))

    problems = [f"only in strings.json: {key}" for key in sorted(set(source) - set(english))]
    problems += [f"only in en.json: {key}" for key in sorted(set(english) - set(source))]
    problems += [
        f"{key}: strings.json says {source[key]!r}, en.json says {english[key]!r}"
        for key in sorted(set(source) & set(english))
        if source[key] != english[key]
    ]

    assert not problems, "strings.json and translations/en.json have drifted:\n  " + "\n  ".join(problems)


def test_every_language_matches_the_source_of_truth():
    """Every ``translations/<lang>.json`` carries exactly the keys of ``strings.json``.

    A missing key is English text served inside somebody else's language. A key in excess is
    text written for a field that no longer exists — the residue of a rename, which is worth
    catching because it is the half that a translator cannot see from their side.

    Empty leaves are refused in the same pass: ``""`` satisfies key parity perfectly and
    renders as nothing at all, so it is precisely how a half-finished translation would walk
    through a key-set check.

    Every deviation is collected and reported together, grouped by file: a translator has to
    see the whole list at once, not discover it one failing run at a time.
    """
    assert _LANG_DOCS, "no translations/*.json found — this guard would pass vacuously"

    expected = set(_leaf_paths(json.loads(_STRINGS.read_text(encoding="utf-8"))))
    problems: list[str] = []

    for path in _LANG_DOCS:
        leaves = _leaf_paths(json.loads(path.read_text(encoding="utf-8")))
        for key in sorted(expected - set(leaves)):
            problems.append(f"{path.name}: missing {key} (the English text is served instead)")
        for key in sorted(set(leaves) - expected):
            problems.append(f"{path.name}: {key} is not in strings.json (stale after a rename?)")
        for key in sorted(set(leaves) & expected):
            value = leaves[key]
            if not isinstance(value, str) or not value.strip():
                problems.append(f"{path.name}: {key} is empty — it renders as nothing")

    assert not problems, "translations out of step with strings.json:\n  " + "\n  ".join(problems)
