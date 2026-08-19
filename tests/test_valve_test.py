"""The supervised valve test: what it measures, and when it refuses to run.

Both refusals below were written after the feature's first live failures, an hour
apart on the same garden. The first run worked and found a real defect (360 L/h
measured against 200 declared). The second was started moments after a restart,
when neither the valve nor its meter had reported yet: the open command was lost
on the radio, no water moved, the valve was held "open" for a full minute in the
software's belief, and nothing was learned.

The user's own reading was "I was hasty". That is the wrong lesson. A feature
that puts water on the ground must not depend on the operator's timing — so the
test now checks that it can *see* before it opens anything.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from never_dry import valve_test


class _Level:
    """Closed until the valve is opened, then open. What a working valve does.

    A plain lambda cannot express this, and the first version of these tests used
    one: it answered "on" to the pre-flight check, which the code correctly reads
    as "this valve has not reported a closed state" and refuses. The tests were
    testing the refusal without meaning to.
    """

    def __init__(self):
        self.opened = False

    def open(self):
        self.opened = True

    def close(self):
        self.opened = False

    def __call__(self):
        return "on" if self.opened else "off"


def _hass_with(meter_value=None, unit="L"):
    hass = MagicMock()
    state = None
    if meter_value is not None:
        state = MagicMock(state=str(meter_value), attributes={"unit_of_measurement": unit})
    hass.states.get = MagicMock(return_value=state)
    return hass


class TestItRefusesToRunBlind:
    """Water must not run when nothing can be observed."""

    @pytest.mark.asyncio
    async def test_a_valve_that_has_not_reported_is_not_opened(self):
        opened = AsyncMock()
        result = await valve_test.run_valve_test(
            _hass_with(10),
            zone_name="Z",
            valve_entity="switch.z",
            meter_entity="sensor.m",
            open_valve=opened,
            close_valve=AsyncMock(),
            read_level=lambda: "unknown",
        )
        opened.assert_not_awaited()
        assert any("refused" in n for n in result.notes)
        assert result.volume_l is None

    @pytest.mark.asyncio
    async def test_a_meter_with_no_reading_is_not_worth_a_minute_of_water(self):
        opened = AsyncMock()
        result = await valve_test.run_valve_test(
            _hass_with(None),
            zone_name="Z",
            valve_entity="switch.z",
            meter_entity="sensor.m",
            open_valve=opened,
            close_valve=AsyncMock(),
            read_level=lambda: "off",
        )
        opened.assert_not_awaited()
        assert any("meter has no reading" in n for n in result.notes)

    @pytest.mark.asyncio
    async def test_a_zone_with_no_meter_at_all_still_runs(self):
        """Latency alone is a legitimate result — refusing here would be too strict."""
        level = _Level()
        opened = AsyncMock(side_effect=lambda: level.open())
        result = await valve_test.run_valve_test(
            _hass_with(None),
            zone_name="Z",
            valve_entity="switch.z",
            meter_entity=None,
            duration_s=0.2,
            open_valve=opened,
            close_valve=AsyncMock(),
            read_level=level,
        )
        opened.assert_awaited()
        assert result.volume_l is None


class TestAnUnconfirmedOpenAbortsInsteadOfWaiting:
    @pytest.mark.asyncio
    async def test_it_gives_up_and_still_closes(self, monkeypatch):
        """Sixty seconds of belief is not a measurement; the close is not optional."""
        monkeypatch.setattr(valve_test, "CONFIRM_TIMEOUT_S", 0.2)
        closed = AsyncMock()
        result = await valve_test.run_valve_test(
            _hass_with(10),
            zone_name="Z",
            valve_entity="switch.z",
            meter_entity="sensor.m",
            duration_s=30,
            open_valve=AsyncMock(),
            close_valve=closed,
            read_level=lambda: "off",  # never becomes "on"
        )
        closed.assert_awaited()
        assert any("never confirmed open" in n for n in result.notes)
        assert result.measured_lpm is None


class TestWhatItMeasuresWhenItCanSee:
    """Readings are injected past ``flow_utils``, on purpose.

    The first version fed a list through ``hass.states.get``, and the unit lookups
    consumed entries from it too — so every value arrived shifted and a 4 L volume
    appeared out of a counter that had reset. Same trap that broke three tests
    earlier the same day: a shared mock advanced by callers nobody counted.
    """

    @staticmethod
    def _with_readings(monkeypatch, values):
        it = iter(values)
        monkeypatch.setattr(valve_test, "SAMPLE_INTERVAL_S", 0.02)
        monkeypatch.setattr(valve_test.flow_utils, "is_flow_rate_sensor", lambda *_a: False)
        monkeypatch.setattr(valve_test.flow_utils, "get_flow_meter_unit", lambda *_a: "L")
        monkeypatch.setattr(valve_test.flow_utils, "read_volume_liters", lambda *_a: next(it, None))

    @pytest.mark.asyncio
    async def test_a_counter_gives_volume_flow_and_its_own_resolution(self, monkeypatch):
        self._with_readings(monkeypatch, [100, 100, 101, 102, 103, 103, 104] + [104] * 200)
        level = _Level()
        result = await valve_test.run_valve_test(
            MagicMock(),
            zone_name="Z",
            valve_entity="switch.z",
            meter_entity="sensor.m",
            duration_s=0.3,
            open_valve=AsyncMock(side_effect=lambda: level.open()),
            close_valve=AsyncMock(side_effect=lambda: level.close()),
            read_level=level,
        )
        assert result.volume_l is not None and result.volume_l > 0
        assert result.smallest_step == 1.0
        assert result.updates >= 2

    @pytest.mark.asyncio
    async def test_a_counter_that_resets_mid_run_reports_nothing_rather_than_a_negative(self, monkeypatch):
        """A decrease is never delivery — the counter reset, so the run measured nothing."""
        self._with_readings(monkeypatch, [500, 500, 3, 5, 7] + [7] * 200)
        level = _Level()
        result = await valve_test.run_valve_test(
            MagicMock(),
            zone_name="Z",
            valve_entity="switch.z",
            meter_entity="sensor.m",
            duration_s=0.3,
            open_valve=AsyncMock(side_effect=lambda: level.open()),
            close_valve=AsyncMock(side_effect=lambda: level.close()),
            read_level=level,
        )
        assert result.volume_l is None
        assert any("reset" in n for n in result.notes)

    def test_the_configured_rate_cannot_even_be_passed_in(self):
        """The module's rule, held structurally instead of by searching for words.

        Searching the source was the first attempt and it failed twice: `_flow_rate`
        matches `is_flow_rate_sensor`, which *reads a sensor* — a measurement, and
        forbidding measurement was exactly backwards. The signature is the honest
        place: if no parameter can carry the declared rate, the function cannot
        consult it, whatever it does inside.
        """
        import inspect

        params = set(inspect.signature(valve_test.run_valve_test).parameters)
        assert params == {
            "hass",
            "zone_name",
            "valve_entity",
            "meter_entity",
            "duration_s",
            "open_valve",
            "close_valve",
            "read_level",
        }


class TestAnAbortedRunMeasuresNothingRatherThanZero:
    """Found in the field the same evening, on the valve with the known mesh trouble.

    The open command was lost, the run aborted correctly — and then the arithmetic
    ran anyway on a baseline equal to the final reading, producing `volume_l = 0.0`:
    a *measurement of zero litres* where there had been no measurement. It also
    dragged in a "below the limit of detection" note about a run that never
    happened. Absence is not zero; this module says so in its own docstring, and
    now the code agrees.
    """

    @pytest.mark.asyncio
    async def test_no_volume_no_rate_no_lod_verdict(self, monkeypatch):
        monkeypatch.setattr(valve_test, "CONFIRM_TIMEOUT_S", 0.2)
        monkeypatch.setattr(valve_test.flow_utils, "is_flow_rate_sensor", lambda *_a: False)
        monkeypatch.setattr(valve_test.flow_utils, "get_flow_meter_unit", lambda *_a: "L")
        monkeypatch.setattr(valve_test.flow_utils, "read_volume_liters", lambda *_a: 0.0)

        result = await valve_test.run_valve_test(
            MagicMock(),
            zone_name="Z",
            valve_entity="switch.z",
            meter_entity="sensor.m",
            duration_s=30,
            open_valve=AsyncMock(),
            close_valve=AsyncMock(),
            read_level=lambda: "off",  # confirms closed, never opens
        )
        assert result.volume_l is None
        assert result.measured_lpm is None
        assert not any("limit of detection" in n for n in result.notes)
        assert any("no retry was attempted" in n for n in result.notes)


class TestTheResultSurvivesTheActOfUsingIt:
    """Saving the measured value must not erase the measurement.

    Field sequence, 17 August: the test measured 360 L/h on one zone, the user
    pressed *Save measured flow rate*, and the number disappeared — from every
    zone, not just that one. Writing the configuration reloads the entry, the
    entities are recreated, and the result lived only in memory. The evidence for
    an action was destroyed by taking the action.
    """

    @pytest.mark.asyncio
    async def test_it_is_restored_from_the_attributes_it_published(self, hass_mock, di_sensor, monkeypatch):
        from unittest.mock import AsyncMock as _AsyncMock

        from never_dry.const import CONF_ZONE_DELIVERY_MODE, CONF_ZONE_FLOW_METER_SENSOR, DELIVERY_MODE_FLOW_METER

        from tests.test_delivery_modes import _make_zone

        published = _make_zone(
            hass_mock,
            di_sensor,
            **{
                CONF_ZONE_DELIVERY_MODE: DELIVERY_MODE_FLOW_METER,
                CONF_ZONE_FLOW_METER_SENSOR: "sensor.meter",
            },
        )
        published.record_valve_test({"zone_name": published.zone_name, "measured_lpm": 6.0, "volume_l": 6.0})
        attrs = published.extra_state_attributes
        assert attrs["valve_test_measured_lpm"] == 6.0  # it went out…

        reborn = _make_zone(
            hass_mock,
            di_sensor,
            **{
                CONF_ZONE_DELIVERY_MODE: DELIVERY_MODE_FLOW_METER,
                CONF_ZONE_FLOW_METER_SENSOR: "sensor.meter",
            },
        )
        reborn.async_get_last_state = _AsyncMock(return_value=MagicMock(state="0", attributes=attrs))
        reborn.hass = hass_mock
        await reborn.async_added_to_hass()

        assert reborn.last_valve_test is not None  # …and it came back
        assert reborn.last_valve_test["measured_lpm"] == 6.0
