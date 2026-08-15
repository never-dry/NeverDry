"""Reachability is a different fault from failure, and the card has to say so.

A valve that never confirms a command is a radio problem the user can act on:
move the device, add a router, change the batteries. A valve that confirms and
moves no water is hydraulic: the supply is off, the filter is clogged. The FSM
has always separated the two — `OPEN_FAILED` and `CLOSE_VERIFICATION_FAILED`
are the comms class, `ACTUATION_FAILED` and `CLOSE_LEAK` are not — but nothing
above it did, so both looked the same from the dashboard: nothing.

Field case behind these tests ('Giardino Pino'): a Zigbee valve that drops off
the mesh periodically keeps reporting a perfectly ordinary `off`, so it never
becomes `unavailable`. Pressing Irrigate looked like it did nothing for 48
seconds; six unanswered attempts later the zone was blocked.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from never_dry.const import (
    CONF_ZONE_AREA,
    CONF_ZONE_FLOW_RATE,
    CONF_ZONE_NAME,
    CONF_ZONE_VALVE,
    VALVE_STARTUP_GRACE_S,
)
from never_dry.sensor import IrrigationZoneSensor, ZoneDeficitSensor
from never_dry.valve_fsm import FailureKind, ValveState

# ── Helpers ───────────────────────────────────────────────────────────


def _zone(hass, di_sensor, *, valve: str | None = "switch.valve_orto"):
    cfg = {CONF_ZONE_NAME: "Orto", CONF_ZONE_AREA: 10.0, CONF_ZONE_FLOW_RATE: 8.0}
    if valve:
        cfg[CONF_ZONE_VALVE] = valve
    return IrrigationZoneSensor(hass, cfg, di_sensor)


def _operator(*, state=ValveState.IDLE, last_failure=None):
    op = MagicMock()
    op.state = state
    op.is_in_maintenance = state == ValveState.MAINTENANCE
    op.last_failure = last_failure
    return op


def _switch(hass, value: str | None):
    """Make the zone's switch read ``value``; ``None`` means the entity is gone."""
    hass.states.get = MagicMock(return_value=None if value is None else MagicMock(state=value))


def _settled(zone):
    """Push the zone past its startup grace, as a running installation is.

    Most tests are about steady state; the grace has its own class below.
    """
    zone._created_at -= VALVE_STARTUP_GRACE_S + 1
    return zone


# ── No evidence either way ────────────────────────────────────────────


class TestNoEvidenceIsNotAFault:
    """``None`` must never be drawn as a warning — it means we do not know."""

    def test_a_zone_without_a_valve(self, hass_mock, di_sensor):
        zone = _zone(hass_mock, di_sensor, valve=None)
        assert zone.valve_reachable is None

    def test_the_attribute_is_omitted_rather_than_published_false(self, hass_mock, di_sensor):
        zone = _zone(hass_mock, di_sensor, valve=None)
        assert "valve_reachable" not in zone.extra_state_attributes


# ── The entity says the device is gone ────────────────────────────────


class TestTheEntityItselfReportsUnavailable:
    @pytest.mark.parametrize("value", ["unavailable", "unknown"])
    def test_unavailable_states(self, hass_mock, di_sensor, value):
        zone = _settled(_zone(hass_mock, di_sensor))
        zone.set_operator(_operator())
        _switch(hass_mock, value)
        assert zone.valve_reachable is False

    def test_missing_entity(self, hass_mock, di_sensor):
        zone = _settled(_zone(hass_mock, di_sensor))
        zone.set_operator(_operator())
        _switch(hass_mock, None)
        assert zone.valve_reachable is False

    def test_fsm_in_unreachable(self, hass_mock, di_sensor):
        """The same fact seen from our side rather than the integration's."""
        zone = _settled(_zone(hass_mock, di_sensor))
        zone.set_operator(_operator(state=ValveState.UNREACHABLE))
        _switch(hass_mock, "off")
        assert zone.valve_reachable is False


# ── The startup grace ─────────────────────────────────────────────────


