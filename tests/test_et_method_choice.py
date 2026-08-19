"""The form's answer when a method is picked that the sensors cannot support.

There are two ways to get this wrong, and only one of them is visible. The
loud way is refusing a method the site *can* run. The quiet way is accepting
one it cannot: setup succeeds, the model degrades to the simple tier at build
time, and the user is left believing they are running Penman-Monteith. Nothing
in the interface would say otherwise, and the number would look just as
confident.

So the check that matters is that the form and the runtime answer the same
question with the same rule — which is why the validator builds a real
``Environment`` and asks it, rather than reimplementing the match in
form-shaped code.
"""

from typing import ClassVar

from never_dry.config_flow import _et_method_error
from never_dry.const import (
    CONF_ET_METHOD,
    CONF_HUMIDITY_SENSOR,
    CONF_NET_RADIATION_SENSOR,
    CONF_RAIN_SENSOR,
    CONF_TEMP_MAX_SENSOR,
    CONF_TEMP_MIN_SENSOR,
    CONF_TEMP_SENSOR,
    CONF_VWC_SENSOR,
    CONF_WIND_SPEED_SENSOR,
    ET_METHOD_AUTO,
)

BARE_SITE = {CONF_TEMP_SENSOR: "sensor.t", CONF_RAIN_SENSOR: "sensor.r"}


def _fill_diurnal_window(hub, low=14.0, high=26.0):
    """Give the hub a full day of readings, anchored to the clock it actually reads.

    Anchoring matters: the hub observes at the real current hour, and the window
    prunes anything older than 24 of them. A test that observes at hours 0..23
    has its whole window discarded on the first real reading, and then exercises
    the warm-up fallback while appearing to test the tier — which is what this
    file did until the derived values were asserted and turned up empty.
    """
    from datetime import datetime

    now_h = datetime.now().timestamp() / 3600.0
    for offset in range(24):
        hub._diurnal.observe(now_h - offset, low if offset % 2 else high)


class TestAutomatic:
    """``auto`` is the promise to pick what the sensors allow, so it never fails."""

    def test_it_is_accepted_on_a_bare_site(self):
        assert _et_method_error({**BARE_SITE, CONF_ET_METHOD: ET_METHOD_AUTO}) is None

    def test_it_is_the_answer_when_the_field_is_absent_entirely(self):
        """An entry saved before this field existed must keep working untouched."""
        assert _et_method_error(BARE_SITE) is None


class TestRefusal:
    """A method whose inputs are missing is refused at the form, not at runtime."""

    def test_a_site_missing_the_sensors_is_refused_and_told_which(self):
        """Penman-Monteith on a site with only a thermometer: the answer names the
        gap, because that is a gap the user can close.
        """
        assert _et_method_error({**BARE_SITE, CONF_ET_METHOD: "penman_monteith"}) == "et_method_missing_sensors"

    def test_the_same_site_is_accepted_once_humidity_and_wind_are_declared(self):
        """Radiation is not required: without a pyranometer the incoming shortwave
        is estimated from the diurnal range, so the tier is reachable for a station
        that has the air measurements but no radiation instrument.
        """
        equipped = {
            **BARE_SITE,
            CONF_ET_METHOD: "penman_monteith",
            CONF_HUMIDITY_SENSOR: "sensor.h",
            CONF_WIND_SPEED_SENSOR: "sensor.w",
        }
        assert _et_method_error(equipped) is None

    def test_the_probe_model_without_a_probe_is_refused(self):
        assert _et_method_error({**BARE_SITE, CONF_ET_METHOD: "vwc_system"}) == "et_method_missing_sensors"

    def test_an_unknown_method_is_named_as_such(self):
        """A distinct error: nothing the user can add would ever satisfy it."""
        assert _et_method_error({**BARE_SITE, CONF_ET_METHOD: "no_such_method"}) == "et_method_unknown"


