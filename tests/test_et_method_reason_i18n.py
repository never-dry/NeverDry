"""The ET method's reason text comes from the catalogue, not from Python (GH #279).

`_method_reason` and `warming_up_because` used to be English f-strings, invisible to
every catalogue and every non-English install — and the explicit-fallback sentence
quoted the raw config slug (`'hargreaves'`) instead of the name a user picked from the
dropdown. None of this could be seen until the model card itself could open on a
non-English install, which is what the first half of #279 fixed.

These tests exercise `_select_model` and `_warming_up_inputs` directly and check the
catalogue keys and their filled-in placeholders, the same way `test_et_method_choice.py`
already checks that a fallback is announced at all.
"""

from never_dry.const import CONF_ET_METHOD, CONF_RAIN_SENSOR, CONF_TEMP_SENSOR
from never_dry.sensor import DrynessIndexSensor
from never_dry.water_balance_model import DiurnalRange

BARE_SITE = {CONF_TEMP_SENSOR: "sensor.t", CONF_RAIN_SENSOR: "sensor.r"}


class TestMethodReason:
    def test_chosen_explicitly_and_it_ran(self, hass_mock):
        hub = DrynessIndexSensor(hass_mock, {**BARE_SITE, CONF_ET_METHOD: "et_simple"})
        assert hub.extra_state_attributes["et_method_reason"] == "Chosen explicitly"

    def test_explicit_fallback_names_the_method_a_user_picked_not_the_config_slug(self, hass_mock):
        # penman_monteith on a bare site (temperature + rain only) degrades to
        # hargreaves, same site test_et_method_choice.py uses for this — but what
        # the reason names is the method that was *asked for and refused*, not
        # the one it fell back to.
        hub = DrynessIndexSensor(hass_mock, {**BARE_SITE, CONF_ET_METHOD: "penman_monteith"})
        reason = hub.extra_state_attributes["et_method_reason"]
        assert "cannot run it" in reason
        assert "Penman-Monteith" in reason
        assert "penman_monteith" not in reason  # the config slug itself never leaks in

    def test_explicit_fallback_does_not_wrap_the_name_in_ascii_single_quotes(self, hass_mock):
        # hassfest refuses this shape in the catalogue (test_translation_consistency.py);
        # it would be just as wrong assembled by hand.
        hub = DrynessIndexSensor(hass_mock, {**BARE_SITE, CONF_ET_METHOD: "penman_monteith"})
        reason = hub.extra_state_attributes["et_method_reason"]
        assert "'Penman-Monteith'" not in reason

    def test_automatic_on_a_flat_sensor_names_the_recommended_method_and_the_range(self, hass_mock):
        hub = DrynessIndexSensor(hass_mock, BARE_SITE)
        hub._select_model(observed_range_c=1.0)
        reason = hub.extra_state_attributes["et_method_reason"]
        assert "1.0" in reason
        assert "Hargreaves-Samani" in reason

    def test_automatic_with_no_flat_reading_uses_the_plain_automatic_text(self, hass_mock):
        hub = DrynessIndexSensor(hass_mock, BARE_SITE)
        hub._select_model(observed_range_c=None)
        assert hub.extra_state_attributes["et_method_reason"] == (
            "Automatic: the best method the declared sensors support"
        )


class TestCachedCommonFallback:
    def test_an_unknown_key_returns_the_key_itself_not_english_pretending_to_be_a_translation(self, hass_mock):
        hub = DrynessIndexSensor(hass_mock, BARE_SITE)
        assert hub._cached_common("does_not_exist_in_any_catalogue") == "does_not_exist_in_any_catalogue"

    def test_placeholders_are_filled_in(self, hass_mock):
        hub = DrynessIndexSensor(hass_mock, BARE_SITE)
        assert hub._cached_common("warming_up_daily_range", hours=20) == "The daily range needs 20 hours of readings"


class TestTranslatedMethodName:
    def test_strips_the_dropdown_gloss(self, hass_mock):
        hub = DrynessIndexSensor(hass_mock, BARE_SITE)
        assert hub._translated_method_name("hargreaves") == "Hargreaves-Samani"

    def test_an_unknown_method_id_falls_back_to_itself_rather_than_crashing(self, hass_mock):
        hub = DrynessIndexSensor(hass_mock, BARE_SITE)
        assert hub._translated_method_name("not_a_real_method") == "not_a_real_method"


class TestWarmingUpBecause:
    def test_the_daily_range_variant_names_the_required_hours(self, hass_mock):
        hub = DrynessIndexSensor(hass_mock, BARE_SITE)
        inputs = hub._warming_up_inputs(temp_c=20.0)
        assert inputs["warming_up_because"] == f"The daily range needs {DiurnalRange.MIN_COVERAGE_H} hours of readings"
