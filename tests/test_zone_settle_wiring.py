"""The zone settles itself — the first behaviour to move onto the domain object.

`Zone.settle()` had been written, tested and reached by nothing: both settle
sites in the controller wrote the seven counter fields by hand instead. Two
copies of the same bookkeeping, and they were not identical — the commanded
path rolled the yearly total on a new year only in its *full*-delivery branch,
so a partial delivery on 1 January added to last year's figure.

These tests hold the seam itself: that both paths go through the object, and
that the behaviour which differed between the copies now has one answer.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock

from never_dry.const import (
    CONF_ZONE_AREA,
    CONF_ZONE_EFFICIENCY,
    CONF_ZONE_FLOW_RATE,
    CONF_ZONE_NAME,
    CONF_ZONE_SYSTEM_TYPE,
    CONF_ZONE_VALVE,
    SYSTEM_TYPE_CUSTOM,
)
from never_dry.controller import IrrigationController
from never_dry.sensor import IrrigationZoneSensor

# ── Helpers ───────────────────────────────────────────────────────────


def _zone(hass, di_sensor) -> IrrigationZoneSensor:
    """A zone with round numbers: 10 m² at efficiency 1.0, so 1 L is 0.1 mm."""
    return IrrigationZoneSensor(
        hass,
        {
            CONF_ZONE_NAME: "Orto",
            CONF_ZONE_VALVE: "switch.valve_orto",
            CONF_ZONE_AREA: 10.0,
            CONF_ZONE_SYSTEM_TYPE: SYSTEM_TYPE_CUSTOM,
            CONF_ZONE_EFFICIENCY: 1.0,
            CONF_ZONE_FLOW_RATE: 8.0,
        },
        di_sensor,
    )


def _settle_partial(controller, zone, *, delivered: float, target: float, duration_s: int = 120):
    """Run the commanded settle path with a delivery short of its target."""
    ts_end = datetime.now()
    ts_start = ts_end - timedelta(seconds=duration_s)
    controller._settle_irrigated_zones(
        [("Orto", delivered, target, zone._zone_deficit, ts_start, ts_end)],
    )


# ── The commanded path ────────────────────────────────────────────────


class TestPartialDeliverySettlesThroughTheZone:
    def test_every_counter_moves_in_one_call(self, hass_mock, di_sensor):
        zone = _zone(hass_mock, di_sensor)
        controller = IrrigationController(hass_mock, di_sensor, [zone])
        zone._zone_deficit = 5.0
        zone.begin_cycle()

        _settle_partial(controller, zone, delivered=20.0, target=50.0)

        assert zone._last_volume_delivered == 20.0
        assert zone._session_water_delivered == 20.0
        assert zone._total_water_delivered == 20.0
        assert zone._yearly_water_delivered == 20.0
        assert zone._last_irrigation_source == "automatic"
        assert zone._last_irrigated is not None

    def test_the_cycle_snapshot_is_dropped(self, hass_mock, di_sensor):
        """Left set, the next credit would subtract from a stale baseline."""
        zone = _zone(hass_mock, di_sensor)
        controller = IrrigationController(hass_mock, di_sensor, [zone])
        zone._zone_deficit = 5.0
        zone.begin_cycle()
        assert zone._deficit_at_irrigation_start == 5.0

        _settle_partial(controller, zone, delivered=20.0, target=50.0)

        assert zone._deficit_at_irrigation_start is None

    def test_credit_comes_off_the_cycle_snapshot(self, hass_mock, di_sensor):
        """20 L over 10 m² at efficiency 1.0 is 2 mm off the 5 mm we started from."""
        zone = _zone(hass_mock, di_sensor)
        controller = IrrigationController(hass_mock, di_sensor, [zone])
        zone._zone_deficit = 5.0
        zone.begin_cycle()

        _settle_partial(controller, zone, delivered=20.0, target=50.0)

        assert zone._zone_deficit == 3.0

    def test_the_session_duration_is_recorded(self, hass_mock, di_sensor):
        zone = _zone(hass_mock, di_sensor)
        controller = IrrigationController(hass_mock, di_sensor, [zone])
        zone._zone_deficit = 5.0
        zone.begin_cycle()

        _settle_partial(controller, zone, delivered=20.0, target=50.0, duration_s=137)

        assert zone._last_session_duration_s == 137


class TestYearlyTotalRollsOnAPartialDelivery:
    """The behaviour the two copies disagreed about.

    The full-delivery branch rolled the yearly counter on a new year; the
    partial branch only ever added to it. A zone whose last delivery was in
    December and whose first of the new year fell short of target carried the
    old total forward — the counter is `total_increasing`, so the jump also
    reached the statistics.
    """

    def test_a_new_year_clears_the_previous_total(self, hass_mock, di_sensor):
        zone = _zone(hass_mock, di_sensor)
        controller = IrrigationController(hass_mock, di_sensor, [zone])
        zone._yearly_water_delivered = 8000.0
        zone._yearly_water_year = datetime.now().year - 1
        zone._zone_deficit = 5.0
        zone.begin_cycle()

        _settle_partial(controller, zone, delivered=20.0, target=50.0)

        assert zone._yearly_water_delivered == 20.0
        assert zone._yearly_water_year == datetime.now().year

    def test_the_lifetime_total_is_preserved(self, hass_mock, di_sensor):
        """Only the yearly figure rolls — the lifetime one never resets."""
        zone = _zone(hass_mock, di_sensor)
        controller = IrrigationController(hass_mock, di_sensor, [zone])
        zone._total_water_delivered = 8000.0
        zone._yearly_water_delivered = 8000.0
        zone._yearly_water_year = datetime.now().year - 1
        zone._zone_deficit = 5.0
        zone.begin_cycle()

        _settle_partial(controller, zone, delivered=20.0, target=50.0)

        assert zone._total_water_delivered == 8020.0

    def test_the_same_year_keeps_accumulating(self, hass_mock, di_sensor):
        zone = _zone(hass_mock, di_sensor)
        controller = IrrigationController(hass_mock, di_sensor, [zone])
        zone._yearly_water_delivered = 100.0
        zone._yearly_water_year = datetime.now().year
        zone._zone_deficit = 5.0
        zone.begin_cycle()

        _settle_partial(controller, zone, delivered=20.0, target=50.0)

        assert zone._yearly_water_delivered == 120.0


# ── The manual path ───────────────────────────────────────────────────


class TestManualSessionSettlesThroughTheZone:
    """The other former copy: a valve opened outside NeverDry's control."""

    def _finalize(self, controller, zone, *, delivered_liters: float):
        """Drive the manual close with a measurable volume, no meter involved."""
        controller._manual_valve_open["switch.valve_orto"] = None
        controller._manual_session_meta["switch.valve_orto"] = (
            datetime.now() - timedelta(seconds=60),
            zone._zone_deficit,
        )
        # No flow meter and no elapsed-time estimate: inject the volume directly
        # by giving the zone a flow rate and a known elapsed window.
        zone._flow_rate = delivered_liters  # L/min over the 60 s window below
        controller._finalize_manual_session("switch.valve_orto", "Orto", zone)

    def test_source_is_manual_and_counters_move(self, hass_mock, di_sensor):
        zone = _zone(hass_mock, di_sensor)
        controller = IrrigationController(hass_mock, di_sensor, [zone])
        zone._zone_deficit = 5.0
        hass_mock.states.get = MagicMock(return_value=MagicMock(state="off"))

        self._finalize(controller, zone, delivered_liters=20.0)

        assert zone._last_irrigation_source == "manual"
        assert zone._total_water_delivered > 0
        assert zone._session_water_delivered > 0

    def test_a_close_never_clears_the_whole_deficit(self, hass_mock, di_sensor):
        """The rule this path exists to keep: only mark_irrigated zeroes it."""
        zone = _zone(hass_mock, di_sensor)
        controller = IrrigationController(hass_mock, di_sensor, [zone])
        zone._zone_deficit = 50.0
        hass_mock.states.get = MagicMock(return_value=MagicMock(state="off"))

        self._finalize(controller, zone, delivered_liters=5.0)

        assert zone._zone_deficit > 0.0

    def test_a_new_year_rolls_here_too(self, hass_mock, di_sensor):
        """Both settle sites share one answer now, which was the point."""
        zone = _zone(hass_mock, di_sensor)
        controller = IrrigationController(hass_mock, di_sensor, [zone])
        zone._zone_deficit = 50.0
        zone._yearly_water_delivered = 8000.0
        zone._yearly_water_year = datetime.now().year - 1
        hass_mock.states.get = MagicMock(return_value=MagicMock(state="off"))

        self._finalize(controller, zone, delivered_liters=20.0)

        assert zone._yearly_water_delivered < 8000.0
        assert zone._yearly_water_year == datetime.now().year


