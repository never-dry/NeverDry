"""The soil probe belongs to a zone, and moving it there must cost nobody anything.

A probe measures one patch of soil, with one kind of planting above it and its
own watering history. Declared once for the whole installation it drove every
zone, which is not a shortcut but a wrong answer: the reading is not
transferable to a zone watered independently.

The risk in fixing it is not the model — that part is arithmetic — it is the
users who already have one. Two of them reported the bugs that led here. So what
these tests hold is mostly about *them*: what happens on upgrade, what happens
while the question is unanswered, and what is never decided on their behalf.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from never_dry.const import (
    CONF_RAIN_SENSOR,
    CONF_TEMP_SENSOR,
    CONF_VWC_SENSOR,
    CONF_ZONE_AREA,
    CONF_ZONE_FIELD_CAPACITY,
    CONF_ZONE_NAME,
    CONF_ZONE_ROOT_DEPTH,
    CONF_ZONE_SOIL_TYPE,
    CONF_ZONE_VWC_SENSOR,
    CONF_ZONES,
    CONFIG_VERSION,
    PROBE_CADENCE_MEMORY_S,
    SOIL_TYPE_CLAY,
    SOIL_TYPE_CUSTOM,
    SOIL_TYPE_SANDY,
)
from never_dry.sensor import DrynessIndexSensor, IrrigationZoneSensor

HUB = {CONF_TEMP_SENSOR: "sensor.t", CONF_RAIN_SENSOR: "sensor.r"}


def _zone(hass, dryness, **cfg):
    return IrrigationZoneSensor(hass, {CONF_ZONE_NAME: "Orto", CONF_ZONE_AREA: 20.0, **cfg}, dryness)


#: A zone whose probe has been told what to read its readings with. The soil is
#: named rather than left automatic so that the arithmetic in these tests stays
#: legible, and it is a *named* soil rather than Custom because the probe needs
#: both ends of the soil's interval and Custom supplies only one (GH #234).
#:
#: Clay holds 0.36 and gives up nothing below 0.22, so 0.30 m of roots is a
#: reservoir of (0.36 - 0.22) * 0.30 * 1000 = **42.0 mm**. A reading of 18 %
#: leaves 82 % of that missing: **34.44 mm**.
DRIVEN = {
    CONF_ZONE_VWC_SENSOR: "sensor.orto_soil",
    CONF_ZONE_ROOT_DEPTH: 0.30,
    CONF_ZONE_SOIL_TYPE: SOIL_TYPE_CLAY,
}

#: The reservoir and the deficit the fixture above produces, named so a change
#: to the fixture cannot leave a stale number behind in twenty assertions.
RESERVOIR_MM = 42.0
AT_18_PCT = 34.44


def _zone_of(hass, dryness, **cfg):
    """A zone with an arbitrary ground configuration."""
    return IrrigationZoneSensor(hass, {CONF_ZONE_NAME: "Orto", CONF_ZONE_AREA: 20.0, **cfg}, dryness)


def _last_state(attributes: dict):
    """A stand-in for ``async_get_last_state`` returning one restored state."""

    async def _get():
        state = MagicMock()
        state.attributes = attributes
        return state

    return _get


def _reading(value: str):
    """A probe state-change event carrying ``value``."""
    event = MagicMock()
    event.data = {"new_state": MagicMock(state=value)}
    return event


def _has_come_back_from(zone, *quiet_s: float):
    """Stretches of quiet this probe went through and came back from.

    The bar is built from ended quiet only, so this is the only way a zone
    acquires one: what the probe has survived, not what it is doing now.
    """
    at = datetime.now(UTC).timestamp()
    for quiet in quiet_s:
        zone._probe_quiet.record(at, quiet)


class TestAZoneWithItsOwnProbe:
    """The probe is data, not the deficit — and that distinction was decided the hard way.

    Implementing it the other way round was rejected on field evidence: two zones
    on the same soil sit at systematically different moisture because the
    irrigation is unbalanced and one has far more shade on the ground. Both are
    circumstances of a spot, and a deficit that followed the reading would feed a
    plumbing imbalance back into the model as if it were information about the
    soil's need.
    """

    def test_the_reading_is_published(self, hass_mock):
        hub = DrynessIndexSensor(hass_mock, dict(HUB))
        zone = _zone(hass_mock, hub, **{CONF_ZONE_VWC_SENSOR: "sensor.orto_soil"})

        event = MagicMock()
        event.data = {"new_state": MagicMock(state="18.0")}  # 18 %, below field capacity
        zone._on_own_probe(event)

        attrs = zone.extra_state_attributes
        assert attrs["probe_moisture_pct"] == pytest.approx(18.0)
        # Published beside it: the gap between this and the model's deficit after
        # an irrigation is what reveals a delivery that moved no water. This zone
        # declared no ground of its own, so the site's numbers scale the reading:
        # (1 - 0.18) * (0.30 - 0.12) * 0.30 m * 1000.
        assert attrs["probe_implied_deficit_mm"] == pytest.approx(44.28)

    def test_the_reading_does_not_touch_the_deficit_until_the_zone_says_how_to_read_it(self, hass_mock):
        """The old rule, now circumstantiated rather than deleted.

        It was never "a probe must not drive the deficit". It was "a probe must
        not drive it *on its own*, with nobody having said what to read it
        with": a reading is a fraction, and the millimetres only exist once a
        root depth and a field capacity have been declared for this patch of
        soil. Without them nothing has changed, and that is what this holds.
        """
        hub = DrynessIndexSensor(hass_mock, dict(HUB))
        zone = _zone(hass_mock, hub, **{CONF_ZONE_VWC_SENSOR: "sensor.orto_soil"})
        zone._zone_deficit = 4.0

        zone._on_own_probe(_reading("18.0"))

        assert zone._probe_drives is False
        assert zone._zone_deficit == 4.0
        assert zone.deficit_source == "site_model"

    def test_the_zone_keeps_integrating_the_model(self, hass_mock):
        """A probe adds a measurement; it does not switch the model off.

        This is also the failure mode that decided it: a probe that dies would
        otherwise freeze the deficit and stop the watering, in silence.
        """
        hub = DrynessIndexSensor(hass_mock, dict(HUB))
        zone = _zone(hass_mock, hub, **{CONF_ZONE_VWC_SENSOR: "sensor.orto_soil"})
        zone._zone_deficit = 1.0

        zone._on_et_update(1.0, 0.3, 0.0)

        assert zone._zone_deficit > 1.0

    def test_a_zone_without_one_is_untouched(self, hass_mock):
        """The change must be invisible to every zone that has no probe."""
        hub = DrynessIndexSensor(hass_mock, dict(HUB))
        zone = _zone(hass_mock, hub)
        zone._zone_deficit = 1.0

        zone._on_et_update(1.0, 0.2, 0.0)

        assert zone._zone_deficit > 1.0

    def test_a_percentage_reading_is_converted_not_believed(self, hass_mock):
        """Consumer probes report 45, not 0.45 — read raw, every reading looks saturated."""
        hub = DrynessIndexSensor(hass_mock, dict(HUB))
        zone = _zone(hass_mock, hub, **{CONF_ZONE_VWC_SENSOR: "sensor.orto_soil"})

        event = MagicMock()
        event.data = {"new_state": MagicMock(state="45")}
        zone._on_own_probe(event)

        assert zone.extra_state_attributes["probe_moisture_pct"] == pytest.approx(45.0)

    def test_an_unreadable_probe_publishes_nothing(self, hass_mock):
        """A missing reading is not a dry soil, and not a wet one either."""
        hub = DrynessIndexSensor(hass_mock, dict(HUB))
        zone = _zone(hass_mock, hub, **{CONF_ZONE_VWC_SENSOR: "sensor.orto_soil"})

        event = MagicMock()
        event.data = {"new_state": MagicMock(state="unavailable")}
        zone._on_own_probe(event)

        assert "probe_moisture_pct" not in zone.extra_state_attributes


class TestTheUpgrade:
    """What happens to the people who already have one."""

    def _entry(self, version, data):
        entry = MagicMock()
        entry.version = version
        entry.data = data
        return entry

    @pytest.mark.asyncio
    async def test_one_zone_needs_no_question(self, hass_mock):
        """With a single zone the probe is in it. Asking would be theatre."""
        from never_dry import async_migrate_entry

        entry = self._entry(
            3,
            {
                **HUB,
                CONF_VWC_SENSOR: "sensor.soil",
                CONF_ZONES: [{CONF_ZONE_NAME: "Orto", CONF_ZONE_AREA: 20.0}],
            },
        )
        captured = {}
        hass_mock.config_entries.async_update_entry = lambda e, **kw: captured.update(kw)

        assert await async_migrate_entry(hass_mock, entry) is True

        zones = captured["data"][CONF_ZONES]
        assert zones[0][CONF_ZONE_VWC_SENSOR] == "sensor.soil"
        assert CONF_VWC_SENSOR not in captured["data"]
        assert captured["version"] == CONFIG_VERSION

    @pytest.mark.asyncio
    async def test_several_zones_are_left_exactly_as_they_were(self, hass_mock):
        """Only the user knows where it is buried, so nothing is guessed.

        And nothing is deleted: removing the binding would degrade those zones
        to an estimate in silence and throw away an entity they had supplied.
        The installation keeps behaving as before while the question waits.
        """
        from never_dry import async_migrate_entry

        entry = self._entry(
            3,
            {
                **HUB,
                CONF_VWC_SENSOR: "sensor.soil",
                CONF_ZONES: [
                    {CONF_ZONE_NAME: "Orto", CONF_ZONE_AREA: 20.0},
                    {CONF_ZONE_NAME: "Prato", CONF_ZONE_AREA: 40.0},
                ],
            },
        )
        captured = {}
        hass_mock.config_entries.async_update_entry = lambda e, **kw: captured.update(kw)

        assert await async_migrate_entry(hass_mock, entry) is True

        assert captured["data"][CONF_VWC_SENSOR] == "sensor.soil"
        assert all(CONF_ZONE_VWC_SENSOR not in z for z in captured["data"][CONF_ZONES])

    @pytest.mark.asyncio
    async def test_an_installation_without_a_probe_is_not_disturbed(self, hass_mock):
        from never_dry import async_migrate_entry

        entry = self._entry(3, {**HUB, CONF_ZONES: [{CONF_ZONE_NAME: "Orto", CONF_ZONE_AREA: 20.0}]})
        captured = {}
        hass_mock.config_entries.async_update_entry = lambda e, **kw: captured.update(kw)

        assert await async_migrate_entry(hass_mock, entry) is True
        assert CONF_VWC_SENSOR not in captured["data"]


class TestTheQuestionAsked:
    """The repair issue: raised when it is needed, gone when it is not."""

    def _hass_with_registry(self, monkeypatch):
        import sys
        from types import SimpleNamespace

        created, deleted = [], []
        fake = SimpleNamespace(
            async_create_issue=lambda hass, domain, issue_id, **kw: created.append((issue_id, kw)),
            async_delete_issue=lambda hass, domain, issue_id: deleted.append(issue_id),
            IssueSeverity=SimpleNamespace(WARNING="warning"),
        )
        monkeypatch.setattr(sys.modules["homeassistant.helpers"], "issue_registry", fake, raising=False)
        monkeypatch.setitem(sys.modules, "homeassistant.helpers.issue_registry", fake)
        return created, deleted

    def _entry(self, data, entry_id="e1"):
        entry = MagicMock()
        entry.data = data
        entry.entry_id = entry_id
        return entry

    def test_it_is_raised_when_several_zones_share_one_probe(self, hass_mock, monkeypatch):
        from never_dry.repairs import async_check_soil_probe

        created, _ = self._hass_with_registry(monkeypatch)
        entry = self._entry(
            {
                CONF_VWC_SENSOR: "sensor.soil",
                CONF_ZONES: [{CONF_ZONE_NAME: "Orto"}, {CONF_ZONE_NAME: "Prato"}],
            }
        )

        async_check_soil_probe(hass_mock, entry)

        assert created and created[0][1]["is_fixable"] is True
        assert created[0][1]["translation_placeholders"]["probe"] == "sensor.soil"

    def test_it_is_not_raised_when_there_is_nothing_to_ask(self, hass_mock, monkeypatch):
        """One zone was migrated automatically; no probe means no question."""
        from never_dry.repairs import async_check_soil_probe

        created, deleted = self._hass_with_registry(monkeypatch)
        async_check_soil_probe(hass_mock, self._entry({CONF_ZONES: [{CONF_ZONE_NAME: "Orto"}]}))

        assert not created
        assert deleted  # and any stale one is cleared

    def test_answering_by_editing_the_zone_clears_it(self, hass_mock, monkeypatch):
        """The user may answer the question without ever opening the repair.

        Checked at every setup for this reason: someone who moves the probe into
        a zone by hand has answered it, and should not be asked again.
        """
        from never_dry.repairs import async_check_soil_probe

        created, deleted = self._hass_with_registry(monkeypatch)
        entry = self._entry(
            {
                CONF_ZONES: [
                    {CONF_ZONE_NAME: "Orto", CONF_ZONE_VWC_SENSOR: "sensor.soil"},
                    {CONF_ZONE_NAME: "Prato"},
                ]
            }
        )

        async_check_soil_probe(hass_mock, entry)

        assert not created
        assert deleted


class TestTheProbeDrivesOnceItIsToldWhatToReadItWith:
    """The two numbers are the switch, and they are also the responsibility.

    A fraction becomes millimetres by being multiplied by a root depth, and no
    root depth is right for every planting: the same 18 % reading is 18 mm under
    a lawn and 72 mm under a hedge. NeverDry cannot know which, the gardener can,
    and the act of typing the pair is the act of saying so. What the integration
    owes in return is to never pretend the number was measured when half of it
    was declared -- which is why the scaling is published beside the result.
    """

    def test_both_numbers_present_hands_the_deficit_to_the_soil(self, hass_mock):
        hub = DrynessIndexSensor(hass_mock, dict(HUB))
        zone = _zone(hass_mock, hub, **DRIVEN)
        zone._zone_deficit = 4.0

        zone._on_own_probe(_reading("18.0"))

        # (0.30 - 0.18) * 0.30 m * 1000
        assert zone._zone_deficit == pytest.approx(AT_18_PCT)
        assert zone.deficit_source == "zone_probe"

    def test_the_zone_numbers_are_used_and_not_the_site_ones(self, hass_mock):
        """The whole point of moving them onto the zone."""
        hub = DrynessIndexSensor(hass_mock, dict(HUB))
        zone = _zone(hass_mock, hub, **{**DRIVEN, CONF_ZONE_ROOT_DEPTH: 0.60})

        zone._on_own_probe(_reading("18.0"))

        assert zone._zone_deficit == pytest.approx(2 * AT_18_PCT)

    def test_what_scaled_the_reading_is_published_beside_it(self, hass_mock):
        """A declaration wearing the clothes of a measurement has to say so."""
        hub = DrynessIndexSensor(hass_mock, dict(HUB))
        zone = _zone(hass_mock, hub, **DRIVEN)
        zone._on_own_probe(_reading("18.0"))

        attrs = zone.extra_state_attributes

        assert attrs["deficit_source"] == "zone_probe"
        assert attrs["probe_root_depth_m"] == 0.30
        assert attrs["probe_field_capacity"] == 0.36
        assert attrs["probe_wilting_point"] == 0.22
        assert attrs["probe_reservoir_mm"] == pytest.approx(RESERVOIR_MM)

    def test_a_zone_with_no_probe_says_the_model_answers_for_it(self, hass_mock):
        hub = DrynessIndexSensor(hass_mock, dict(HUB))
        assert _zone(hass_mock, hub).extra_state_attributes["deficit_source"] == "site_model"


class TestTheModelKeepsRunningUnderneath:
    """The reserve is not rebuilt after the failure; it was never switched off.

    A probe fails when its battery does, which is to say without warning and
    usually in the dry half of the year. A reserve that started integrating at
    that moment would start from zero with the garden already thirsty, so the
    estimate advances the whole time, published or not.
    """

    def test_the_estimate_advances_while_the_probe_is_driving(self, hass_mock):
        hub = DrynessIndexSensor(hass_mock, dict(HUB))
        zone = _zone(hass_mock, hub, **DRIVEN)
        zone._zone_deficit = 1.0
        zone._on_own_probe(_reading("18.0"))

        zone._on_et_update(1.0, 0.3, 0.0)

        assert zone._et_deficit > 1.0, "the reserve stopped advancing"
        assert zone._zone_deficit == pytest.approx(AT_18_PCT), "the published number left the soil"

    def test_the_estimate_is_not_seeded_from_the_soil_each_tick(self, hass_mock):
        """The trap in sharing one accessor: the integration would restart from
        the probe every hour and the reserve would only ever be one tick old."""
        hub = DrynessIndexSensor(hass_mock, dict(HUB))
        zone = _zone(hass_mock, hub, **DRIVEN)
        zone._zone_deficit = 1.0
        zone._on_own_probe(_reading("18.0"))

        zone._on_et_update(1.0, 0.3, 0.0)
        first = zone._et_deficit
        zone._on_et_update(1.0, 0.3, 0.0)

        assert zone._et_deficit > first
        assert first < 36.0, "the reserve was seeded from the probe instead of integrating"


class TestAProbeThatStopsSpeaking:
    """The failure the freshness check exists for, and the only one that is silent.

    A probe whose battery dies keeps its last value on display for as long as
    anyone cares to look. Believed, it would report damp soil from June until
    September and the zone would never be watered again -- a fault that presents
    as nothing at all.
    """

    def _driven_zone(self, hass_mock):
        hub = DrynessIndexSensor(hass_mock, dict(HUB))
        zone = _zone(hass_mock, hub, **DRIVEN)
        zone._zone_deficit = 4.0
        zone._on_own_probe(_reading("18.0"))
        return zone

    def test_quiet_for_longer_than_it_has_ever_been_falls_back(self, hass_mock):
        zone = self._driven_zone(hass_mock)
        _has_come_back_from(zone, 300.0, 310.0, 305.0)
        zone._probe_last_seen = datetime.now(UTC) - timedelta(minutes=20)

        assert zone._probe_is_fresh() is False
        assert zone.deficit_source == "site_model"
        assert zone._zone_deficit == 4.0

    def test_quiet_within_its_own_cadence_is_believed(self, hass_mock):
        """The bar is this probe's habit, never a constant: one sensor speaks
        every thirty seconds and another twice a day."""
        zone = self._driven_zone(hass_mock)
        _has_come_back_from(zone, 300.0, 310.0, 305.0)
        zone._probe_last_seen = datetime.now(UTC) - timedelta(minutes=2)

        assert zone._probe_is_fresh() is True
        assert zone._zone_deficit == pytest.approx(AT_18_PCT)

    def test_with_no_cadence_yet_the_reading_still_stands(self, hass_mock):
        """No samples is not evidence of silence, and refusing a good reading
        would send the zone onto the estimate for no reason at all."""
        zone = self._driven_zone(hass_mock)

        assert zone._probe_quiet.value(datetime.now(UTC).timestamp()) is None
        assert zone._probe_is_fresh() is True

    def test_the_backstop_catches_a_probe_that_never_established_one(self, hass_mock):
        """The case its own bar cannot see: dead before it ever had a habit."""
        zone = self._driven_zone(hass_mock)
        zone._probe_last_seen = datetime.now(UTC) - timedelta(hours=48)

        assert zone._probe_quiet.value(datetime.now(UTC).timestamp()) is None
        assert zone._probe_is_fresh() is False
        assert zone._zone_deficit == 4.0

    def test_the_fall_is_said_out_loud_once(self, hass_mock, caplog):
        zone = self._driven_zone(hass_mock)
        zone._probe_last_seen = datetime.now(UTC) - timedelta(hours=48)

        zone._on_et_update(1.0, 0.3, 0.0)
        zone._on_et_update(1.0, 0.3, 0.0)

        assert sum("stopped reporting" in r.message for r in caplog.records) == 1

    def test_speaking_again_is_enough_to_be_believed_again(self, hass_mock):
        """No mode to leave: an afternoon of quiet costs an afternoon."""
        zone = self._driven_zone(hass_mock)
        zone._probe_last_seen = datetime.now(UTC) - timedelta(hours=48)
        assert zone.deficit_source == "site_model"

        zone._on_own_probe(_reading("18.0"))

        assert zone.deficit_source == "zone_probe"


class TestTheBarIsTheProbesHeartbeatNotTheSoilsMood:
    """Field, 2026-09-16: a healthy probe declared stopped every single night.

    Two zones on a real installation swung between the soil's number and the
    weather estimate with nothing physical happening in between. The cause was
    in how the bar was learned. A probe reports **on change**, so while the
    ground was drying after a watering it spoke every thirty seconds for an hour
    and a half, and those twenty-five readings filled a window counted in
    samples. The one gap that carried the device's real heartbeat -- 55 minutes,
    from earlier that evening -- was then the largest of twenty-five, and a 0.95
    quantile discards the largest of twenty-five. The bar came out at 33
    minutes. Thirty-four minutes after the last reading, the probe was called
    stopped.

    So the bar is the longest quiet the probe has come back from, and a burst of
    chatter can no longer lower it.
    """

    def _driven_zone(self, hass_mock):
        hub = DrynessIndexSensor(hass_mock, dict(HUB))
        zone = _zone(hass_mock, hub, **DRIVEN)
        zone._zone_deficit = 4.0
        zone._on_own_probe(_reading("18.0"))
        return zone

    def test_an_evening_of_chatter_does_not_lower_a_bar_the_heartbeat_set(self, hass_mock):
        """The reproduction, with the field's own numbers."""
        zone = self._driven_zone(hass_mock)
        _has_come_back_from(zone, 55 * 60)  # the heartbeat, seen once
        _has_come_back_from(zone, *([30.0] * 25))  # the soil drying, seen often

        zone._probe_last_seen = datetime.now(UTC) - timedelta(minutes=34)

        assert zone._probe_is_fresh() is True
        assert zone.deficit_source == "zone_probe"
        assert zone._zone_deficit == pytest.approx(AT_18_PCT)

    def test_quiet_longer_than_it_has_ever_managed_still_falls_back(self, hass_mock):
        """Erring long is not the same as never erring: the guard still fires."""
        zone = self._driven_zone(hass_mock)
        _has_come_back_from(zone, 55 * 60)

        zone._probe_last_seen = datetime.now(UTC) - timedelta(hours=3)

        assert zone._probe_is_fresh() is False
        assert zone._zone_deficit == 4.0

    def test_a_stretch_stops_counting_once_the_window_has_turned(self, hass_mock):
        """A one-off outage must not widen the bar for good.

        A Zigbee coordinator restart or a night of maintenance produces one
        enormous gap. Believed for ever, it would leave a dead probe unnoticed
        until the backstop; the week-long window is what lets it age out.
        """
        zone = self._driven_zone(hass_mock)
        now = datetime.now(UTC).timestamp()
        zone._probe_quiet.record(now - PROBE_CADENCE_MEMORY_S - 1, 12 * 3600)

        assert zone._probe_quiet.value(now) is None

    def test_the_bar_is_published_beside_the_verdict(self, hass_mock):
        """A zone on the estimate is a fact with no explanation without it."""
        zone = self._driven_zone(hass_mock)
        _has_come_back_from(zone, 55 * 60)

        attrs = zone.extra_state_attributes

        assert attrs["probe_quiet_bar_s"] == 55 * 60
        assert attrs["probe_fresh"] is True