class TestAcceptance:
    """Declaring the sensors is the whole unlock — no other setting is involved."""

    def test_the_simple_tier_needs_only_a_thermometer(self):
        assert _et_method_error({**BARE_SITE, CONF_ET_METHOD: "et_simple"}) is None

    def test_the_probe_model_is_accepted_with_a_probe(self):
        assert _et_method_error({**BARE_SITE, CONF_ET_METHOD: "vwc_system", CONF_VWC_SENSOR: "sensor.vwc"}) is None

    def test_a_cleared_sensor_field_reads_as_absent_not_as_empty(self):
        """The options form sends nothing for a cleared picker; ``None`` must not satisfy."""
        cleared = {**BARE_SITE, CONF_ET_METHOD: "vwc_system", CONF_VWC_SENSOR: None}
        assert _et_method_error(cleared) == "et_method_missing_sensors"


class TestFormAndRuntimeAgree:
    """The guard for the drift this design can suffer and nothing else would show.

    Two places answer "can this site run this method": the form, so the user is
    told, and ``build_model``, so the right object runs. They are written once
    and called twice today — but nothing structural stops someone adding a
    special case to one of them, and the symptom would be silent. A method the
    form accepts and the builder declines produces a model the user did not
    choose, with no error anywhere.
    """

    SITES: ClassVar[dict] = {
        "bare": BARE_SITE,
        "with_probe": {**BARE_SITE, CONF_VWC_SENSOR: "sensor.vwc"},
        "with_extremes": {
            **BARE_SITE,
            CONF_TEMP_MAX_SENSOR: "sensor.tmax",
            CONF_TEMP_MIN_SENSOR: "sensor.tmin",
        },
        "full_weather": {
            **BARE_SITE,
            CONF_HUMIDITY_SENSOR: "sensor.h",
            CONF_WIND_SPEED_SENSOR: "sensor.w",
            CONF_NET_RADIATION_SENSOR: "sensor.rad",
        },
    }

    def _environment_for(self, site):
        from never_dry.environment import Environment

        return Environment(
            temperature_sensor=site.get(CONF_TEMP_SENSOR) or "",
            rain_sensor=site.get(CONF_RAIN_SENSOR) or "",
            soil_moisture_sensor=site.get(CONF_VWC_SENSOR),
            humidity_sensor=site.get(CONF_HUMIDITY_SENSOR),
            wind_speed_sensor=site.get(CONF_WIND_SPEED_SENSOR),
            net_radiation_sensor=site.get(CONF_NET_RADIATION_SENSOR),
            temp_max_sensor=site.get(CONF_TEMP_MAX_SENSOR),
            temp_min_sensor=site.get(CONF_TEMP_MIN_SENSOR),
        )

    def test_an_accepted_method_is_the_one_that_actually_runs(self):
        from never_dry.water_balance_model import MODEL_CATALOGUE, build_model

        checked = 0
        for site in self.SITES.values():
            for model in MODEL_CATALOGUE:
                accepted = _et_method_error({**site, CONF_ET_METHOD: model.method_id}) is None
                built = build_model(self._environment_for(site), method_id=model.method_id)
                if accepted:
                    assert isinstance(built, model), (
                        f"the form accepted {model.method_id} but the builder ran {type(built).__name__}"
                    )
                else:
                    assert not isinstance(built, model), (
                        f"the form refused {model.method_id} but the builder ran it anyway"
                    )
                checked += 1
        assert checked == len(self.SITES) * len(MODEL_CATALOGUE)

    def test_automatic_always_produces_something_runnable(self):
        """Whatever the site declares, ``auto`` must land on a model, never on nothing."""
        from never_dry.water_balance_model import WaterBalanceModel, build_model

        for site in self.SITES.values():
            assert isinstance(build_model(self._environment_for(site)), WaterBalanceModel)


