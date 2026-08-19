"""Upgrading a garden that is already running: nothing may change in silence.

The migration tests cover a *version* bump — the entry changes shape and a
migration rewrites it. This file covers the case that has no migration and is
therefore easier to get wrong: the entry stays exactly as it was, and the code
underneath it changes. That is what every HACS update is.

The failure this guards against is not a crash. A crash is loud and gets
reported the same evening. The dangerous version is the one where setup
succeeds, the entities come back, and a number is quietly different — a deficit
that dropped, a model that is no longer the one that was running, a valve whose
retry budget changed. Nobody reports that, because nothing looks wrong; the
garden just gets watered differently.

So the entry used here is not invented. It is the shape a real installation
carries after months of running: the six keys the config flow wrote before any
of today's fields existed, and not one of the new ones.
"""

from unittest.mock import MagicMock

import pytest
from never_dry.const import (
    CONF_ALPHA,
    CONF_D_MAX,
    CONF_RAIN_SENSOR,
    CONF_RAIN_SENSOR_TYPE,
    CONF_T_BASE,
    CONF_TEMP_SENSOR,
    CONF_VWC_SENSOR,
)
from never_dry.sensor import DrynessIndexSensor
from never_dry.water_balance_model import ETModel, ETStep, VWCSystemModel

#: An entry written by the config flow *before* this release. No `et_method`,
#: no humidity, no wind, no radiation, no daily extremes.
LEGACY_ENTRY = {
    CONF_TEMP_SENSOR: "sensor.outdoor_temperature",
    CONF_RAIN_SENSOR: "sensor.rain_daily",
    CONF_RAIN_SENSOR_TYPE: "daily_total",
    CONF_ALPHA: 0.22,
    CONF_T_BASE: 9.0,
    CONF_D_MAX: 100.0,
}


class TestTheModelIsTheOneThatWasRunning:
    """An upgrade must not change which physics computes the deficit."""

    def test_a_legacy_entry_moves_to_the_best_its_sensors_support(self, hass_mock):
        """An upgrade *does* change the method here, and that is the decision, not a slip.

        "Automatic" that meant "whatever you already had" would never improve for
        anyone: a user who adds a sensor expects the estimate to follow, and one
        who wants a fixed method names it. The change is announced — the running
        method is an entity, with the reason attached, and it is logged at
        startup.
        """
        from never_dry.water_balance_model import HargreavesModel

        hub = DrynessIndexSensor(hass_mock, dict(LEGACY_ENTRY))
        assert isinstance(hub._model, HargreavesModel)
        assert hub._configured_method == "auto"

    def test_the_stored_parameters_reach_it(self, hass_mock):
        """A site that tuned alpha must keep the tuning, not inherit the default."""
        tuned = {**LEGACY_ENTRY, CONF_ALPHA: 0.31, CONF_T_BASE: 7.5, CONF_D_MAX: 60.0}
        hub = DrynessIndexSensor(hass_mock, tuned)
        assert hub._model.d_max == 60.0
        # One full day at 17.5 °C: alpha x (T - t_base) = 0.31 x 10.
        assert hub._model.step(ETStep(dt_h=24.0, temp_c=17.5)).value_mm == pytest.approx(3.1)

    def test_a_legacy_probe_install_still_runs_the_probe_model(self, hass_mock):
        """The probe used to bypass ET by an `if`. It must still bypass it, by the model."""
        hub = DrynessIndexSensor(hass_mock, {**LEGACY_ENTRY, CONF_VWC_SENSOR: "sensor.soil"})
        assert isinstance(hub._model, VWCSystemModel)

    def test_no_new_key_is_required_for_setup(self, hass_mock):
        """Every field added this release is optional; absent must mean absent."""
        hub = DrynessIndexSensor(hass_mock, dict(LEGACY_ENTRY))
        assert hub._model is not None
        assert hub.environment.declared_sensors  # temperature + rain, nothing more


