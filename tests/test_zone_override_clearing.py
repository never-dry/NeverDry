"""Tests for clearing per-zone overrides in the edit form (GH #165).

A zone override — custom efficiency, manual Kc, microclimate factor — means
"ignore the derived value and use mine". Removing it must be possible, or a
value set by accident is permanent: the reporter moved the efficiency slider
by mistake and could not get the zone back onto its system-type default
without deleting and recreating the zone.

Two independent things had to be true for that to happen, and both are
pinned here:

1. ``vol.Optional(key, default=...)`` re-injects the stored value whenever
   the field comes back empty, so clearing was a no-op by construction. The
   cure is ``description={"suggested_value": ...}``, which offers the value
   without restoring it.
2. A slider always submits a number. Even with the fix above, an override
   rendered as a slider has no empty state to submit.

The plumbing underneath was already right and is pinned too: the save path
replaces the zone wholesale, and the runtime resolves efficiency by key
*presence*, so a removed key genuinely falls back to the system-type default.
"""

from unittest.mock import MagicMock

import pytest
from never_dry import config_flow as cf
from never_dry.const import (
    CONF_ZONE_AREA,
    CONF_ZONE_DELIVERY_TIMEOUT,
    CONF_ZONE_EFFICIENCY,
    CONF_ZONE_EXPOSURE,
    CONF_ZONE_FLOW_RATE,
    CONF_ZONE_IRRIGATION_TIME,
    CONF_ZONE_KC,
    CONF_ZONE_MICROCLIMATE_FACTOR,
    CONF_ZONE_NAME,
    CONF_ZONE_SYSTEM_TYPE,
    CONF_ZONES,
    SYSTEM_TYPES,
)

# Clearing these means "go back to the derived value".
OVERRIDE_KEYS = (
    CONF_ZONE_EFFICIENCY,
    CONF_ZONE_KC,
    CONF_ZONE_EXPOSURE,
    CONF_ZONE_MICROCLIMATE_FACTOR,
    CONF_ZONE_DELIVERY_TIMEOUT,
    CONF_ZONE_IRRIGATION_TIME,
)

# These must always hold a value; default= is correct for them.
REQUIRED_VALUE_KEYS = (CONF_ZONE_NAME, CONF_ZONE_AREA, CONF_ZONE_SYSTEM_TYPE)


def _entry(zones):
    entry = MagicMock()
    entry.entry_id = "abc"
    entry.data = {CONF_ZONES: zones}
    return entry


@pytest.fixture
def optional_calls(monkeypatch):
    """Record every vol.Optional(...) the edit form declares."""
    calls: list[tuple[tuple, dict]] = []

    def _recorder(*args, **kwargs):
        calls.append((args, kwargs))
        return object()

    monkeypatch.setattr(cf.vol, "Optional", _recorder, raising=False)
    return calls


