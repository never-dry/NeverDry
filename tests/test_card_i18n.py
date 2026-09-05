"""Consistency guards for the *other* GUI: the Lovelace card.

NeverDry puts text in front of a person through two surfaces, and they do not share a
mechanism:

* the **backend GUI** — config flow, options flow, entity names — is served by Home
  Assistant from ``translations/<lang>.json``, with English loaded first and the requested
  language overlaid on top, so a missing key falls back to English (see
  ``test_translation_consistency.py``);
* the **cards** — ``never-dry-zone-card`` and ``never-dry-model-card``, both defined in
  ``www/never-dry-zone-card.js`` — are JavaScript handed to the browser. They cannot read the
  integration's translation files, so they share one pair of dictionaries, ``I18N`` and
  ``VALVE_STATE_I18N``, read through ``t()`` and ``valveStateLabel()``. One dictionary for two
  cards is why a language is added once and covers both: the ``model*`` and ``measured_*`` /
  ``derived_*`` keys belong to the model card, the rest to the zone card.

The card is the more fragile of the two. Its lookup ends
``(I18N[lang] && I18N[lang][key]) || I18N.en[key] || key`` — on the **raw key**. Where the
backend degrades to English, the card degrades to ``expDeepShade`` printed at the user.

The card is also *mixed*: everything entity-backed (``friendly_name``,
``formatEntityState``) comes down the backend chain and follows the user's language on its
own, while the static text does not. The two halves can end up speaking different languages
inside the same tile — which is exactly what a German ``de.json`` with an English-only card
produces, and the reason the language check below is a gate rather than a warning.

Everything here is read statically from the source. No JavaScript is executed.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_COMPONENT = Path(__file__).resolve().parent.parent / "custom_components" / "never_dry"
_CARD = _COMPONENT / "www" / "never-dry-zone-card.js"
_VALVE_FSM = _COMPONENT / "valve_fsm.py"
_LANG_DOCS = sorted(_COMPONENT.glob("translations/*.json"))


def _object_literal(src: str, name: str) -> str:
    """Return the body of ``const <name> = { ... }`` by matching braces.

    Quoted strings are skipped while counting, so a brace inside a label cannot end the
    object early. A regex would have to choose between stopping at the first ``}`` (wrong,
    the dictionaries are nested) and greedily running to the last one (wrong, it would
    swallow the next declaration).
    """
    start = src.index(f"const {name} = {{") + len(f"const {name} = ")
    depth, i, quote = 0, start, ""
    while i < len(src):
        ch = src[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = ""
        elif ch in "\"'`":
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
        i += 1
    raise AssertionError(f"unbalanced braces while reading {name} from {_CARD.name}")


def _dictionary(name: str) -> dict[str, set[str]]:
    """``{language: {key, ...}}`` for one of the card's translation objects."""
    body = _object_literal(_CARD.read_text(encoding="utf-8"), name)
    out: dict[str, set[str]] = {}
    for match in re.finditer(r"\n  (\w+): \{", body):
        block = _object_literal("const x = " + body[match.end() - 1 :], "x")
        out[match.group(1)] = set(re.findall(r"\n\s+(\w+):", block))
    return out


def _valve_states() -> set[str]:
    """The values of the ``ValveState`` enum, read from ``valve_fsm.py`` without importing it."""
    tree = ast.parse(_VALVE_FSM.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "ValveState":
            return {
                stmt.value.value
                for stmt in node.body
                if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Constant)
            }
    raise AssertionError("ValveState not found in valve_fsm.py")


def test_card_covers_every_shipped_language():
    """A language shipped in ``translations/`` must also exist in the card.

    This is the gate. Half a GUI in German and half in English is worse than no German at
    all: the form speaks the user's language, the card underneath it does not, and there is
    nothing in the product that explains why. Adding ``<lang>.json`` is therefore not a
    self-contained contribution — it is a commitment across both surfaces, and this test is
    where that gets said out loud instead of discovered by a user.
    """
    shipped = {path.stem for path in _LANG_DOCS}
    assert shipped, "no translations/*.json found — this guard would pass vacuously"

    problems = [
        f"{name}: missing {sorted(shipped - set(_dictionary(name)))}"
        for name in ("I18N", "VALVE_STATE_I18N")
        if shipped - set(_dictionary(name))
    ]
    assert not problems, (
        f"the card does not speak every language the integration ships ({sorted(shipped)}):\n  " + "\n  ".join(problems)
    )


def test_card_dictionaries_agree_across_languages():
    """Within a dictionary, every language carries the same keys as English.

    The card's fallback ends on the raw key, so a hole here is not degraded text — it is an
    identifier printed in the interface.
    """
    problems: list[str] = []
    for name in ("I18N", "VALVE_STATE_I18N"):
        table = _dictionary(name)
        english = table["en"]
        for language, keys in sorted(table.items()):
            if language == "en":
                continue
            for key in sorted(english - keys):
                problems.append(f"{name}.{language}: missing '{key}' — the raw key is what renders")
            for key in sorted(keys - english):
                problems.append(f"{name}.{language}: '{key}' has no English counterpart")
    assert not problems, "card dictionaries have drifted:\n  " + "\n  ".join(problems)


def test_valve_state_labels_cover_the_state_machine():
    """``VALVE_STATE_I18N`` must name every ``ValveState``, and no more.

    The card renders whatever state the machine reports. A state added to ``valve_fsm.py``
    without a label here reaches the user as its identifier — ``stuck_open`` in a tile that
    otherwise reads in whole words. The reverse direction catches the label left behind
    after a state is removed.
    """
    states = _valve_states()
    labelled = _dictionary("VALVE_STATE_I18N")["en"]
    assert not (states - labelled), f"ValveState members with no card label: {sorted(states - labelled)}"
    assert not (labelled - states), f"card labels for states that no longer exist: {sorted(labelled - states)}"


def test_statically_referenced_keys_exist():
    """Keys reached through ``t(hass, "…")`` or ``i18n: "…"`` must exist in ``I18N.en``.

    A **partial** guard, deliberately. Only the two directly greppable call shapes are
    checked; other keys arrive through lookup tables (``deep_shade: ["expDeepShade", …]``)
    that cannot be resolved without either running the file or accepting false positives.
    So this is a ratchet on the common case, not proof that every key resolves — the
    dictionary defines far more keys than are reachable from here.
    """
    src = _CARD.read_text(encoding="utf-8")
    referenced = set(re.findall(r't\(hass,\s*"(\w+)"', src)) | set(re.findall(r'i18n:\s*"(\w+)"', src))
    assert referenced, "no t()/i18n call sites found — the extractor has drifted from the card"

    missing = sorted(referenced - _dictionary("I18N")["en"])
    assert not missing, f"keys used by the card but absent from I18N.en: {missing}"