class TestARestartKeepsTheMeasurement:
    """Field, 2026-09-16: every restart swapped the number the zone acts on.

    The live read at setup covers a reload, where the probe is up and has a
    state to read. It does not cover a full Home Assistant restart, where the
    probe's own integration has not set up yet: there is no state, nothing is
    recorded, and the zone falls onto the estimate until the probe next speaks
    -- forty minutes on the devices this was found on. The subscription cannot
    close that gap, because the event it waits for *is* the next reading.

    Observed as a deficit alternating between 2.6 mm and 0.15 mm across two
    restarts an hour apart, two numbers on two different scales with no
    physical event between them.
    """

    def _driven(self, hass_mock):
        hub = DrynessIndexSensor(hass_mock, dict(HUB))
        zone = _zone(hass_mock, hub, **DRIVEN)
        zone.hass = hass_mock
        # The restart condition itself: the probe's integration has not set up
        # yet, so there is no state for the live read at setup to find.
        hass_mock.states.get.return_value = None
        return zone

    @pytest.mark.asyncio
    async def test_a_reading_from_a_minute_ago_survives_a_restart(self, hass_mock):
        zone = self._driven(hass_mock)
        taken = datetime.now(UTC) - timedelta(minutes=1)
        zone.async_get_last_state = _last_state(
            {
                "estimate_mm": 4.0,
                "probe_moisture_pct": 18.0,
                "probe_last_seen": taken.isoformat(),
            }
        )

        await zone.async_added_to_hass()

        assert zone.deficit_source == "zone_probe"
        assert zone._zone_deficit == pytest.approx(AT_18_PCT)
        assert zone._zone.deficit.value_mm == pytest.approx(4.0), "the reserve is still the reserve"

    @pytest.mark.asyncio
    async def test_a_reading_from_last_week_stays_stale_through_it(self, hass_mock):
        """The timestamp is restored, never invented: otherwise a restart would
        launder every dead probe into a fresh one, which is worse than the bug
        being fixed."""
        zone = self._driven(hass_mock)
        taken = datetime.now(UTC) - timedelta(days=7)
        zone.async_get_last_state = _last_state(
            {
                "estimate_mm": 4.0,
                "probe_moisture_pct": 18.0,
                "probe_last_seen": taken.isoformat(),
            }
        )

        await zone.async_added_to_hass()

        assert zone._probe_is_fresh() is False
        assert zone.deficit_source == "site_model"
        assert zone._zone_deficit == pytest.approx(4.0)

    @pytest.mark.asyncio
    async def test_the_millimetres_are_recomputed_not_restored(self, hass_mock):
        """The edit that caused the reload may have been the ground itself."""
        zone = self._driven(hass_mock)
        zone.async_get_last_state = _last_state(
            {
                "estimate_mm": 4.0,
                "probe_moisture_pct": 18.0,
                "probe_implied_deficit_mm": 999.0,
                "probe_last_seen": datetime.now(UTC).isoformat(),
            }
        )

        await zone.async_added_to_hass()

        assert zone._probe_implied_mm == pytest.approx(AT_18_PCT)

    @pytest.mark.asyncio
    async def test_a_state_written_before_the_timestamp_existed_changes_nothing(self, hass_mock):
        """Upgrades land on states that carry the reading and not its age. An
        age that is not known is not zero, so the reading is simply not
        rehydrated and the zone waits for the probe, exactly as before."""
        zone = self._driven(hass_mock)
        zone.async_get_last_state = _last_state({"estimate_mm": 4.0, "probe_moisture_pct": 18.0})

        await zone.async_added_to_hass()

        assert zone._probe_last_seen is None
        assert zone.deficit_source == "site_model"


