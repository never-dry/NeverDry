"""Tests for the water-balance model — the *how much*, and the Deficit it returns.

The load-bearing rule of the reference model is that two deficits are comparable
only within one frame. Most of the ``Deficit`` tests are about that, because a
bare float cannot express it and a silent cross-frame comparison is the class of
bug the value object exists to prevent.
"""

from dataclasses import FrozenInstanceError

import pytest
from never_dry.water_balance_model import (
    DEFAULT_ALPHA,
    DEFAULT_T_BASE,
    MODEL_CATALOGUE,
    RUNNABLE_INPUTS,
    W_M2_TO_MJ_DAY,
    DailySolarEnergy,
    Deficit,
    DiurnalRange,
    ETModel,
    ETStep,
    HargreavesModel,
    HargreavesStep,
    PenmanMonteithModel,
    PenmanStep,
    ReferenceFrame,
    VWCPerZoneModel,
    VWCReading,
    VWCSystemModel,
    build_model,
    models_offered_by,
    net_radiation_mj,
    solar_radiation_from_range,
    vwc_to_fraction,
)


class TestVWCToFraction:
    """The boundary rule that keeps a percentage from silencing a zone (GH #170)."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (0.22, 0.22),  # already a fraction — untouched
            (0.0, 0.0),  # bone dry
            (45.0, 0.45),  # the Ecowitt case: a percentage
            (15.0, 0.15),  # dry, and still a percentage
            (100.0, 1.0),  # saturated, expressed as a percentage
        ],
    )
    def test_reads_both_scales(self, raw, expected):
        assert vwc_to_fraction(raw) == pytest.approx(expected)

    def test_exactly_one_is_saturation_not_one_percent(self):
        """1.0 is a fraction: soil cannot sit at 1 % water content, but it can saturate."""
        assert vwc_to_fraction(1.0) == 1.0

    @pytest.mark.parametrize("raw", [310.0, 500.0, 101.0, -5.0, -0.1, float("nan"), float("inf")])
    def test_rejects_what_is_not_a_water_content(self, raw):
        """Raw ADC counts and negatives are refused, never clamped into a lie."""
        assert vwc_to_fraction(raw) is None


class TestDeficit:
    def test_zero_starts_a_zone_at_nothing(self):
        deficit = Deficit.zero(ReferenceFrame.ET, source="lawn")
        assert deficit.value_mm == 0.0
        assert deficit.source == "lawn"

    def test_clamped_bounds_both_ends(self):
        assert Deficit(-5.0, ReferenceFrame.ET, d_max=100.0).clamped().value_mm == 0.0
        assert Deficit(150.0, ReferenceFrame.ET, d_max=100.0).clamped().value_mm == 100.0

    def test_with_value_does_not_clamp_on_its_own(self):
        """The pair is deliberate: set, then clamp. Callers that want the box ask for it."""
        assert Deficit.zero(ReferenceFrame.ET).with_value(150.0).value_mm == 150.0

    def test_with_value_keeps_frame_and_source(self):
        original = Deficit.zero(ReferenceFrame.VWC_PER_ZONE, source="probe-1")
        moved = original.with_value(7.0)
        assert moved.frame is ReferenceFrame.VWC_PER_ZONE
        assert moved.source == "probe-1"

    def test_projects_onto_an_area(self):
        assert Deficit(10.0, ReferenceFrame.ET).as_liters(50.0) == 500.0

    def test_is_immutable(self):
        with pytest.raises(FrozenInstanceError):
            Deficit(1.0, ReferenceFrame.ET).value_mm = 2.0


class TestReferenceFrames:
    """Comparability is the whole reason the frame travels with the number."""

    def test_shared_frames_compare_by_frame_alone(self):
        a = Deficit(10.0, ReferenceFrame.ET, source="lawn")
        b = Deficit(20.0, ReferenceFrame.ET, source="roses")
        assert a.is_comparable_to(b)

    def test_different_frames_never_compare(self):
        et = Deficit(10.0, ReferenceFrame.ET)
        vwc = Deficit(10.0, ReferenceFrame.VWC_SYSTEM)
        assert not et.is_comparable_to(vwc)

    def test_two_zones_on_their_own_probes_are_not_comparable(self):
        """Each measures a different patch of soil, so the numbers are not the same quantity."""
        a = Deficit(10.0, ReferenceFrame.VWC_PER_ZONE, source="lawn")
        b = Deficit(10.0, ReferenceFrame.VWC_PER_ZONE, source="roses")
        assert not a.is_comparable_to(b)

    def test_the_same_probe_compares_with_itself(self):
        a = Deficit(10.0, ReferenceFrame.VWC_PER_ZONE, source="lawn")
        b = Deficit(14.0, ReferenceFrame.VWC_PER_ZONE, source="lawn")
        assert a.is_comparable_to(b)

    def test_a_per_zone_deficit_without_a_source_compares_with_nothing(self):
        a = Deficit(10.0, ReferenceFrame.VWC_PER_ZONE)
        b = Deficit(10.0, ReferenceFrame.VWC_PER_ZONE)
        assert not a.is_comparable_to(b)

    def test_shared_flag_matches_the_frames(self):
        assert ReferenceFrame.ET.is_shared
        assert ReferenceFrame.VWC_SYSTEM.is_shared
        assert not ReferenceFrame.VWC_PER_ZONE.is_shared


class TestETModel:
    def test_hourly_rate_matches_the_formula(self):
        assert ETModel.et_hourly(20.0) == pytest.approx(DEFAULT_ALPHA * (20.0 - DEFAULT_T_BASE) / 24.0)

    def test_cold_weather_produces_no_evaporation(self):
        """Below the base temperature the rate floors at zero rather than going negative."""
        assert ETModel.et_hourly(DEFAULT_T_BASE - 5.0) == 0.0

    def test_integrates_over_time_and_kc(self):
        model = ETModel(alpha=0.24, t_base=10.0, kc=1.0)
        model.step(ETStep(dt_h=24.0, temp_c=20.0))
        assert model.deficit.value_mm == pytest.approx(0.24 * (20.0 - 10.0) / 24.0 * 24.0)

    def test_kc_scales_the_demand(self):
        thirsty = ETModel(kc=1.0)
        frugal = ETModel(kc=0.5)
        thirsty.step(ETStep(dt_h=24.0, temp_c=25.0))
        frugal.step(ETStep(dt_h=24.0, temp_c=25.0))
        assert frugal.deficit.value_mm == pytest.approx(thirsty.deficit.value_mm / 2)

    def test_rain_is_subtracted(self):
        model = ETModel()
        model.step(ETStep(dt_h=24.0, temp_c=25.0))
        dry = model.deficit.value_mm
        model.step(ETStep(dt_h=0.0, temp_c=25.0, rain_mm=2.0))
        assert model.deficit.value_mm == pytest.approx(max(0.0, dry - 2.0))

    def test_clamps_at_d_max(self):
        model = ETModel(d_max=5.0)
        model.step(ETStep(dt_h=1000.0, temp_c=35.0))
        assert model.deficit.value_mm == 5.0

    def test_reports_the_et_frame(self):
        assert ETModel().reference_frame is ReferenceFrame.ET

    def test_irrigation_reduces_the_deficit(self):
        model = ETModel()
        model.step(ETStep(dt_h=24.0, temp_c=30.0))
        before = model.deficit.value_mm
        model.apply_irrigation(1.0)
        assert model.deficit.value_mm == pytest.approx(before - 1.0)

    def test_reset_clears_it(self):
        model = ETModel()
        model.step(ETStep(dt_h=24.0, temp_c=30.0))
        assert model.reset().value_mm == 0.0

    def test_rejects_the_wrong_input_shape(self):
        with pytest.raises(TypeError):
            ETModel().step(VWCReading(vwc=0.2))


class TestVWCSystemModel:
    def test_deficit_is_read_from_the_probe(self):
        model = VWCSystemModel(field_capacity=0.30, root_depth=0.30)
        model.step(VWCReading(vwc=0.20))
        assert model.deficit.value_mm == pytest.approx((0.30 - 0.20) * 0.30 * 1000)

    def test_above_field_capacity_is_no_deficit(self):
        model = VWCSystemModel(field_capacity=0.30, root_depth=0.30)
        model.step(VWCReading(vwc=0.35))
        assert model.deficit.value_mm == 0.0

    def test_is_stateless_so_readings_do_not_accumulate(self):
        """Two identical readings must not add up — the probe is the truth, not a tally."""
        model = VWCSystemModel(field_capacity=0.30, root_depth=0.30)
        model.step(VWCReading(vwc=0.20))
        first = model.deficit.value_mm
        model.step(VWCReading(vwc=0.20))
        assert model.deficit.value_mm == first

    def test_irrigation_is_a_no_op(self):
        """A stateless probe reports the wetter soil on its own next reading."""
        model = VWCSystemModel()
        model.step(VWCReading(vwc=0.10))
        before = model.deficit.value_mm
        model.apply_irrigation(10.0)
        assert model.deficit.value_mm == before

    def test_reset_is_a_no_op(self):
        model = VWCSystemModel()
        model.step(VWCReading(vwc=0.10))
        assert model.reset().value_mm == model.deficit.value_mm

    def test_reports_a_shared_frame(self):
        assert VWCSystemModel().reference_frame is ReferenceFrame.VWC_SYSTEM

    def test_rejects_the_wrong_input_shape(self):
        with pytest.raises(TypeError):
            VWCSystemModel().step(ETStep(dt_h=1.0, temp_c=20.0))


class TestVWCPerZoneModel:
    def test_reports_a_per_zone_frame_carrying_its_source(self):
        model = VWCPerZoneModel(source="lawn")
        model.step(VWCReading(vwc=0.20))
        assert model.reference_frame is ReferenceFrame.VWC_PER_ZONE
        assert model.deficit.source == "lawn"

    def test_computes_the_same_way_as_the_system_probe(self):
        per_zone = VWCPerZoneModel(source="lawn", field_capacity=0.30, root_depth=0.30)
        system = VWCSystemModel(field_capacity=0.30, root_depth=0.30)
        per_zone.step(VWCReading(vwc=0.18))
        system.step(VWCReading(vwc=0.18))
        assert per_zone.deficit.value_mm == pytest.approx(system.deficit.value_mm)


class TestHigherTiers:
    """Sanity only: the point is that a tier is one rate behind a shared integrator."""

    def test_hargreaves_needs_no_extra_sensor_and_gives_a_plausible_rate(self):
        model = HargreavesModel(latitude_deg=45.0)
        model.step(HargreavesStep(dt_h=24.0, tmax_c=30.0, tmin_c=15.0, day_of_year=196))
        assert 0.0 < model.deficit.value_mm < 15.0

    def test_hargreaves_grows_with_the_temperature_range(self):
        narrow = HargreavesModel(latitude_deg=45.0)
        wide = HargreavesModel(latitude_deg=45.0)
        narrow.step(HargreavesStep(dt_h=24.0, tmax_c=24.0, tmin_c=20.0, day_of_year=196))
        wide.step(HargreavesStep(dt_h=24.0, tmax_c=34.0, tmin_c=10.0, day_of_year=196))
        assert wide.deficit.value_mm > narrow.deficit.value_mm

    def test_penman_monteith_gives_a_plausible_summer_rate(self):
        model = PenmanMonteithModel()
        model.step(
            PenmanStep(dt_h=24.0, temp_c=25.0, rh_pct=50.0, wind_m_s=2.0, net_radiation_mj=15.0),
        )
        assert 0.0 < model.deficit.value_mm < 15.0

    def test_every_et_tier_shares_the_same_frame(self):
        """The seam is the output: different inputs, one comparable quantity."""
        assert ETModel().reference_frame is ReferenceFrame.ET
        assert HargreavesModel(latitude_deg=45.0).reference_frame is ReferenceFrame.ET
        assert PenmanMonteithModel().reference_frame is ReferenceFrame.ET


class TestCapabilityMatch:
    """Which models a site may pick, and what happens when its sensors change.

    The rule is one line — ``declared >= required`` — so what these tests really
    hold is the two halves being written in the same vocabulary. A model added
    to the catalogue without declaring what it needs would be offered to
    everyone, which is the failure the match exists to prevent.
    """

    def test_every_catalogued_model_declares_what_it_needs(self):

        for model in MODEL_CATALOGUE:
            assert isinstance(model.required_sensors, frozenset), model.__name__
            assert model.method_id, model.__name__

    def test_identifiers_are_unique(self):
        """The id is stored in the config entry: a collision would silently swap models."""

        ids = [m.method_id for m in MODEL_CATALOGUE]
        assert len(ids) == len(set(ids))

    def test_a_thermometer_alone_offers_both_temperature_tiers(self):
        """Hargreaves needs no more hardware than the simple tier once the daily
        range is observed rather than declared — which is the whole point of
        observing it. What separates them is what they know about the sun, not
        what they read.
        """
        from never_dry.environment import Environment

        env = Environment(temperature_sensor="sensor.t", rain_sensor="sensor.r")
        assert set(models_offered_by(env)) == {ETModel, HargreavesModel}

    def test_the_automatic_choice_takes_the_best_the_sensors_support(self):
        """Automatic means what it says, on an upgrade exactly as on a fresh install.

        Pinning existing gardens to whatever they happened to be running would
        make "automatic" mean "whatever you had", which nobody would ask for.
        Someone who adds a sensor expects the estimate to improve; someone who
        wants a specific method names it.
        """
        from never_dry.environment import Environment

        env = Environment(temperature_sensor="sensor.t", rain_sensor="sensor.r")
        assert isinstance(build_model(env), HargreavesModel)

    def test_a_flat_thermometer_withdraws_the_tiers_that_read_the_range(self):
        """Evidence, not dogma: a sensor that never swings cannot feed Hargreaves.

        A shaded or indoor probe shows a flat day, which the formula reads as
        permanent overcast and turns into a systematic under-estimate — every
        hour, invisibly. So the automatic choice steps back to the tier that does
        not look at the range.
        """
        from never_dry.environment import Environment

        env = Environment(temperature_sensor="sensor.t", rain_sensor="sensor.r")
        assert isinstance(build_model(env, diurnal_range_c=0.7), ETModel)

    def test_an_explicit_choice_survives_the_same_evidence(self):
        """The statistic does not know what the user knows — a sensor about to be
        moved, a site where the flatness is real. Naming a method is an assertion,
        and it is honoured.
        """
        from never_dry.environment import Environment

        env = Environment(temperature_sensor="sensor.t", rain_sensor="sensor.r")
        assert isinstance(build_model(env, method_id="hargreaves", diurnal_range_c=0.7), HargreavesModel)

    def test_it_can_still_be_chosen_explicitly(self):
        from never_dry.environment import Environment

        env = Environment(temperature_sensor="sensor.t", rain_sensor="sensor.r")
        assert isinstance(build_model(env, method_id="hargreaves"), HargreavesModel)

    def test_declaring_the_sensors_is_not_enough_if_the_input_cannot_be_built(self):
        """Two conditions, and this is the one that is easy to forget.

        Every catalogued model is runnable today, so the rule is checked against
        a model that is not. It is worth keeping: a written-and-tested class
        whose reading nothing produces is *selectable and not runnable*, which
        is worse than absent — it builds, and then raises on every update. That
        happened twice, on a live instance, before this existed.
        """
        from dataclasses import dataclass

        from never_dry.environment import Environment

        @dataclass(frozen=True)
        class UnbuildableReading:
            dt_h: float

        class NotFedByAnyone(ETModel):
            method_id = "not_fed"
            input_type = UnbuildableReading

        env = Environment(temperature_sensor="sensor.t", rain_sensor="sensor.r")
        assert env.satisfies(NotFedByAnyone.required_sensors)

        import never_dry.water_balance_model as wbm

        original = wbm.MODEL_CATALOGUE
        wbm.MODEL_CATALOGUE = (*original, NotFedByAnyone)
        try:
            assert NotFedByAnyone not in models_offered_by(env)
        finally:
            wbm.MODEL_CATALOGUE = original

    def test_a_probe_wins_over_the_weather_tiers(self):
        """A measured soil is better evidence than an estimate, so it leads the order."""
        from never_dry.environment import Environment

        env = Environment(
            temperature_sensor="sensor.t",
            rain_sensor="sensor.r",
            soil_moisture_sensor="sensor.vwc",
        )
        assert models_offered_by(env)[0] is VWCSystemModel


class TestBuildModel:
    """Turning a site plus a stored preference into the object that runs."""

    def _bare_site(self):
        from never_dry.environment import Environment

        return Environment(temperature_sensor="sensor.t", rain_sensor="sensor.r")

    def test_without_a_preference_it_takes_the_best_supported(self):

        assert isinstance(build_model(self._bare_site()), HargreavesModel)

    def test_a_site_with_a_probe_gets_the_probe_model(self):
        from never_dry.environment import Environment

        env = Environment(temperature_sensor="sensor.t", rain_sensor="sensor.r", soil_moisture_sensor="sensor.vwc")
        assert isinstance(build_model(env), VWCSystemModel)

    def test_a_choice_the_site_cannot_support_degrades_instead_of_failing(self):
        """A sensor can be removed after the choice was stored. Watering must not stop."""

        model = build_model(self._bare_site(), method_id="penman_monteith")
        assert isinstance(model, HargreavesModel)

    def test_an_unknown_identifier_falls_back_rather_than_raising(self):
        """A config entry from a future version must not break setup."""

        assert isinstance(build_model(self._bare_site(), method_id="no_such_model"), HargreavesModel)

    def test_a_supported_choice_is_honoured_over_the_default_order(self):
        """The user's preference beats the ranking — that is the point of asking."""
        from never_dry.environment import Environment

        env = Environment(temperature_sensor="sensor.t", rain_sensor="sensor.r", soil_moisture_sensor="sensor.vwc")
        assert isinstance(build_model(env, method_id="et_simple"), ETModel)

    def test_the_configured_values_reach_the_model(self):

        model = build_model(self._bare_site(), alpha=0.5, t_base=5.0, d_max=42.0)
        assert model.d_max == 42.0
        assert model.step(ETStep(dt_h=24.0, temp_c=15.0)).value_mm == pytest.approx(0.5 * (15.0 - 5.0))


