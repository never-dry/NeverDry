"""What the declared minimum Home Assistant version actually supports.

Until this existed, the minimum was a number in two files and nothing checked
it: the suite stubs Home Assistant, so a schema Home Assistant would reject at
that version passed every test and then failed in a user's form.

That is not hypothetical. The unit of the ET coefficient box needed
``translation_key`` on a number selector, which Home Assistant only accepts from
2025.8 - found by a contributor reading the schema by hand, because nothing here
could tell her. These tests are that reading, automated.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent


def _declared_minimum() -> str:
    return json.loads((_ROOT / "hacs.json").read_text(encoding="utf-8"))["homeassistant"]


def test_the_installed_version_is_the_one_we_claim_to_support():
    """The job installs the minimum; if it drifts, every result below is about
    some other version and would quietly mean nothing."""
    from homeassistant.const import __version__ as installed

    declared = _declared_minimum()
    assert installed.startswith(declared.rsplit(".", 1)[0]), (
        f"these tests are meaningful only against the declared minimum: hacs.json says {declared}, "
        f"the environment has {installed}"
    )


def test_hacs_and_the_manual_agree_on_the_minimum():
    """Two files stated two different minimums for months, and neither was
    checked against anything."""
    manual = (_ROOT / "docs" / "user_manual.md").read_text(encoding="utf-8")
    declared = _declared_minimum()
    assert f"**Home Assistant** {declared} or newer" in manual, (
        f"hacs.json declares {declared}; the user manual must say the same"
    )


def test_every_config_flow_schema_builds():
    """The whole point: a selector this version rejects raises here rather than
    in somebody's browser."""
    from never_dry import config_flow

    config_flow._sensors_schema(is_imperial=False)
    config_flow._sensors_schema(is_imperial=True)
    config_flow._model_params_schema(is_imperial=False, current={})
    config_flow._model_params_schema(is_imperial=True, current={})


def test_the_translated_unit_reaches_the_minimum_without_the_fallback():
    """The fallback exists for installs below the declared minimum. At the
    minimum itself the real path must be the one taken - otherwise the unit is
    silently English for everyone and no test would say so."""
    from never_dry.config_flow import _alpha_selector

    built = _alpha_selector()
    config = built.config

    assert config.get("translation_key") == "et_coefficient", (
        "at the declared minimum the selector must carry its translation key; "
        "falling back here means the unit is untranslated for every user"
    )
    assert config.get("unit_of_measurement") == "mm_per_celsius_per_day"


def test_the_catalogue_carries_the_unit_the_selector_asks_for():
    """A key the selector names and the catalogue lacks shows the raw slug."""
    strings = json.loads((_ROOT / "custom_components" / "never_dry" / "strings.json").read_text(encoding="utf-8"))
    unit = strings["selector"]["et_coefficient"]["unit_of_measurement"]

    assert "mm_per_celsius_per_day" in unit, "the selector's slug must exist in the catalogue"

    translations = _ROOT / "custom_components" / "never_dry" / "translations"
    missing = [
        path.stem
        for path in sorted(translations.glob("*.json"))
        if "mm_per_celsius_per_day"
        not in json.loads(path.read_text(encoding="utf-8"))
        .get("selector", {})
        .get("et_coefficient", {})
        .get("unit_of_measurement", {})
    ]
    assert not missing, f"these languages would show the raw slug to a user: {missing}"


@pytest.mark.parametrize("module", ["sensor", "controller", "valve_notifier", "config_flow", "water_balance_model"])
def test_the_integration_imports_against_a_real_home_assistant(module):
    """Import is the cheapest check there is and the suite cannot do it: with
    Home Assistant stubbed, an import of something Home Assistant removed
    succeeds against the stub."""
    __import__(f"never_dry.{module}")