class TestWhatTheFormSaysAboutTheProbesRole:
    """The defect behind the report was a belief, not a number.

    Binding a probe to a zone reads as "this zone now waters by what the soil
    says". Nothing contradicted it, and the deficit went on coming from the
    weather.
    """

    def test_a_probe_without_a_root_depth_is_told_it_will_not_drive(self):
        from never_dry.config_flow import _probe_role_warnings

        warnings = _probe_role_warnings({CONF_ZONE_VWC_SENSOR: "sensor.soil"})

        assert len(warnings) == 1
        assert "will not set" in warnings[0]
        assert "root depth" in warnings[0]

    def test_a_named_soil_and_a_depth_say_nothing_because_the_choice_was_made(self):
        from never_dry.config_flow import _probe_role_warnings

        assert _probe_role_warnings(dict(DRIVEN)) == []

    def test_an_assumed_soil_is_named_rather_than_passed_over(self):
        """The price of the automatic entry, and the condition that makes it fair.

        A default nobody is told about is the hardcoded constant this replaced,
        with a dropdown standing in front of it.
        """
        from never_dry.config_flow import _probe_role_warnings

        warnings = _probe_role_warnings({CONF_ZONE_VWC_SENSOR: "sensor.soil", CONF_ZONE_ROOT_DEPTH: 0.3})

        assert len(warnings) == 1
        assert "medium soil" in warnings[0]

    def test_numbers_with_no_probe_to_read_are_flagged_as_unused(self):
        """The same shape as the ignored-override warnings: a value nobody reads."""
        from never_dry.config_flow import _probe_role_warnings

        warnings = _probe_role_warnings({CONF_ZONE_ROOT_DEPTH: 0.3, CONF_ZONE_FIELD_CAPACITY: 0.25})

        assert "will not be used" in warnings[0]

    def test_a_zone_with_neither_is_not_lectured(self):
        from never_dry.config_flow import _probe_role_warnings

        assert _probe_role_warnings({CONF_ZONE_NAME: "Orto"}) == []