class TestRestore:
    """Adopting a value computed elsewhere — a restart, or a recorder replay."""

    def test_it_adopts_the_value(self):
        model = ETModel()
        assert model.restore(12.5).value_mm == 12.5

    def test_a_stored_value_above_the_current_ceiling_is_clamped(self):
        """d_max can shrink between releases; a stored value must not outlive it."""
        model = ETModel(d_max=10.0)
        assert model.restore(50.0).value_mm == 10.0

    def test_a_negative_stored_value_is_refused(self):
        model = ETModel()
        assert model.restore(-3.0).value_mm == 0.0


def test_the_form_options_mirror_the_catalogue():
    """`const.ET_METHOD_OPTIONS` is a copy, so it needs a guard, not a comment.

    The form cannot import the model module (the translation guard resolves the
    dropdown statically), so the identifiers are written twice. This is the test
    that makes the duplication safe, in both directions: a method that runs and
    is missing here is unreachable from the UI, and a method listed here that
    does *not* run is an option that raises when chosen.
    """
    from never_dry.const import ET_METHOD_AUTO, ET_METHOD_OPTIONS

    runnable = tuple(m.method_id for m in MODEL_CATALOGUE if m.input_type in RUNNABLE_INPUTS)
    expected = (ET_METHOD_AUTO, *runnable)
    assert tuple(ET_METHOD_OPTIONS) == expected, (
        "the dropdown must offer exactly the methods that run: add a method here "
        "in the change that builds its input, not in the one that writes the class"
    )


