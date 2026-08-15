"""Reload safety: everything the integration subscribes to must be released.

Home Assistant does not free a listener registered with
``async_track_state_change_event`` when the entity that made it goes away —
the entity has to hand the unsubscribe handle to ``async_on_remove``. Nor does
dropping an object from ``hass.data`` release what that object subscribed to.

Both were missing, and neither is visible in a single-setup test: the failure
only appears on the *second* setup. Every options-flow save reloads the entry,
so the count grew with each edit — two hubs advancing two water balances from
the same temperature feed, and two operators watching one valve, each with a
watchdog able to force it closed under the successor legitimately driving it.

These tests exercise the removal path itself, which nothing did before.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from never_dry.const import (
    CONF_ZONE_AREA,
    CONF_ZONE_FLOW_RATE,
    CONF_ZONE_NAME,
    CONF_ZONE_VALVE,
)
from never_dry.controller import IrrigationController
from never_dry.sensor import ETSensor, IrrigationZoneSensor, ZoneLinkedSensor
from never_dry.valve_fsm import FsmConfig
from never_dry.valve_operator import ValveOperator

# ── Helpers ───────────────────────────────────────────────────────────


def _distinct_unsubs(monkeypatch, module_path: str) -> list[MagicMock]:
    """Make the tracker hand out a *fresh* unsub per call, and collect them.

    The shared stub returns one MagicMock for every subscription, so "was an
    unsub called" cannot distinguish which. These tests are about exactly that
    distinction — the old subscription released, the new one still live.
    """
    handles: list[MagicMock] = []

    def _track(*_args, **_kwargs):
        handle = MagicMock(name=f"unsub_{len(handles)}")
        handles.append(handle)
        return handle

    monkeypatch.setattr(f"{module_path}.async_track_state_change_event", _track)
    return handles


def _valve_operator(hass, switch: str, *, flow: str | None = None) -> ValveOperator:
    """A real operator on ``switch``, with timeouts short enough to be harmless."""
    return ValveOperator(
        hass=hass,
        switch_entity_id=switch,
        flow_sensor_entity_id=flow,
        zone_name=switch,
        fsm_config=FsmConfig(
            has_flow_meter=flow is not None,
            open_timeout_s=0.05,
            close_timeout_s=0.05,
            flow_verify_timeout_s=0.05,
            leak_timeout_s=0.05,
        ),
        max_retries=0,
    )


def _zone(hass, di_sensor, name: str, valve: str) -> IrrigationZoneSensor:
    return IrrigationZoneSensor(
        hass,
        {
            CONF_ZONE_NAME: name,
            CONF_ZONE_VALVE: valve,
            CONF_ZONE_AREA: 10.0,
            CONF_ZONE_FLOW_RATE: 8.0,
        },
        di_sensor,
    )


# ── Entity listeners ──────────────────────────────────────────────────


class TestEntityListenersAreReleasedOnRemoval:
    """Each entity must hand its unsubscribe handle to ``async_on_remove``."""

    async def test_et_sensor(self, hass_mock, base_config, monkeypatch):
        handles = _distinct_unsubs(monkeypatch, "never_dry.sensor")
        sensor = ETSensor(hass_mock, base_config)

        await sensor.async_added_to_hass()
        assert len(handles) == 1, "the temperature listener was not registered"
        assert not handles[0].called

        sensor.run_on_remove_callbacks()
        assert handles[0].called, "removing the ET sensor left it subscribed to temperature"

    async def test_dryness_index(self, di_sensor, monkeypatch):
        handles = _distinct_unsubs(monkeypatch, "never_dry.sensor")
        di_sensor.async_get_last_state = AsyncMock(return_value=None)

        await di_sensor.async_added_to_hass()
        assert len(handles) == 1

        di_sensor.run_on_remove_callbacks()
        assert handles[0].called, "removing the hub left it broadcasting to its zone listeners"

    async def test_zone_linked_sensor(self, hass_mock, monkeypatch):
        handles = _distinct_unsubs(monkeypatch, "never_dry.sensor")
        sensor = ZoneLinkedSensor(hass_mock, "switch.valve_orto", "Valve", "mdi:valve", "linked_valve_orto")
        sensor.hass = hass_mock

        await sensor.async_added_to_hass()
        assert len(handles) == 1

        sensor.run_on_remove_callbacks()
        assert handles[0].called


# ── Valve operators ───────────────────────────────────────────────────


class TestControllerStopDetachesOperators:
    """``async_stop`` is the only unload hook — the operators must die with it."""

    async def test_unloads_every_operator(self, hass_mock, di_sensor):
        zone_a = _zone(hass_mock, di_sensor, "Orto", "switch.valve_orto")
        zone_b = _zone(hass_mock, di_sensor, "Prato", "switch.valve_prato")
        operators = {"switch.valve_orto": MagicMock(), "switch.valve_prato": MagicMock()}
        controller = IrrigationController(hass_mock, di_sensor, [zone_a, zone_b], valve_operators=operators)

        await controller.async_stop()

        for entity_id, operator in operators.items():
            assert operator.async_unload.called, f"operator for {entity_id} was never detached"

    async def test_clears_the_operator_map(self, hass_mock, di_sensor):
        zone = _zone(hass_mock, di_sensor, "Orto", "switch.valve_orto")
        controller = IrrigationController(
            hass_mock, di_sensor, [zone], valve_operators={"switch.valve_orto": MagicMock()}
        )

        await controller.async_stop()

        assert controller.valve_operators == {}, "a stopped controller still advertises live operators"

    async def test_detach_runs_after_the_manual_session_settles(self, hass_mock, di_sensor):
        """Order matters: settling an open manual session still needs the FSM.

        Detaching first would send the closing command outside the operator, so
        the valve would be closed without its state machine ever knowing.
        """
        zone = _zone(hass_mock, di_sensor, "Orto", "switch.valve_orto")
        operator = MagicMock()
        operator.close = AsyncMock(return_value=MagicMock(status=MagicMock(value="ok")))
        order: list[str] = []
        operator.close.side_effect = lambda: order.append("close") or MagicMock(status=MagicMock(value="ok"))
        operator.async_unload.side_effect = lambda: order.append("unload")

        controller = IrrigationController(hass_mock, di_sensor, [zone], valve_operators={"switch.valve_orto": operator})
        # A manual session is open and the switch still reads on.
        controller._manual_valve_open["switch.valve_orto"] = None
        hass_mock.states.get = MagicMock(return_value=MagicMock(state="on"))

        await controller.async_stop()

        assert order == ["close", "unload"], f"expected close before unload, got {order}"

    async def test_reload_leaves_only_the_successor_subscribed(self, hass_mock, di_sensor, monkeypatch):
        """The regression itself, in the shape it takes in the field.

        Stop the first controller the way an entry unload does, build the
        second, and check that the switch has exactly one live subscriber.
        """
        handles = _distinct_unsubs(monkeypatch, "never_dry.valve_operator")
        zone = _zone(hass_mock, di_sensor, "Orto", "switch.valve_orto")

        old_operator = _valve_operator(hass_mock, "switch.valve_orto")
        old_controller = IrrigationController(
            hass_mock, di_sensor, [zone], valve_operators={"switch.valve_orto": old_operator}
        )
        assert len(handles) == 1

        await old_controller.async_stop()

        new_operator = _valve_operator(hass_mock, "switch.valve_orto")
        IrrigationController(hass_mock, di_sensor, [zone], valve_operators={"switch.valve_orto": new_operator})

        assert len(handles) == 2, "the successor did not subscribe"
        assert handles[0].called, "the old operator is still watching the valve after the reload"
        assert not handles[1].called, "the successor was detached instead"

    async def test_watchdog_of_a_stopped_operator_cannot_fire(self, hass_mock, di_sensor):
        """The consequence that makes the leak dangerous, not merely untidy."""
        operator = _valve_operator(hass_mock, "switch.valve_orto")
        operator._max_open_duration_s = 0.05
        operator._watchdog_task = asyncio.get_running_loop().create_task(operator._watchdog())
        await asyncio.sleep(0)  # let the task reach its sleep

        zone = _zone(hass_mock, di_sensor, "Orto", "switch.valve_orto")
        controller = IrrigationController(hass_mock, di_sensor, [zone], valve_operators={"switch.valve_orto": operator})
        await controller.async_stop()
        await asyncio.sleep(0.15)  # well past the watchdog's own deadline

        assert operator._watchdog_task is None
        turn_offs = [
            call for call in hass_mock.services.async_call.call_args_list if call.args[:2] == ("switch", "turn_off")
        ]
        assert not turn_offs, "a detached operator's watchdog still closed the valve"


@pytest.mark.parametrize("entity_id", ["switch.valve_orto", "switch.valve_prato"])
async def test_operator_unload_is_idempotent(hass_mock, di_sensor, entity_id):
    """A second stop must not raise: unload order is not fully under our control."""
    zone = _zone(hass_mock, di_sensor, "Orto", entity_id)
    controller = IrrigationController(hass_mock, di_sensor, [zone], valve_operators={entity_id: MagicMock()})

    await controller.async_stop()
    await controller.async_stop()