def test_root_depth_is_a_length_and_crosses_the_unit_boundary():
    """Entered in inches on an imperial form, stored in metres like everything else."""
    from never_dry.unit_convert import zone_input_to_metric

    metric = zone_input_to_metric({CONF_ZONE_ROOT_DEPTH: 12.0}, True)

    assert metric[CONF_ZONE_ROOT_DEPTH] == pytest.approx(0.3048)


def test_field_capacity_is_a_fraction_and_does_not():
    from never_dry.unit_convert import zone_input_to_metric

    assert zone_input_to_metric({CONF_ZONE_FIELD_CAPACITY: 0.25}, True)[CONF_ZONE_FIELD_CAPACITY] == 0.25


class TestTheTriggerAndTheDoseAnswerTheSameQuestion:
    """The defect the first version shipped with, caught before any beta.

    The dose came from the entity property and the trigger from the domain
    object, and only one of the two had been pointed at the measurement. A zone
    whose soil said thirty millimetres and whose weather said two would never
    have started; one started by the weather would have been dosed by the soil.
    One number now, read through `acting_deficit`.
    """

    def _zone_with(self, hass_mock, *, estimate_mm, reading):
        hub = DrynessIndexSensor(hass_mock, dict(HUB))
        zone = _zone(hass_mock, hub, **DRIVEN)
        zone._zone.threshold_mm = 10.0
        zone._zone_deficit = estimate_mm
        zone._on_own_probe(_reading(reading))
        return zone

    def test_dry_soil_asks_for_water_though_the_weather_is_calm(self, hass_mock):
        zone = self._zone_with(hass_mock, estimate_mm=2.0, reading="18.0")  # soil: 34.44 mm

        assert zone.domain_zone.needs_water is True

    def test_wet_soil_keeps_a_thirsty_estimate_quiet(self, hass_mock):
        """The direction that matters most: not watering is the irreversible half."""
        zone = self._zone_with(hass_mock, estimate_mm=30.0, reading="88.0")  # soil: 5.04 mm

        assert zone.domain_zone.needs_water is False

    def test_the_dose_is_taken_from_the_same_number(self, hass_mock):
        zone = self._zone_with(hass_mock, estimate_mm=2.0, reading="18.0")

        assert zone._zone.acting_deficit.value_mm == pytest.approx(AT_18_PCT)
        assert zone._zone.water_demand_l > zone._zone.deficit.as_liters(zone._zone.area_m2)