class TestDiurnalRange:
    """The daily extremes, observed instead of asked for.

    The direction of error is the whole design. A window that is too thin has a
    range that is too small, a small range reads as an overcast day, and an
    overcast day means little water. Being wrong here does not produce a visible
    mistake — it produces a garden that is watered less than it needs, quietly.
    So a fragment answers ``None`` rather than its best guess.
    """

    def _fill(self, tracker, hours, temps):
        for h, t in zip(hours, temps, strict=True):
            tracker.observe(h, t)

    def test_a_fragment_of_a_day_refuses_to_answer(self):

        tracker = DiurnalRange()
        self._fill(tracker, range(5), [15.0, 18.0, 22.0, 25.0, 21.0])
        assert tracker.extremes() is None
        assert not tracker.is_ready

    def test_a_full_day_reports_its_extremes(self):

        tracker = DiurnalRange()
        temps = [12.0, 11.5, 11.0, 12.0, 14.0, 17.0, 20.0, 23.0, 26.0, 28.0, 30.0, 31.0]
        temps += [30.5, 29.0, 27.0, 24.0, 21.0, 19.0, 17.0, 16.0, 15.0, 14.0, 13.0, 12.5]
        self._fill(tracker, range(24), temps)
        assert tracker.extremes() == (11.0, 31.0)

    def test_several_readings_in_one_hour_widen_that_hour(self):
        """The bucket keeps the hour's own min and max, not the last value seen."""

        tracker = DiurnalRange()
        for h in range(24):
            tracker.observe(h, 20.0)
        tracker.observe(5, 33.0)
        tracker.observe(5, 8.0)
        assert tracker.extremes() == (8.0, 33.0)

    def test_yesterday_falls_out_of_the_window(self):
        """Rolling, not cumulative: a heatwave three days ago is not today's range."""

        tracker = DiurnalRange()
        tracker.observe(0, 40.0)
        for h in range(1, 30):
            tracker.observe(h, 20.0)
        assert tracker.extremes() == (20.0, 20.0)

    def test_storage_stays_bounded_however_often_it_is_observed(self):
        """The caller observes on every sensor change, which is often."""

        tracker = DiurnalRange()
        for i in range(5000):
            tracker.observe(i / 60.0, 20.0 + (i % 7))
        assert tracker.coverage_h <= 24

    def test_a_sensor_that_never_sees_the_sky_is_called_out(self):
        """An indoor or sheltered probe gives a flat day, and a flat day is not weather."""

        tracker = DiurnalRange()
        for h in range(24):
            tracker.observe(h, 21.0 + (h % 2) * 0.3)
        assert tracker.extremes() is not None
        assert tracker.is_implausible()

    def test_a_real_day_is_not_called_out(self):

        tracker = DiurnalRange()
        for h in range(24):
            tracker.observe(h, 15.0 + 8.0 * (h % 12) / 12.0)
        assert not tracker.is_implausible()