class TestStartupGrace:
    """Zigbee entities are unavailable for a minute or two after a restart.

    Without a grace, three zones out of four raised the warning on every
    restart — observed twice on a live instance. An alarm that cries wolf after
    every reboot is the surest way to teach the user to ignore it, and every
    options-flow save is a reload.
    """

    def test_a_fresh_zone_says_unknown_rather_than_broken(self, hass_mock, di_sensor):
        zone = _zone(hass_mock, di_sensor)
        zone.set_operator(_operator())
        _switch(hass_mock, "unavailable")
        assert zone.valve_reachable is None

    def test_a_fresh_zone_with_the_fsm_unreachable_also_says_unknown(self, hass_mock, di_sensor):
        """How it really presents at startup: entity not loaded, FSM following it.

        The two arrive together — the operator drives the FSM to UNREACHABLE
        from the same `unavailable` the entity is reporting — so a test that
        pairs an alive entity with an unreachable FSM would be describing a
        state that cannot occur.
        """
        zone = _zone(hass_mock, di_sensor)
        zone.set_operator(_operator(state=ValveState.UNREACHABLE))
        _switch(hass_mock, "unavailable")
        assert zone.valve_reachable is None

    def test_the_unknown_is_not_published_as_a_fault(self, hass_mock, di_sensor):
        """The card draws nothing rather than an amber triangle."""
        zone = _zone(hass_mock, di_sensor)
        zone.set_operator(_operator())
        _switch(hass_mock, "unavailable")
        assert "valve_reachable" not in zone.extra_state_attributes

    def test_the_window_expires(self, hass_mock, di_sensor):
        zone = _zone(hass_mock, di_sensor)
        zone.set_operator(_operator())
        _switch(hass_mock, "unavailable")
        assert zone.valve_reachable is None

        zone._created_at -= VALVE_STARTUP_GRACE_S + 1
        assert zone.valve_reachable is False

    def test_seeing_the_valve_once_closes_the_window_early(self, hass_mock, di_sensor):
        """A valve up at twenty seconds and gone at two minutes is reported at once.

        The grace covers the absence of evidence at startup, not the first five
        minutes indiscriminately.
        """
        zone = _zone(hass_mock, di_sensor)
        zone.set_operator(_operator())

        _switch(hass_mock, "off")  # seen alive, well inside the window
        assert zone.valve_reachable is True

        _switch(hass_mock, "unavailable")
        assert zone.valve_reachable is False

    def test_a_failed_command_outranks_the_grace(self, hass_mock, di_sensor):
        """Active evidence is proof, and proof is never suspended.

        Pressing Irrigate thirty seconds after a restart and watching six
        attempts go unanswered is a finding, not a startup artefact.
        """
        zone = _zone(hass_mock, di_sensor)
        zone.set_operator(_operator(last_failure=FailureKind.OPEN_FAILED))
        _switch(hass_mock, "off")
        assert zone.valve_reachable is False


# ── The case that actually bites ──────────────────────────────────────


class TestCommandsThatGoUnanswered:
    """The valve reports a normal level and still answers nothing."""

    @pytest.mark.parametrize(
        "failure",
        [FailureKind.OPEN_FAILED, FailureKind.CLOSE_VERIFICATION_FAILED],
    )
    def test_comms_failures_mean_unreachable(self, hass_mock, di_sensor, failure):
        zone = _zone(hass_mock, di_sensor)
        zone.set_operator(_operator(last_failure=failure))
        _switch(hass_mock, "off")  # exactly what the flaky valve keeps reporting
        assert zone.valve_reachable is False

    @pytest.mark.parametrize("failure", [FailureKind.ACTUATION_FAILED, FailureKind.CLOSE_LEAK])
    def test_physical_failures_are_not_a_reachability_problem(self, hass_mock, di_sensor, failure):
        """The distinction the whole feature rests on.

        The valve answered — it is on the mesh. Telling the user to check the
        radio link would send them after the wrong fault entirely.
        """
        zone = _zone(hass_mock, di_sensor)
        zone.set_operator(_operator(last_failure=failure))
        _switch(hass_mock, "off")
        assert zone.valve_reachable is True

    def test_a_clean_cycle_clears_the_warning(self, hass_mock, di_sensor):
        """The FSM drops last_failure on any clean cycle, so recovery is automatic."""
        zone = _zone(hass_mock, di_sensor)
        operator = _operator(last_failure=FailureKind.OPEN_FAILED)
        zone.set_operator(operator)
        _switch(hass_mock, "off")
        assert zone.valve_reachable is False

        operator.last_failure = None
        assert zone.valve_reachable is True


class TestWithoutAnOperator:
    def test_volume_preset_trusts_the_entity(self, hass_mock, di_sensor):
        """Smart valves bypass the operator, so the entity is all the evidence there is."""
        zone = _zone(hass_mock, di_sensor)
        _switch(hass_mock, "off")
        assert zone.valve_reachable is True

    def test_and_still_reports_an_unavailable_entity(self, hass_mock, di_sensor):
        zone = _settled(_zone(hass_mock, di_sensor))
        _switch(hass_mock, "unavailable")
        assert zone.valve_reachable is False


# ── What the card actually reads ──────────────────────────────────────


class TestPublishedAttributes:
    """The card takes its chips from the Deficit sensor first, Volume second."""

    def test_deficit_sensor_carries_the_flag(self, hass_mock, di_sensor):
        zone = _zone(hass_mock, di_sensor)
        zone.set_operator(_operator(last_failure=FailureKind.OPEN_FAILED))
        _switch(hass_mock, "off")

        attrs = ZoneDeficitSensor(zone).extra_state_attributes

        assert attrs["valve_reachable"] is False
        assert attrs["valve_last_failure"] == "open_failed"

    def test_volume_sensor_carries_it_too(self, hass_mock, di_sensor):
        zone = _zone(hass_mock, di_sensor)
        zone.set_operator(_operator(last_failure=FailureKind.OPEN_FAILED))
        _switch(hass_mock, "off")

        attrs = zone.extra_state_attributes

        assert attrs["valve_reachable"] is False
        assert attrs["valve_last_failure"] == "open_failed"

    def test_last_failure_is_null_after_a_clean_cycle(self, hass_mock, di_sensor):
        zone = _zone(hass_mock, di_sensor)
        zone.set_operator(_operator())
        _switch(hass_mock, "off")

        assert zone.extra_state_attributes["valve_last_failure"] is None
        assert zone.extra_state_attributes["valve_reachable"] is True