class TestWaterTheSoilHasNotSeenYet:
    """A stateless measurement does not fall when you water: it falls when it
    next reports, and on a battery device that is minutes away.

    Left standing it would go on saying the zone is dry and, in reactive mode,
    ask for the same water again before the soil had a chance to disagree. The
    rule applied is the one the rest of the integration already follows: a
    measurement that cannot have observed the event does not get to speak
    about it.
    """

    def _watered(self, hass_mock):
        hub = DrynessIndexSensor(hass_mock, dict(HUB))
        zone = _zone(hass_mock, hub, **DRIVEN)
        zone._zone.threshold_mm = 10.0
        zone._zone_deficit = 12.0
        zone._on_own_probe(_reading("18.0"))
        assert zone.deficit_source == "zone_probe"
        zone.credit_delivery(zone._zone.water_demand_l)
        return zone

    def test_a_delivery_withdraws_the_measurement(self, hass_mock):
        assert self._watered(hass_mock)._zone.measured_deficit is None

    def test_the_estimate_answers_until_a_fresh_reading_arrives(self, hass_mock):
        zone = self._watered(hass_mock)

        assert zone.deficit_source == "site_model"

    def test_the_zone_does_not_immediately_ask_again(self, hass_mock):
        """The re-irrigation loop this exists to prevent: with the measurement
        left standing the trigger would fire on the very next broadcast, and the
        only thing in its way is a ten-second limit on service calls."""
        zone = self._watered(hass_mock)

        assert zone.domain_zone.needs_water is False

    def test_a_fresh_reading_gives_the_soil_its_voice_back(self, hass_mock):
        zone = self._watered(hass_mock)

        zone._on_own_probe(_reading("18.0"))

        assert zone.deficit_source == "zone_probe"
        assert zone.domain_zone.needs_water is True

    def test_the_measurement_carries_the_frame_of_a_measurement(self, hass_mock):
        """Not ET: one patch of soil, not comparable with a sibling's."""
        from never_dry.water_balance_model import ReferenceFrame

        hub = DrynessIndexSensor(hass_mock, dict(HUB))
        zone = _zone(hass_mock, hub, **DRIVEN)
        zone._on_own_probe(_reading("18.0"))

        assert zone._zone.acting_deficit.frame is ReferenceFrame.VWC_PER_ZONE
        assert zone._zone.deficit.frame is ReferenceFrame.ET

    def test_marking_the_zone_irrigated_is_not_overruled_by_the_soil(self, hass_mock):
        """The same rule at the other door, and this one has a button behind it.

        `mark_irrigated` asserts an outcome rather than crediting an amount: the
        user says the zone is full. A measurement taken before the water arrived
        would have won over that zero the moment anyone asked, and the button
        would have looked broken.
        """
        hub = DrynessIndexSensor(hass_mock, dict(HUB))
        zone = _zone(hass_mock, hub, **DRIVEN)
        zone._on_own_probe(_reading("18.0"))
        assert zone._zone_deficit == pytest.approx(AT_18_PCT)

        zone.reset_deficit(source="manual")

        assert zone._zone_deficit == 0.0
        assert zone.deficit_source == "site_model"