class TestTheNumbersDoNotMove:
    """The deficit a site wakes up with, and the rate it broadcasts, are unchanged."""

    def test_a_restored_deficit_survives_exactly(self, hass_mock):
        """Restore runs before any reading; a rounded or clamped value here is water lost."""
        hub = DrynessIndexSensor(hass_mock, dict(LEGACY_ENTRY))
        hub._deficit = 7.43
        assert hub._deficit == pytest.approx(7.43)

    def test_a_restored_deficit_beyond_the_ceiling_is_capped_not_kept(self, hass_mock):
        """A deliberate difference, asserted so it stays deliberate.

        The old restore assigned the raw stored value and the first integration
        step clamped it. The new one clamps on the way in. The visible window is
        between a restart and the next sensor reading, and capping is the right
        answer there: a value above `d_max` is one the model can no longer
        explain, usually because the user lowered the ceiling.
        """
        hub = DrynessIndexSensor(hass_mock, {**LEGACY_ENTRY, CONF_D_MAX: 50.0})
        hub._deficit = 80.0
        assert hub._deficit == 50.0

    def test_one_step_matches_the_formula_it_replaced(self, hass_mock):
        """The expression that used to sit inline in the entity, asserted against the model.

        Not bit-identical, and the difference is worth naming rather than
        hiding: a ``Deficit`` rounds to four decimals on the way out, while the
        old inline value was raw. The sensor publishes two decimals, so this is
        an order of magnitude below anything anyone can see — but it is a real
        difference, and the next test is what makes it harmless.
        """
        hub = DrynessIndexSensor(hass_mock, dict(LEGACY_ENTRY))
        hub._deficit = 2.0
        dt_h, temp_c, rain_mm = 0.5, 24.0, 0.2

        expected = 2.0 + ETModel.et_hourly(temp_c, alpha=0.22, t_base=9.0) * dt_h - rain_mm
        got = hub._model.step(ETStep(dt_h=dt_h, temp_c=temp_c, rain_mm=rain_mm)).value_mm

        assert got == pytest.approx(expected, abs=5e-5)
        assert round(got, 2) == round(expected, 2)

    def test_rounding_happens_at_the_edge_and_never_accumulates(self, hass_mock):
        """The property that makes the rounding above harmless, held explicitly.

        If each step fed its own rounded output back in, the error would compound
        — a hundred steps a day, every day, on a value the whole schedule reads.
        It does not: the model integrates its full-precision accumulator and
        rounds only what it hands out.
        """
        hub = DrynessIndexSensor(hass_mock, dict(LEGACY_ENTRY))
        reading = ETStep(dt_h=0.1, temp_c=23.3, rain_mm=0.0)

        for _ in range(1000):
            hub._model.step(reading)

        per_step = ETModel.et_hourly(23.3, alpha=0.22, t_base=9.0) * 0.1
        assert hub._deficit == pytest.approx(1000 * per_step, abs=5e-5)

    def test_the_rate_broadcast_to_zones_is_unchanged(self, hass_mock):
        """Zones scale this by their Kc, so a drift here moves every zone at once."""
        hub = DrynessIndexSensor(hass_mock, dict(LEGACY_ENTRY))
        reading = ETStep(dt_h=1.0, temp_c=21.0)
        assert hub._model.et_rate(reading) == pytest.approx(ETModel.et_hourly(21.0, alpha=0.22, t_base=9.0))


class TestTheValveKeepsItsHabits:
    """The driver replaced the operator underneath a running garden.

    Its behaviour is *meant* to differ in one place — the already-open
    confirmation, which is the bug the swap delivers. Everything else must be
    the same, and the settings below are the ones that decide how a flaky valve
    is treated: how many times it is retried, and how many failures in a row
    take the zone out of service. A silent change here shows up as a zone that
    stops watering, days later, with no obvious cause.

    Compares against the module it superseded. When `valve_operator.py` is
    removed (AI-270) this comparison goes with it — and by then the values it
    pins will have been observed in the field for weeks.
    """

    def test_the_retry_budget_and_failure_threshold_are_unchanged(self):
        import inspect

        from never_dry.driver import Driver
        from never_dry.valve_operator import ValveOperator

        old = inspect.signature(ValveOperator.__init__).parameters
        new = inspect.signature(Driver.__init__).parameters

        for shared in ("max_retries", "flow_zero_threshold", "max_open_duration_s", "hw_max_duration_multiplier"):
            assert new[shared].default == old[shared].default, f"{shared} changed under a running install"

    def test_the_default_backoff_ladder_is_unchanged(self):
        from never_dry.driver import Driver
        from never_dry.valve_operator import ValveOperator

        assert Driver.DEFAULT_BACKOFF_S == ValveOperator.DEFAULT_BACKOFF_S

    def test_a_zone_without_a_flow_meter_still_builds_an_fsm_that_expects_none(self, hass_mock):
        """`has_flow_meter` decides whether a missing flow reading is a fault."""
        from never_dry.driver import ZoneDriver

        hass_mock.states.get = MagicMock(return_value=MagicMock(state="off"))
        driver = ZoneDriver(hass_mock, "switch.zone", name="z")
        assert driver._fsm_config.has_flow_meter is False
        assert driver._fsm_config.max_consecutive_failures == driver._max_retries + 1


class TestChangingTheModelDoesNotLoseTheDeficit:
    """The model holds the deficit, so replacing it is replacing the state.

    Found on a live garden: the hub restores its value, then re-selects the model
    once the diurnal evidence is in, and the second step handed back a fresh
    object starting at zero. The index went from 10 mm to 0 with nothing in the
    log — a garden that believes it has just been watered, and stops watering.
    """

    def test_the_value_survives_a_re_selection(self, hass_mock):
        hub = DrynessIndexSensor(hass_mock, dict(LEGACY_ENTRY))
        hub._deficit = 9.4

        hub._select_model(observed_range_c=11.0)

        assert hub._deficit == pytest.approx(9.4)

    def test_it_survives_a_re_selection_that_changes_the_method(self, hass_mock):
        """The demotion path is the one that changes the object, so it is the risk."""
        from never_dry.water_balance_model import ETModel

        hub = DrynessIndexSensor(hass_mock, dict(LEGACY_ENTRY))
        hub._deficit = 9.4

        hub._select_model(observed_range_c=0.5)

        assert isinstance(hub._model, ETModel)
        assert hub._deficit == pytest.approx(9.4)