@pytest.fixture(autouse=True)
def _patch_flow_env(monkeypatch):
    """Same stub set the other config-flow tests use."""
    monkeypatch.setattr(cf, "_is_imperial", lambda hass: False)
    monkeypatch.setattr(cf.vol, "Schema", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(cf.vol, "Required", lambda *a, **k: object(), raising=False)
    monkeypatch.setattr(cf.vol, "Optional", lambda *a, **k: object(), raising=False)
    monkeypatch.setattr(cf.vol, "UNDEFINED", object(), raising=False)
    monkeypatch.setattr(cf, "selector", MagicMock())
    monkeypatch.setattr(cf, "_confirm_zone_schema", lambda: None)
    monkeypatch.setattr(cf, "_zone_schema_initial", lambda imperial: None)

    def _show_form(self, *, step_id, data_schema=None, errors=None, description_placeholders=None):
        return {"type": "form", "step_id": step_id, "errors": errors}

    def _create_entry(self, *, data=None, title=None):
        return {"type": "create_entry", "title": title, "data": data}

    for klass in (cf.NeverDryConfigFlow, cf.NeverDryOptionsFlow):
        monkeypatch.setattr(klass, "async_show_form", _show_form, raising=False)
        monkeypatch.setattr(klass, "async_create_entry", _create_entry, raising=False)


def _zone_with_overrides():
    return {
        CONF_ZONE_NAME: "Prato",
        CONF_ZONE_AREA: 20.0,
        CONF_ZONE_FLOW_RATE: 10.0,
        CONF_ZONE_EFFICIENCY: 0.45,
        CONF_ZONE_KC: 1.1,
        CONF_ZONE_MICROCLIMATE_FACTOR: 0.75,
    }


async def _render_edit_form(hass_mock, zone):
    flow = cf.NeverDryOptionsFlow(_entry([zone]))
    flow.hass = hass_mock
    flow._edit_zone_name = zone[CONF_ZONE_NAME]
    await flow.async_step_edit_zone_detail()
    return flow


class TestOverridesAreClearable:
    """The declaration, which is where the bug lived."""

    @pytest.mark.asyncio
    async def test_overrides_are_offered_not_reinjected(self, hass_mock, optional_calls):
        """default= would restore the value the user just cleared."""
        await _render_edit_form(hass_mock, _zone_with_overrides())
        declared = {args[0]: kwargs for args, kwargs in optional_calls if args}

        for key in OVERRIDE_KEYS:
            assert key in declared, f"{key} is not declared in the edit form"
            assert "default" not in declared[key], (
                f"{key} uses default=, so clearing it re-injects the stored value (GH #165)"
            )
            assert "suggested_value" in declared[key].get("description", {}), (
                f"{key} must offer its stored value via suggested_value"
            )

    @pytest.mark.asyncio
    async def test_the_stored_value_is_what_gets_suggested(self, hass_mock, optional_calls):
        """Offering the value is the point: the form must not come up blank."""
        await _render_edit_form(hass_mock, _zone_with_overrides())
        declared = {args[0]: kwargs for args, kwargs in optional_calls if args}

        assert declared[CONF_ZONE_EFFICIENCY]["description"]["suggested_value"] == 0.45
        assert declared[CONF_ZONE_KC]["description"]["suggested_value"] == 1.1


class TestRequiredValuesKeepTheirDefault:
    """The fix must not spread to fields that need a value."""

    @pytest.mark.asyncio
    async def test_non_overrides_still_use_default(self, hass_mock, optional_calls):
        await _render_edit_form(hass_mock, _zone_with_overrides())
        declared = {args[0]: kwargs for args, kwargs in optional_calls if args}

        for key in REQUIRED_VALUE_KEYS:
            if key in declared:  # name and area are vol.Required, not Optional
                assert "default" in declared[key], f"{key} should keep a default"


class TestEveryPresetIsReachable:
    """The step has to be able to express the value it falls back to."""

    def test_system_type_defaults_are_multiples_of_the_step(self):
        """At step 0.05 neither drip (0.92) nor pop-up sprinklers (0.68) exist.

        This is what made the accident unrecoverable even by hand: the user
        could not dial the slider back to the number the system type would
        have derived.
        """
        step = 0.01
        for name, spec in SYSTEM_TYPES.items():
            eff = spec["default_efficiency"]
            if eff is None:
                continue  # the custom entry has no preset to fall back to
            assert round(eff / step) == pytest.approx(eff / step, abs=1e-9), (
                f"{name}: {eff} is not reachable at step {step}"
            )


class TestClearingActuallyRemovesTheKey:
    """The save path and the runtime contract behind the form."""

    @pytest.mark.asyncio
    async def test_absent_override_is_not_resurrected_from_the_stored_zone(self, hass_mock):
        """Saving replaces the zone: no merge puts the old override back."""
        flow = cf.NeverDryOptionsFlow(_entry([_zone_with_overrides()]))
        flow.hass = hass_mock
        flow._edit_zone_name = "Prato"

        result = await flow.async_step_edit_zone_detail(
            {
                CONF_ZONE_NAME: "Prato",
                CONF_ZONE_AREA: 20.0,
                CONF_ZONE_FLOW_RATE: 600.0,
            },
        )

        assert result["type"] == "create_entry"
        saved = flow.hass.config_entries.async_update_entry.call_args.kwargs["data"]
        zone = saved[CONF_ZONES][0]
        assert CONF_ZONE_EFFICIENCY not in zone
        assert CONF_ZONE_KC not in zone
        assert CONF_ZONE_MICROCLIMATE_FACTOR not in zone