class TestEveryOfferedMethodCanActuallyRun:
    """Offered means runnable, and only a real update can prove it.

    The earlier guard checked that a chosen method *builds*. That is not the
    same thing: both Hargreaves-Samani and Penman-Monteith built correctly and
    then raised on their first reading, because the hub fed them the reading it
    knows how to make rather than the one they consume. Construction was never
    the hard part.

    So this walks the dropdown and drives one real update per method. It is the
    cheapest possible end-to-end, and it is the test that would have caught both
    of the crashes found on the running instance.
    """

    def _hub(self, hass, method, **extra):
        from never_dry.sensor import DrynessIndexSensor

        return DrynessIndexSensor(
            hass,
            {
                CONF_TEMP_SENSOR: "sensor.t",
                CONF_RAIN_SENSOR: "sensor.r",
                CONF_ET_METHOD: method,
                **extra,
            },
        )

    #: What each method needs declared in order to actually run, rather than
    #: quietly degrade to the simple tier and prove nothing.
    SENSORS_FOR: ClassVar[dict] = {
        "et_simple": {},
        "hargreaves": {},
        "vwc_system": {CONF_VWC_SENSOR: "sensor.soil"},
        "penman_monteith": {CONF_HUMIDITY_SENSOR: "sensor.h", CONF_WIND_SPEED_SENSOR: "sensor.w"},
    }

    def test_each_offered_method_survives_an_update(self, hass_mock):
        """Drives a real update per method, on a site equipped for that method.

        The equipment matters: without it `build_model` degrades to the simple
        tier and the test passes while exercising nothing. Both crashes found on
        the live instance were in models that were never actually reached.
        """
        from unittest.mock import MagicMock

        from never_dry.const import ET_METHOD_AUTO, ET_METHOD_OPTIONS
        from never_dry.water_balance_model import model_by_id

        hass_mock.states.get = MagicMock(return_value=MagicMock(state="24.0"))
        for method in ET_METHOD_OPTIONS:
            if method == ET_METHOD_AUTO:
                continue
            hub = self._hub(hass_mock, method, **self.SENSORS_FOR[method])
            assert isinstance(hub._model, model_by_id(method)), f"{method} degraded instead of running"
            _fill_diurnal_window(hub)
            hub._on_sensor_change(MagicMock())  # must not raise
            assert hub._last_inputs, f"{method} never built a reading — it ran the warm-up fallback"

    def test_a_tier_that_needs_the_daily_range_waits_instead_of_guessing(self, hass_mock):
        """Before the window fills there is no honest reading, so the deficit freezes.

        The alternative is a partial range, which is systematically too small and
        reads as an overcast day — watering less than the garden needs, silently.
        """
        from unittest.mock import MagicMock

        hass_mock.states.get = MagicMock(return_value=MagicMock(state="24.0"))
        hub = self._hub(hass_mock, "hargreaves")
        before = hub._deficit

        hub._on_sensor_change(MagicMock())

        assert hub._deficit == before

    def test_a_probe_site_that_picks_the_simple_tier_still_works(self, hass_mock):
        """The disagreement the choice made possible, and the reason the branch moved.

        A declared probe used to be the only way to reach the VWC frame, so
        branching on the sensor and branching on the model were the same
        question. They are not any more, and the sensor answer feeds a moisture
        reading to a temperature model.
        """
        from unittest.mock import MagicMock

        from never_dry.water_balance_model import ETModel

        hass_mock.states.get = MagicMock(return_value=MagicMock(state="24.0"))
        hub = self._hub(hass_mock, "et_simple", **{CONF_VWC_SENSOR: "sensor.soil"})

        assert isinstance(hub._model, ETModel)
        hub._on_sensor_change(MagicMock())  # must not raise