class TestNetRadiation:
    """Rn is computed, never asked for — and the two halves pull opposite ways."""

    def _ra(self, doy=196, lat=45.0):

        return HargreavesModel.extraterrestrial_radiation(doy, lat)

    def test_a_bright_day_keeps_most_of_what_arrives(self):

        ra = self._ra()
        rn = net_radiation_mj(solar_mj=0.75 * ra, ra_mj=ra, tmax_c=31.0, tmin_c=19.0, rh_pct=50.0)
        assert 0.4 * ra < rn < 0.7 * ra

    def test_an_overcast_day_keeps_less(self):
        """Same site, same day, a third of the radiation: the balance must follow."""

        ra = self._ra()
        bright = net_radiation_mj(solar_mj=0.75 * ra, ra_mj=ra, tmax_c=31.0, tmin_c=19.0, rh_pct=50.0)
        dull = net_radiation_mj(solar_mj=0.25 * ra, ra_mj=ra, tmax_c=24.0, tmin_c=20.0, rh_pct=85.0)
        assert dull < bright

    def test_dry_air_loses_more_to_the_sky(self):
        """Water vapour is what sends the ground's heat back; without it, more escapes."""

        ra = self._ra()
        humid = net_radiation_mj(solar_mj=0.7 * ra, ra_mj=ra, tmax_c=30.0, tmin_c=18.0, rh_pct=80.0)
        dry = net_radiation_mj(solar_mj=0.7 * ra, ra_mj=ra, tmax_c=30.0, tmin_c=18.0, rh_pct=20.0)
        assert dry < humid

    def test_the_ground_never_appears_to_gain_radiation_at_night(self):
        """Found by this test: with Rs at zero the bracket turned negative and the
        balance came out *positive* — the soil warming itself from a colder sky.

        FAO-56 defines the cloudiness ratio over a day; fed an instantaneous zero
        it breaks. The loss is floored at zero instead: neutralised, never
        reversed. Night-time cooling therefore goes uncredited, which understates
        nothing that matters — evapotranspiration at night is near zero anyway.
        """

        ra = self._ra()
        rn = net_radiation_mj(solar_mj=0.0, ra_mj=ra, tmax_c=25.0, tmin_c=15.0, rh_pct=60.0)
        assert rn <= 0.0

    def test_a_site_without_a_pyranometer_estimates_the_radiation(self):
        """The fallback: the same diurnal range, used to produce a radiation."""

        ra = self._ra()
        clear = solar_radiation_from_range(ra, tmax_c=32.0, tmin_c=16.0)
        cloudy = solar_radiation_from_range(ra, tmax_c=24.0, tmin_c=21.0)
        assert clear > cloudy
        assert 0.0 < clear < ra

    def test_the_estimate_is_in_the_same_range_as_a_measurement(self):
        """It stands in for Rs, so it has to be comparable to one, not merely ordered."""

        ra = self._ra()
        estimated = solar_radiation_from_range(ra, tmax_c=31.0, tmin_c=19.0)
        assert 0.4 * ra < estimated < 0.85 * ra