class TestTheGroundIsChosenNotTyped:
    """Field capacity and wilting point are one piece of information, not two.

    They come off the same texture table and a gardener has neither to hand, so
    asking for both as figures asks twice for something nobody owns. One choice
    supplies them, which leaves the root depth as the only number the user must
    give -- and that is the right one to have kept, because no table holds it: a
    lawn, a hedge and a pot differ by a factor of four on the same ground.
    """

    def _zone(self, hass_mock, **cfg):
        hub = DrynessIndexSensor(hass_mock, dict(HUB))
        zone = _zone_of(hass_mock, hub, **{CONF_ZONE_VWC_SENSOR: "sensor.orto_soil", **cfg})
        zone._on_own_probe(_reading("18.0"))
        return zone

    def test_a_depth_alone_is_enough_because_the_soil_has_a_default(self, hass_mock):
        zone = self._zone(hass_mock, **{CONF_ZONE_ROOT_DEPTH: 0.30})

        assert zone._probe_drives is True
        assert zone.deficit_source == "zone_probe"
        # medium soil: (1 - 0.18) * (0.25 - 0.12) * 0.30 m * 1000
        assert zone._zone_deficit == pytest.approx(31.98)

    def test_without_a_depth_no_default_can_rescue_it(self, hass_mock):
        """The switch is the depth, and nothing stands in for it."""
        zone = self._zone(hass_mock)

        assert zone._probe_drives is False
        assert zone.deficit_source == "site_model"

    def test_sand_and_clay_are_not_the_same_garden(self, hass_mock):
        """The whole reason the choice is worth offering: same reading, same
        roots, and a reservoir that differs by more than a factor of three.

        This test used to read:

            assert sandy._zone_deficit == pytest.approx(-9.0 + 9.0)
            # (0.15 - 0.18) < 0, clamped to 0

        which is GH #234 written down and blessed. It spelled out the negative
        bracket, spelled out the clamp, and asserted that a sandy zone reading
        18 % needs no water at all - for ever, at any root depth. Two users then
        reported exactly that as a bug. A test that states the arithmetic it
        watched happen is not a guard; the question it never asked was whether
        zero was a defensible answer.
        """
        sandy = self._zone(hass_mock, **{CONF_ZONE_ROOT_DEPTH: 0.30, CONF_ZONE_SOIL_TYPE: SOIL_TYPE_SANDY})
        clay = self._zone(hass_mock, **{CONF_ZONE_ROOT_DEPTH: 0.30, CONF_ZONE_SOIL_TYPE: SOIL_TYPE_CLAY})

        # (1 - 0.18) * (0.15 - 0.06) * 0.30 * 1000, and the same on clay's
        # wider interval. Both are real quantities of missing water.
        assert sandy._zone_deficit == pytest.approx(22.14)
        assert clay._zone_deficit == pytest.approx(34.44)
        assert clay._zone_deficit > sandy._zone_deficit

        # And neither soil can be talked into "full" by a reading like this one.
        assert sandy._zone_deficit > 0.0

    def test_the_assumed_soil_is_published_with_the_number_it_produced(self, hass_mock):
        zone = self._zone(hass_mock, **{CONF_ZONE_ROOT_DEPTH: 0.30})

        attrs = zone.extra_state_attributes

        assert attrs["probe_soil_type"] == "auto"
        assert attrs["probe_field_capacity"] == pytest.approx(0.25)

    def test_a_named_soil_drives_the_zone(self, hass_mock):
        zone = self._zone(hass_mock, **DRIVEN)

        assert zone._probe_drives is True
        assert zone._zone_deficit == pytest.approx(AT_18_PCT)

    def test_custom_soil_cannot_drive_the_probe(self, hass_mock):
        """Custom gives a field capacity and no wilting point, and half an
        interval is not a reservoir.

        Inventing the missing end would be the tempting move and the wrong one:
        the ratio between the two is not constant across soils (0.40 on sand,
        0.48 on loam, 0.61 on clay), so a derived wilting point would put a made
        -up number under a figure the user reads as a measurement. The zone keeps
        its estimate, the probe stays telemetry, and the form says so.
        """
        zone = self._zone(
            hass_mock,
            **{
                CONF_ZONE_ROOT_DEPTH: 0.30,
                CONF_ZONE_SOIL_TYPE: SOIL_TYPE_CUSTOM,
                CONF_ZONE_FIELD_CAPACITY: 0.30,
            },
        )

        assert zone._probe_drives is False
        assert zone.deficit_source == "site_model"

    def test_custom_soil_with_an_empty_box_is_refused_by_the_form(self):
        """The preset/override contract, fourth application: Custom says the
        value is mine to give, and without it the zone would fall back to a
        neutral default and behave as if nothing had been chosen."""
        from never_dry.config_flow import _override_errors

        errors = _override_errors({CONF_ZONE_SOIL_TYPE: SOIL_TYPE_CUSTOM})

        assert errors.get(CONF_ZONE_FIELD_CAPACITY) == "field_capacity_required"

    def test_a_named_soil_makes_the_box_dead_weight_and_says_so(self):
        from never_dry.config_flow import _ignored_override_warnings

        warnings = _ignored_override_warnings({CONF_ZONE_SOIL_TYPE: SOIL_TYPE_CLAY, CONF_ZONE_FIELD_CAPACITY: 0.30})

        assert any("Field capacity" in w for w in warnings)