class TestTheUserCanSeeWhichModelRuns:
    """Reading a deficit without knowing how it was computed is reading blind.

    The number means different things depending on its origin — estimated from
    temperature, computed from a full weather station, measured by a probe — so
    the method is not a diagnostic detail, it is the unit the number is in.

    And two behaviours make the configuration the wrong place to look: ``auto``
    has to resolve to something, and a stored choice whose sensors are gone
    degrades to what the site can still support. Both are cases where what runs
    is not what is written down.
    """

    def _hub(self, hass, **cfg):
        from never_dry.sensor import DrynessIndexSensor

        return DrynessIndexSensor(hass, {CONF_TEMP_SENSOR: "sensor.t", CONF_RAIN_SENSOR: "sensor.r", **cfg})

    def test_automatic_reports_what_it_resolved_to(self, hass_mock):
        hub = self._hub(hass_mock, **{CONF_ET_METHOD: "auto"})
        assert hub.active_method == "hargreaves"
        assert hub.extra_state_attributes["et_method"] == "hargreaves"
        assert hub.extra_state_attributes["et_method_configured"] == "auto"

    def test_a_flat_thermometer_is_explained_not_merely_overruled(self, hass_mock):
        """The answer alone is not actionable: "simple" tells a user nothing.

        The reason has to name the observation and say the choice can be
        overruled, because the statistic may be wrong about a site the user
        knows.
        """
        hub = self._hub(hass_mock, **{CONF_ET_METHOD: "auto"})
        hub._select_model(observed_range_c=0.8)

        assert hub.active_method == "et_simple"
        reason = hub.extra_state_attributes["et_method_reason"]
        assert "0.8" in reason
        assert "select it explicitly" in reason

    def test_a_probe_site_reports_the_probe_model(self, hass_mock):
        hub = self._hub(hass_mock, **{CONF_VWC_SENSOR: "sensor.soil"})
        assert hub.active_method == "vwc_system"

    def test_the_two_differ_when_a_choice_had_to_degrade(self, hass_mock):
        """The case that is invisible without this: asked for one thing, running another."""
        hub = self._hub(hass_mock, **{CONF_ET_METHOD: "penman_monteith"})
        assert hub.extra_state_attributes["et_method_configured"] == "penman_monteith"
        assert hub.active_method == "hargreaves"
        assert "cannot run it" in hub.extra_state_attributes["et_method_reason"]

    def test_the_entity_publishes_it_with_the_reason(self, hass_mock):
        from never_dry.sensor import WaterBalanceMethodSensor

        hub = self._hub(hass_mock, **{CONF_ET_METHOD: "auto"})
        entity = WaterBalanceMethodSensor(hub)

        assert entity.native_value == "hargreaves"
        attrs = entity.extra_state_attributes
        assert attrs["configured"] == "auto"
        assert attrs["reason"]
        assert attrs["reference_frame"] == "et"
        assert "temperature" in attrs["declared_sensors"]


class TestTheRateEntityAgreesWithTheModel:
    """One rate, published once. A second opinion on the same device is a bug.

    The entity computed the simple formula itself, which was true while that was
    the only model. Running Penman-Monteith it published 0.24 mm/h next to a
    deficit being integrated at 0.22 — a number nobody was using, on the device
    where the ones that matter live.
    """

    def test_it_reports_the_rate_the_hub_broadcast(self, hass_mock):
        from never_dry.sensor import DrynessIndexSensor, ETSensor

        cfg = {CONF_TEMP_SENSOR: "sensor.t", CONF_RAIN_SENSOR: "sensor.r"}
        hub = DrynessIndexSensor(hass_mock, cfg)
        entity = ETSensor(hass_mock, cfg, hub=hub)

        hub._broadcast_to_zones(1.0, 0.1234, 0.0)

        assert entity.native_value == 0.1234

    def test_it_says_which_method_produced_the_rate(self, hass_mock):
        from never_dry.sensor import DrynessIndexSensor, ETSensor

        cfg = {CONF_TEMP_SENSOR: "sensor.t", CONF_RAIN_SENSOR: "sensor.r"}
        hub = DrynessIndexSensor(hass_mock, cfg)
        entity = ETSensor(hass_mock, cfg, hub=hub)

        assert entity.extra_state_attributes["et_method"] == hub.active_method