class TestDailySolarEnergy:
    """A pyranometer reports power; the equations need the day's energy.

    Treating one as the other is not a unit slip, it is a different quantity:
    an evening reading scaled to a day understates the radiation several-fold,
    and every number downstream inherits it — ending in a garden watered a
    fraction of what it needs, with nothing to show for it.
    """

    def test_a_thin_window_refuses_to_answer(self):

        acc = DailySolarEnergy()
        for h in range(5):
            acc.observe(float(h), 800.0)
        assert acc.energy_mj() is None

    def test_a_full_day_sums_to_a_plausible_summer_total(self):
        """A clear August day at mid-latitude delivers roughly 20-30 MJ/m2."""

        acc = DailySolarEnergy()
        profile = [0, 0, 0, 0, 0, 20, 120, 300, 500, 680, 810, 880]
        profile += [900, 860, 760, 600, 400, 200, 60, 5, 0, 0, 0, 0]
        for h, watts in enumerate(profile):
            acc.observe(float(h), float(watts))

        total = acc.energy_mj()
        assert 20.0 < total < 30.0

    def test_night_hours_count_as_zero_and_are_needed(self):
        """Dropping them would make the average a daytime average, inflating the day."""

        with_night = DailySolarEnergy()
        for h in range(24):
            with_night.observe(float(h), 900.0 if 6 <= h < 18 else 0.0)

        assert with_night.energy_mj() == pytest.approx(900.0 * 12 * 3600 / 1e6, rel=1e-6)

    def test_repeated_readings_in_an_hour_average_rather_than_add(self):
        """The station reports every minute; adding them would multiply the day by sixty."""

        acc = DailySolarEnergy()
        for h in range(24):
            for _ in range(60):
                acc.observe(h + 0.5, 500.0)
        assert acc.energy_mj() == pytest.approx(500.0 * 24 * 3600 / 1e6, rel=1e-6)

    def test_an_evening_reading_alone_is_not_mistaken_for_a_day(self):
        """The defect this class exists to remove, stated as a test."""

        naive = 65.9 * W_M2_TO_MJ_DAY

        acc = DailySolarEnergy()
        profile = [0, 0, 0, 0, 0, 20, 120, 300, 500, 680, 810, 880]
        profile += [900, 860, 760, 600, 400, 200, 66, 5, 0, 0, 0, 0]
        for h, watts in enumerate(profile):
            acc.observe(float(h), float(watts))

        assert acc.energy_mj() > 3 * naive
