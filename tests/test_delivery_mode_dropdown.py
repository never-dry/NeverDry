"""The delivery-mode dropdown must name what its own description promised (#309, #310).

The field's description introduces the three modes by name — "Estimated flow",
"Flow meter", "Volume preset" — and a user who reads it goes looking for that name in
the dropdown right below. Two of the three were always findable there, because the
distinctive noun sat inside the entry. `estimated_flow` was not, in any of the four
shipped languages, because every translation faithfully reproduced an English source
that already had the gap (#309). Its entry also kept `On/Off` untranslated in Spanish
and Italian after that fix, which is `#310` — German had already translated it, which
settled that the word travels.

This is a narrower, permanent version of the read-back that found both: for each
language, does the mode's own dropdown entry contain the word its description used to
introduce it?
"""

import json
from pathlib import Path

import pytest

_COMPONENT = Path(__file__).resolve().parent.parent / "custom_components" / "never_dry"
_CATALOGUES = {"en": "en", "es": "es", "de": "de", "it": "it"}

# The word a reader would look for, per language, per mode — taken from each
# language's own field description (config.step.zone.sections.valve_and_pipe
# .data_description.delivery_mode), not invented here.
_FINDABLE_WORD = {
    "estimated_flow": {
        "en": "Estimated flow",
        "es": "Caudal estimado",
        "de": "Geschätzter Durchfluss",
        "it": "Portata stimata",
    },
    "flow_meter": {"en": "flow meter", "es": "caudalímetro", "de": "Durchfluss-Sensor", "it": "flussometro"},
    "volume_preset": {"en": "volume", "es": "volumen", "de": "Volumenvorgabe", "it": "volume"},
}


def _options(lang: str) -> dict:
    doc = json.loads((_COMPONENT / f"translations/{lang}.json").read_text(encoding="utf-8"))
    return doc["selector"]["delivery_mode"]["options"]


@pytest.mark.parametrize("mode", ["estimated_flow", "flow_meter", "volume_preset"])
@pytest.mark.parametrize("lang", list(_CATALOGUES))
def test_the_dropdown_entry_contains_the_name_its_description_gave_the_mode(lang, mode):
    entry = _options(lang)[mode]
    word = _FINDABLE_WORD[mode][lang]
    assert word.lower() in entry.lower(), f"{lang}/{mode}: {word!r} not findable in {entry!r}"


@pytest.mark.parametrize("lang", ["es", "it"])
def test_on_off_is_translated_not_left_as_the_english_slash_pair(lang):
    entry = _options(lang)["estimated_flow"]
    assert "on/off" not in entry.lower(), f"{lang}: still carries the English 'On/Off' — {entry!r}"


def test_strings_json_matches_the_english_catalogue():
    # strings.json is the source hassfest validates; en.json is what ships. #309's
    # fix has to land in both or the mismatch just moves from the dropdown to here.
    strings = json.loads((_COMPONENT / "strings.json").read_text(encoding="utf-8"))
    assert strings["selector"]["delivery_mode"]["options"]["estimated_flow"] == _options("en")["estimated_flow"]