class TestWhatTheModelWasFed:
    """Measured and derived, published side by side so the estimate is checkable.

    Every richer tier works out more than it reads: a daily maximum from a
    stream of readings, a radiation balance from a pyranometer and some
    astronomy, a wind speed corrected to a height nobody measured at. None of it
    is visible in the deficit, and a derived value that is quietly wrong looks
    exactly like one that is right.
    """

    def _fed_hub(self, hass_mock, method, **cfg):
        from unittest.mock import MagicMock

        from never_dry.sensor import DrynessIndexSensor

        hub = DrynessIndexSensor(
            hass_mock,
            {CONF_TEMP_SENSOR: "sensor.t", CONF_RAIN_SENSOR: "sensor.r", CONF_ET_METHOD: method, **cfg},
        )
        hass_mock.states.get = MagicMock(return_value=MagicMock(state="24.0"))
        _fill_diurnal_window(hub)
        hub._on_sensor_change(MagicMock())
        return hub

    def test_a_derived_maximum_is_shown_next_to_the_reading_it_came_from(self, hass_mock):
        hub = self._fed_hub(hass_mock, "hargreaves")
        fed = hub._last_inputs

        assert fed["measured_temperature_c"] == 24.0
        assert fed["derived_temp_max_c"] == 26.0
        assert fed["derived_temp_min_c"] == 14.0
        assert fed["derived_diurnal_range_c"] == 12.0
        assert fed["diurnal_window_hours"] >= 24

    def test_the_radiation_balance_shows_its_ingredients(self, hass_mock):
        """Rn is three steps from anything a user can see; each step is published."""
        hub = self._fed_hub(
            hass_mock,
            "penman_monteith",
            **{CONF_HUMIDITY_SENSOR: "sensor.h", CONF_WIND_SPEED_SENSOR: "sensor.w"},
        )
        fed = hub._last_inputs

        assert "derived_extraterrestrial_mj" in fed
        assert "derived_solar_mj" in fed
        assert "derived_net_radiation_mj" in fed
        assert "derived_wind_2m_m_s" in fed

    def test_it_says_whether_the_radiation_was_measured_or_estimated(self, hass_mock):
        """The same number means different things depending on where it came from."""
        hub = self._fed_hub(
            hass_mock,
            "penman_monteith",
            **{CONF_HUMIDITY_SENSOR: "sensor.h", CONF_WIND_SPEED_SENSOR: "sensor.w"},
        )
        assert hub._last_inputs["solar_is_measured"] is False

    def test_the_entity_carries_them(self, hass_mock):
        from never_dry.sensor import WaterBalanceMethodSensor

        hub = self._fed_hub(hass_mock, "hargreaves")
        attrs = WaterBalanceMethodSensor(hub).extra_state_attributes

        assert attrs["derived_diurnal_range_c"] == 12.0
        assert "et_rate_mm_h" in attrs


class TestTheMethodEntityStaysAlive:
    """It came up `unavailable` on the instance, and the suite had nothing to say.

    The entity subscribes to the hub so its attributes do not freeze at startup.
    Getting that subscription wrong takes the whole entity down — which is worse
    than the stale attributes it was meant to fix, and exactly what happened.
    """

    async def test_it_subscribes_without_raising(self, hass_mock):
        from never_dry.sensor import DrynessIndexSensor, WaterBalanceMethodSensor

        hub = DrynessIndexSensor(hass_mock, {CONF_TEMP_SENSOR: "sensor.t", CONF_RAIN_SENSOR: "sensor.r"})
        hub.entity_id = "sensor.neverdry_dryness_index"

        entity = WaterBalanceMethodSensor(hub)
        entity.hass = hass_mock
        entity.async_on_remove = lambda _cb: None

        await entity.async_added_to_hass()

    async def test_it_does_not_capture_hass_before_home_assistant_provides_it(self, hass_mock):
        """The bug: `hass` was read from the hub at construction, where it is None."""
        from never_dry.sensor import DrynessIndexSensor, WaterBalanceMethodSensor

        hub = DrynessIndexSensor(hass_mock, {CONF_TEMP_SENSOR: "sensor.t", CONF_RAIN_SENSOR: "sensor.r"})
        entity = WaterBalanceMethodSensor(hub)

        assert not hasattr(entity, "_hass")


def _install_registry(monkeypatch, registry):
    """Put a fake entity registry where the production import will find it.

    The function imports the attribute from ``homeassistant.helpers``, so
    patching ``sys.modules`` alone does nothing — the import succeeds against the
    package and quietly returns the real (absent) one, and the cleanup silently
    does not run. Which is also how it would fail in production.
    """
    import sys
    from types import SimpleNamespace

    fake = SimpleNamespace(async_get=lambda _hass: registry)
    monkeypatch.setattr(sys.modules["homeassistant.helpers"], "entity_registry", fake, raising=False)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.entity_registry", fake)


