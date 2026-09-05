"""The integration's Python must not read translation files. This keeps it that way.

Localisation belongs to the two presentation surfaces and to nothing else: Home Assistant
resolves ``translations/<lang>.json`` for the backend GUI, and the card carries its own
dictionaries for the browser. Neither is the integration's business — the Python side deals
in identifiers (``estimated_flow``, ``req_open``, error codes) and hands them out; the text
is somebody else's problem, resolved later, in the user's language, in a layer that knows
what language that is.

That separation holds today. This test is not here to establish it — it is here so that the
first module tempted to open a translation file to fish out a label has to argue with a
failing test first. A guard that currently passes trivially is the cheapest moment to write
it; the expensive moment is after something already depends on the coupling.

``manifest.json`` is the one file production code legitimately reads (``__init__.py`` takes
the version from it) and it is not a translation file, so it is out of scope by construction
rather than by exception.
"""

from __future__ import annotations

from pathlib import Path

_COMPONENT = Path(__file__).resolve().parent.parent / "custom_components" / "never_dry"

# Substrings that only appear when a module is reaching for translated text.
_FORBIDDEN = ("strings.json", "translations/", "translations\\")


def test_no_production_module_reads_translation_files():
    offenders: list[str] = []
    for module in sorted(_COMPONENT.rglob("*.py")):
        if "__pycache__" in module.parts:
            continue
        source = module.read_text(encoding="utf-8")
        for line_number, line in enumerate(source.splitlines(), start=1):
            for needle in _FORBIDDEN:
                if needle in line:
                    offenders.append(f"{module.relative_to(_COMPONENT)}:{line_number} mentions '{needle}'")

    assert not offenders, (
        "production code must not read translation files — emit identifiers and let the "
        "presentation layer resolve them:\n  " + "\n  ".join(offenders)
    )
