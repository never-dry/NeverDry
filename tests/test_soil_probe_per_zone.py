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

from unittest.mock import MagicMock

import pytest
from never_dry.const import (
    CONF_RAIN_SENSOR,
    CONF_TEMP_SENSOR,
    CONF_VWC_SENSOR,
    CONF_ZONE_AREA,
    CONF_ZONE_NAME,
    CONF_ZONE_VWC_SENSOR,
    CONF_ZONES,
    CONFIG_VERSION,
)
from never_dry.sensor import DrynessIndexSensor, IrrigationZoneSensor

HUB = {CONF_TEMP_SENSOR: "sensor.t", CONF_RAIN_SENSOR: "sensor.r"}


def _zone(hass, dryness, **cfg):
    return IrrigationZoneSensor(hass, {CONF_ZONE_NAME: "Orto", CONF_ZONE_AREA: 20.0, **cfg}, dryness)


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
        assert attrs["probe_water_content"] == pytest.approx(0.18)
        # Published beside it: the gap between this and the model's deficit after
        # an irrigation is what reveals a delivery that moved no water.
        assert attrs["probe_implied_deficit_mm"] == pytest.approx(36.0)

    def test_the_reading_does_not_touch_the_deficit(self, hass_mock):
        """The rejected design, kept as a test so it cannot come back by accident."""
        hub = DrynessIndexSensor(hass_mock, dict(HUB))
        zone = _zone(hass_mock, hub, **{CONF_ZONE_VWC_SENSOR: "sensor.orto_soil"})
        zone._zone_deficit = 4.0

        event = MagicMock()
        event.data = {"new_state": MagicMock(state="18.0")}
        zone._on_own_probe(event)

        assert zone._zone_deficit == 4.0

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

        assert zone.extra_state_attributes["probe_water_content"] == pytest.approx(0.45)

    def test_an_unreadable_probe_publishes_nothing(self, hass_mock):
        """A missing reading is not a dry soil, and not a wet one either."""
        hub = DrynessIndexSensor(hass_mock, dict(HUB))
        zone = _zone(hass_mock, hub, **{CONF_ZONE_VWC_SENSOR: "sensor.orto_soil"})

        event = MagicMock()
        event.data = {"new_state": MagicMock(state="unavailable")}
        zone._on_own_probe(event)

        assert "probe_water_content" not in zone.extra_state_attributes


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