class TestSwitchingMethodLeavesNothingBehind:
    """The derived entities depend on the method, so changing it changes the set.

    Home Assistant keeps an entity that stops being created — it survives in the
    registry and reads unavailable for ever. Six dead rows in the diagnostic
    group teach people to stop reading the group, which is the problem the set
    was narrowed to avoid, moved one step along.
    """

    def _registry_with(self, unique_ids, entry_id="entry1"):
        from types import SimpleNamespace

        removed = []
        entities = {
            uid: SimpleNamespace(unique_id=uid, entity_id=f"sensor.{uid}", config_entry_id=entry_id)
            for uid in unique_ids
        }
        return SimpleNamespace(
            entities=entities,
            async_remove=lambda entity_id: removed.append(entity_id),
        ), removed

    def test_a_quantity_the_new_method_does_not_compute_is_forgotten(self, hass_mock, monkeypatch):
        from types import SimpleNamespace

        from never_dry import sensor as sensor_mod

        registry, removed = self._registry_with(
            ["entry1_model_input_derived_solar_mj", "entry1_model_input_derived_diurnal_range_c"]
        )
        _install_registry(monkeypatch, registry)

        kept = SimpleNamespace(_attr_unique_id="entry1_model_input_derived_diurnal_range_c")
        sensor_mod._drop_stale_model_inputs(hass_mock, "entry1", [kept])

        assert removed == ["sensor.entry1_model_input_derived_solar_mj"]

    def test_entities_of_other_entries_and_other_kinds_are_untouched(self, hass_mock, monkeypatch):
        """A zone sensor removed by accident takes its history with it."""
        from types import SimpleNamespace

        from never_dry import sensor as sensor_mod

        registry, removed = self._registry_with(["entry1_zone_deficit_orto", "entry1_model_input_derived_solar_mj"])
        registry.entities["other"] = SimpleNamespace(
            unique_id="entry2_model_input_derived_solar_mj",
            entity_id="sensor.other",
            config_entry_id="entry2",
        )
        _install_registry(monkeypatch, registry)

        sensor_mod._drop_stale_model_inputs(hass_mock, "entry1", [])

        assert removed == ["sensor.entry1_model_input_derived_solar_mj"]


class TestNothingReadsUnknownAfterAStart:
    """A restart used to leave every derived entity blank for minutes.

    The wait was only until the next temperature change, but an entity that says
    nothing after a restart is indistinguishable from one that is broken — which
    is how three separate looks at this device found an empty attribute list and
    concluded the feature was not working.
    """

    async def test_the_inputs_are_published_before_any_sensor_moves(self, hass_mock):
        from unittest.mock import AsyncMock, MagicMock

        from never_dry.sensor import DrynessIndexSensor

        hass_mock.states.get = MagicMock(return_value=MagicMock(state="24.0"))
        hub = DrynessIndexSensor(hass_mock, {CONF_TEMP_SENSOR: "sensor.t", CONF_RAIN_SENSOR: "sensor.r"})
        hub.async_get_last_state = AsyncMock(return_value=None)
        hub.async_write_ha_state = MagicMock()
        hub.async_on_remove = lambda _cb: None
        hub._bootstrap_diurnal_range = AsyncMock()
        hub._backfill_from_recorder = AsyncMock()

        await hub.async_added_to_hass()

        assert hub._last_inputs.get("status") != "no reading computed since startup"
        assert "measured_temperature_c" in hub._last_inputs

    async def test_it_does_not_fix_the_rain_baseline_early(self, hass_mock):
        """Publishing a display value must not move rain accounting.

        The first real reading is what fixes the baseline, deliberately and
        without crediting. Running a full tick at startup to populate the
        entities would take that decision minutes earlier, for the sake of how a
        dialog looks.
        """
        from unittest.mock import AsyncMock, MagicMock

        from never_dry.sensor import DrynessIndexSensor

        hass_mock.states.get = MagicMock(return_value=MagicMock(state="24.0"))
        hub = DrynessIndexSensor(hass_mock, {CONF_TEMP_SENSOR: "sensor.t", CONF_RAIN_SENSOR: "sensor.r"})
        hub.async_get_last_state = AsyncMock(return_value=None)
        hub.async_write_ha_state = MagicMock()
        hub.async_on_remove = lambda _cb: None
        hub._bootstrap_diurnal_range = AsyncMock()
        hub._backfill_from_recorder = AsyncMock()

        await hub.async_added_to_hass()

        assert hub._last_rain is None