# ── The completed delivery and the hose ───────────────────────────────


class TestAFinishedZoneIsClearedNotCredited:
    """The distinction that took a decision: outcome known, not amount known."""

    def test_a_full_delivery_lands_on_exactly_zero(self, hass_mock, di_sensor):
        """Not near zero. The target came from the deficit, so it clears it."""
        zone = _zone(hass_mock, di_sensor)
        controller = IrrigationController(hass_mock, di_sensor, [zone])
        zone._zone_deficit = 5.0
        zone.begin_cycle()

        _settle_partial(controller, zone, delivered=50.0, target=50.0)

        assert zone._zone_deficit == 0.0

    def test_the_measured_volume_is_credited_not_the_demand(self, hass_mock, di_sensor):
        """A metered cycle has already emptied the deficit, so demand reads ~0."""
        zone = _zone(hass_mock, di_sensor)
        controller = IrrigationController(hass_mock, di_sensor, [zone])
        zone._zone_deficit = 0.05  # depleted live during the cycle
        zone.begin_cycle()

        _settle_partial(controller, zone, delivered=50.0, target=50.0)

        assert zone._last_volume_delivered == 50.0

    def test_the_session_duration_survives(self, hass_mock, di_sensor):
        zone = _zone(hass_mock, di_sensor)
        controller = IrrigationController(hass_mock, di_sensor, [zone])
        zone._zone_deficit = 5.0
        zone.begin_cycle()

        _settle_partial(controller, zone, delivered=50.0, target=50.0, duration_s=97)

        assert zone._last_session_duration_s == 97

    def test_the_hose_case_infers_the_volume_from_the_deficit(self, hass_mock, di_sensor):
        """Nothing was measured, so what was missing is what went in."""
        zone = _zone(hass_mock, di_sensor)
        zone._zone_deficit = 4.0  # 4 mm over 10 m2 at efficiency 1.0 = 40 L

        zone.reset_deficit("mark_irrigated")

        assert zone._zone_deficit == 0.0
        assert zone._last_volume_delivered == 40.0

    def test_the_hose_case_claims_no_duration(self, hass_mock, di_sensor):
        """Writing 0 would assert a session length nobody measured."""
        zone = _zone(hass_mock, di_sensor)
        zone._last_session_duration_s = 120
        zone._zone_deficit = 4.0

        zone.reset_deficit("mark_irrigated")

        assert zone._last_session_duration_s == 120

    def test_clearing_rolls_the_year_too(self, hass_mock, di_sensor):
        """The fourth copy of the roll-over lived here."""
        zone = _zone(hass_mock, di_sensor)
        zone._yearly_water_delivered = 8000.0
        zone._yearly_water_year = datetime.now().year - 1
        zone._zone_deficit = 4.0

        zone.reset_deficit("mark_irrigated")

        assert zone._yearly_water_delivered == 40.0
        assert zone._yearly_water_year == datetime.now().year