class TestTheScaleIsOneScale:
    """GH #234: the reading is on the 0-100 scale, and on no other.

    Two installations reported a zone stuck at zero deficit for good. The cause
    was not a bad value: it was that the reading and the soil table were being
    treated as the same quantity. A garden probe reports where the ground sits
    between dry and wet on its own scale; the table holds volumetric water
    content. Subtract one from the other and a wet reading makes the bracket
    negative, the clamp calls it zero, and zero is indistinguishable on screen
    from soil that has just been watered.

    So the scale is now declared by the product rather than guessed per reading,
    which is what these tests hold.
    """

    def _driven(self, hass_mock):
        hub = DrynessIndexSensor(hass_mock, dict(HUB))
        return _zone(hass_mock, hub, **DRIVEN)

    def test_a_wet_reading_leaves_a_small_deficit_not_no_deficit(self, hass_mock):
        """The reporter's own case, on the maintainer's own probes: 94 %.

        Under the old arithmetic this was a negative bracket on every soil in
        the table, so the zone never watered again. Now it is what it should
        always have been: nearly full, with a little missing.
        """
        zone = self._driven(hass_mock)

        zone._on_own_probe(_reading("94.0"))

        assert zone._zone_deficit == pytest.approx(RESERVOIR_MM * 0.06)
        assert zone._zone_deficit > 0.0

    def test_only_a_hundred_means_no_deficit(self, hass_mock):
        zone = self._driven(hass_mock)

        zone._on_own_probe(_reading("100"))

        assert zone._zone_deficit == 0.0

    def test_an_empty_soil_asks_for_the_whole_reservoir(self, hass_mock):
        zone = self._driven(hass_mock)

        zone._on_own_probe(_reading("0"))

        assert zone._zone_deficit == pytest.approx(RESERVOIR_MM)

    def test_one_per_cent_is_the_driest_soil_and_not_a_full_one(self, hass_mock):
        """The trap in the reader this replaces, and the worst one available.

        ``vwc_to_fraction`` treats anything at or below 1.0 as "already a
        fraction", which is a reasonable guess when the scale is unknown. Here
        the scale is known, and that guess would read ``1`` - one per cent, the
        driest reading a probe can give - as a saturated soil, and send the
        deficit to zero on the one zone that needs water most.
        """
        zone = self._driven(hass_mock)

        zone._on_own_probe(_reading("1"))

        assert zone._zone_deficit == pytest.approx(RESERVOIR_MM * 0.99)

    def test_a_reading_off_the_scale_withdraws_the_measurement(self, hass_mock):
        """A raw ADC count is not a wet soil, and holding it is worse than dropping it.

        The previous behaviour held the last good value, which kept the
        measurement *fresh* for the freshness guard and so kept a broken probe
        in charge for ever. The estimate underneath is a worse number than a
        working probe and a far better one than a wrong probe.
        """
        zone = self._driven(hass_mock)
        zone._on_own_probe(_reading("18.0"))
        assert zone.deficit_source == "zone_probe"

        zone._zone.deficit = zone._zone.deficit.with_value(7.0)
        zone._on_own_probe(_reading("310"))  # Ecowitt raw count on some firmwares

        assert zone.deficit_source == "site_model"
        assert zone._zone_deficit == pytest.approx(7.0)

    def test_a_rejected_reading_stops_being_displayed(self, hass_mock):
        """Not just unused: gone from the attributes.

        A stale percentage left on the card reads as the current state of the
        soil, which is the same class of lie as the deficit that started this.
        """
        zone = self._driven(hass_mock)
        zone._on_own_probe(_reading("18.0"))
        assert "probe_moisture_pct" in zone.extra_state_attributes

        zone._on_own_probe(_reading("310"))

        assert "probe_moisture_pct" not in zone.extra_state_attributes

    def test_a_negative_reading_is_not_a_reading(self, hass_mock):
        zone = self._driven(hass_mock)

        zone._on_own_probe(_reading("-5"))

        assert zone.deficit_source == "site_model"

    def test_the_probe_is_believed_again_once_it_talks_sense(self, hass_mock):
        zone = self._driven(hass_mock)
        zone._on_own_probe(_reading("500"))
        assert zone.deficit_source == "site_model"

        zone._on_own_probe(_reading("18.0"))

        assert zone.deficit_source == "zone_probe"
        assert zone._zone_deficit == pytest.approx(AT_18_PCT)


