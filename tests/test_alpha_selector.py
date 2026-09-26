"""The ET coefficient's unit is translated where Home Assistant can, and safe where it cannot.

A number selector can carry a ``translation_key`` (Home Assistant 2025.8), and the
unit is then looked up under ``selector.<key>.unit_of_measurement.<unit>``. Two
constraints shape the helper:

- Before 2025.8 the selector's schema refuses the key outright, so the form would
  fail to open on an install still inside the range ``hacs.json`` declares. The
  helper therefore tries the key and falls back to the plain selector, which keeps
  the literal ``mm/°C/day``.
- Hassfest only accepts a slug as that ``<unit>`` key, and ``mm/°C/day`` is not one.
  So the translated box carries a slug, and each catalogue turns it into the text
  the user reads.

These tests cannot run Home Assistant's real schema, so they stand one in front of
the helper and check what it does with each answer.
"""

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from never_dry import config_flow as cf

INTEGRATION = Path(__file__).parent.parent / "custom_components" / "never_dry"
CATALOGUES = ["strings.json"] + [f"translations/{lang}.json" for lang in ("en", "es", "de", "it")]
# script/hassfest/translations.py, RE_TRANSLATION_KEY: the rule a translation key must meet.
HASSFEST_KEY = re.compile(r"^(?!.+[_-]{2})(?![_-])[a-z0-9-_]+(?<![_-])$")


class _Rejected(Exception):
    """Stands for voluptuous.Invalid, which the test harness does not provide."""


def _selector_module(accepts_translation_key: bool):
    def number_selector(config):
        if "translation_key" in config and not accepts_translation_key:
            raise _Rejected("extra keys not allowed @ data['translation_key']")
        return SimpleNamespace(config=dict(config))

    return SimpleNamespace(
        NumberSelector=number_selector,
        NumberSelectorConfig=dict,
    )


@pytest.fixture
def helper(monkeypatch):
    def _with(accepts_translation_key: bool):
        monkeypatch.setattr(cf, "selector", _selector_module(accepts_translation_key))
        monkeypatch.setattr(cf.vol, "Invalid", _Rejected, raising=False)
        return cf._alpha_selector()

    return _with


def test_carries_the_translation_key_when_home_assistant_accepts_it(helper):
    assert helper(True).config["translation_key"] == "et_coefficient"


def test_the_translated_box_carries_a_key_hassfest_accepts(helper):
    assert HASSFEST_KEY.match(helper(True).config["unit_of_measurement"])


def test_keeps_the_literal_unit_when_home_assistant_rejects_the_key(helper):
    config = helper(False).config
    assert "translation_key" not in config
    assert config["unit_of_measurement"] == "mm/°C/day"


def test_both_paths_describe_the_same_box(helper):
    def shape(config):
        return {key: value for key, value in config.items() if key not in ("translation_key", "unit_of_measurement")}

    assert shape(helper(True).config) == shape(helper(False).config)


def _translated_unit(catalogue: str, config: dict) -> str:
    selectors = json.loads((INTEGRATION / catalogue).read_text(encoding="utf-8"))["selector"]
    return selectors[config["translation_key"]]["unit_of_measurement"][config["unit_of_measurement"]]


@pytest.mark.parametrize("catalogue", CATALOGUES)
def test_every_catalogue_translates_the_unit_the_selector_carries(helper, catalogue):
    assert _translated_unit(catalogue, helper(True).config).strip()


@pytest.mark.parametrize("catalogue", ["strings.json", "translations/en.json"])
def test_english_shows_the_same_unit_as_the_fallback(helper, catalogue):
    assert _translated_unit(catalogue, helper(True).config) == helper(False).config["unit_of_measurement"]