class TestARestartKeepsTheReserve:
    """GH #234, second defect: the restart used to overwrite the estimate.

    The published ``deficit_mm`` is the number the zone acts on, which while a
    probe drives is the soil's. Restoring it into ``_zone_deficit`` writes the
    *estimate*, so every Home Assistant restart replaced the weather reserve
    with whatever the probe happened to say - zero, in the case that started
    this. Field evidence: one zone at 0.03 mm while its three siblings, through
    the same restart, held 0.31, 1.52 and 2.38.

    The reserve is what the zone falls back on the moment the probe stops being
    believed, so a reserve rebuilt from zero arrives with the garden already dry.
    """

    def _driven(self, hass_mock):
        hub = DrynessIndexSensor(hass_mock, dict(HUB))
        return _zone(hass_mock, hub, **DRIVEN)

    def test_the_estimate_is_published_separately_from_what_is_on_display(self, hass_mock):
        zone = self._driven(hass_mock)
        zone._zone.deficit = zone._zone.deficit.with_value(8.0)
        zone._on_own_probe(_reading("100"))  # soil says full

        attrs = zone.extra_state_attributes

        assert attrs["deficit_mm"] == 0.0, "on display: the soil's answer"
        assert attrs["estimate_mm"] == pytest.approx(8.0), "kept: the weather reserve"

    @pytest.mark.asyncio
    async def test_a_restart_restores_the_reserve_and_not_the_measurement(self, hass_mock):
        zone = self._driven(hass_mock)
        zone.hass = hass_mock
        zone.async_get_last_state = _last_state({"deficit_mm": 0.0, "estimate_mm": 8.0})

        await zone.async_added_to_hass()

        assert zone._zone.deficit.value_mm == pytest.approx(8.0)

    @pytest.mark.asyncio
    async def test_an_old_state_without_the_reserve_still_restores(self, hass_mock):
        """States written before ``estimate_mm`` existed carry only the one
        number. For a zone with no probe the two are equal, so nothing is lost;
        for a zone with one this is the last restart that can inherit the bug."""
        zone = self._driven(hass_mock)
        zone.hass = hass_mock
        zone.async_get_last_state = _last_state({"deficit_mm": 5.0})

        await zone.async_added_to_hass()

        assert zone._zone.deficit.value_mm == pytest.approx(5.0)


class TestThePlaceholderReachesTheForm:
    """A placeholder the flow does not supply is rendered to the user verbatim.

    The link to the probe document cannot live in the string: hassfest refuses a
    URL inside a translation and says to use a description placeholder instead.
    That moves the value into the code, which means the two can now drift, and
    the drift is visible to the user as the literal text `{soil_doc}` under the
    field. Worse than no link, and no existing test would see it.
    """

    CATALOGUES = (
        "strings.json",
        "translations/en.json",
        "translations/it.json",
        "translations/de.json",
        "translations/es.json",
    )

    def _flow_source(self):
        from pathlib import Path

        import never_dry.config_flow as cf

        return Path(cf.__file__).read_text(encoding="utf-8")

    def _catalogue(self, name):
        import json
        from pathlib import Path

        import never_dry

        return json.loads((Path(never_dry.__file__).parent / name).read_text(encoding="utf-8"))

    def test_every_placeholder_in_a_description_is_passed_by_the_flow(self):
        import re

        source = self._flow_source()
        missing = []
        for name in self.CATALOGUES:
            catalogue = self._catalogue(name)
            for root in ("config", "options"):
                for step_id, step in catalogue.get(root, {}).get("step", {}).items():
                    texts = list(step.get("data_description", {}).values())
                    for section in step.get("sections", {}).values():
                        texts += list(section.get("data_description", {}).values())
                    for text in texts:
                        for ph in re.findall(r"\{(\w+)\}", text):
                            if f'"{ph}"' not in source:
                                missing.append(f"{name}:{root}.{step_id} -> {{{ph}}}")
        assert not missing, "placeholders no code path supplies: " + ", ".join(sorted(set(missing)))

    def test_the_probe_description_carries_the_link_as_a_placeholder(self):
        """Both halves, so neither can be dropped on its own."""
        catalogue = self._catalogue("strings.json")
        text = catalogue["config"]["step"]["zone"]["sections"]["ground_and_location"]["data_description"]["vwc_sensor"]

        assert "{soil_doc}" in text, "the description must reference the document"
        assert "https://" not in text, "a URL in the string is what hassfest refuses"
        assert "soil-moisture-model.md" in self._flow_source(), "and the flow must supply it"
