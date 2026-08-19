"""Tests for the async half of the driver — commands, delivery, and the master.

The other driver test file covers the pure surface. This one covers the part that
needs a Home Assistant loop: issuing a command and awaiting its confirmation,
delivering water, and the master valve's off-linger.

It matters more than the line count suggests. ``driver.py`` supersedes
``valve_operator.py``, which sits at 99% — but it is not a copy: it shares under
half its lines with the operator and nearly doubles them. The Driver/ZoneDriver/
MasterDriver hierarchy, the delivery strategies and the entity adapter are new
code that no existing test reaches, and they are where valve closure is decided.

Harness follows ``test_valve_operator.py``: a mocked hass, tiny FSM timeouts, and
state confirmations pushed in by hand through ``_on_switch_state``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from never_dry.driver import (
    DeliveryMode,
    DeliveryQuality,
    MasterDriver,
    OperationResult,
    OperationStatus,
    ZoneDriver,
)
from never_dry.valve_fsm import FailureKind, FsmConfig, ValveState
from never_dry.valve_notifier import NotificationKind, Severity, ValveNotifier


@pytest.fixture
def hass():
    """Mock HomeAssistant instance suitable for driver tests."""
    hass = MagicMock()
    hass.states = MagicMock()
    hass.states.get = MagicMock(return_value=MagicMock(state="off"))
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()

    def _create_task(coro):
        try:
            return asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            coro.close()
            return MagicMock()

    hass.async_create_task = _create_task
    return hass


def _fast_fsm() -> FsmConfig:
    """FSM config with tiny timeouts, so a test does not wait on real seconds."""
    return FsmConfig(
        has_flow_meter=False,
        open_timeout_s=0.05,
        close_timeout_s=0.05,
        flow_verify_timeout_s=0.05,
        leak_timeout_s=0.05,
        max_consecutive_failures=3,
    )


def _zone_driver(hass, *, entity_id="switch.valve", flow_rate=60.0, **kwargs) -> ZoneDriver:
    return ZoneDriver(
        hass,
        entity_id,
        delivery_mode=DeliveryMode.ESTIMATED_FLOW,
        flow_rate_lpm=flow_rate,
        fsm_config=_fast_fsm(),
        max_retries=0,
        backoff_s=(0.01,),
        name="testzone",
        **kwargs,
    )


class _Meter:
    """A settable meter reading, so a test can move it at a chosen moment."""

    def __init__(self, value: str, unit: str) -> None:
        self.value = value
        self.unit = unit

    def set(self, value: str) -> None:
        self.value = value


def _wire_meter(hass, value: str, *, unit: str) -> _Meter:
    """Make sensor.flow read `value` with an explicit unit, and return a handle.

    The unit is what tells the driver whether it is holding a rate or a
    cumulative counter, and the two are judged by different questions. The
    handle lets a test advance the counter mid-recovery, which is the only way
    to distinguish a real leak from a meter that simply has a large total.
    """
    meter = _Meter(value, unit)

    def states_get(entity_id):
        state = MagicMock()
        state.state = meter.value
        state.attributes = {"unit_of_measurement": meter.unit}
        return state

    hass.states.get = MagicMock(side_effect=states_get)
    return meter


def _state_event(value: str) -> MagicMock:
    event = MagicMock()
    event.data = {"new_state": MagicMock(state=value)}
    return event


async def _yield_loop(times: int = 3) -> None:
    for _ in range(times):
        await asyncio.sleep(0)


async def _confirm(driver, value: str) -> None:
    """Push a state confirmation in, the way HA would."""
    await _yield_loop()
    driver._on_switch_state(_state_event(value))
    await _yield_loop()


def _wire_flowing_valve(hass, *, rate_lpm: float) -> None:
    """Make the mock read as an OPEN valve with water running through the meter.

    Both halves matter: the delivery loop stops the moment the valve reads off,
    so a harness that reports "off" for everything silently exercises the
    fallback path instead of the metered one.
    """

    def states_get(entity_id):
        if entity_id == "sensor.flow":
            state = MagicMock()
            state.state = str(rate_lpm)
            state.attributes = {"unit_of_measurement": "L/min"}
            return state
        return MagicMock(state="on")

    hass.states.get = MagicMock(side_effect=states_get)


class TestCommands:
    """Opening and closing, and the entity adapter picking the right service."""

    async def test_open_confirms_on_a_switch(self, hass):
        driver = _zone_driver(hass)

        async def simulate():
            await _confirm(driver, "on")

        sim = asyncio.create_task(simulate())
        result = await driver.async_turn_on()
        await sim

        assert result.status is OperationStatus.OK
        assert driver.is_open
        hass.services.async_call.assert_any_call("switch", "turn_on", {"entity_id": "switch.valve"}, blocking=False)

    async def test_open_uses_the_valve_service_for_a_valve_entity(self, hass):
        """The GH #94 payoff: a `valve.*` timer is driven without any config migration."""
        driver = _zone_driver(hass, entity_id="valve.bhyve")

        async def simulate():
            await _confirm(driver, "open")

        sim = asyncio.create_task(simulate())
        result = await driver.async_turn_on()
        await sim

        assert result.status is OperationStatus.OK
        hass.services.async_call.assert_any_call("valve", "open_valve", {"entity_id": "valve.bhyve"}, blocking=False)

    async def test_close_confirms(self, hass):
        driver = _zone_driver(hass)

        async def open_then_close():
            await _confirm(driver, "on")

        sim = asyncio.create_task(open_then_close())
        await driver.async_turn_on()
        await sim

        async def simulate_close():
            await _confirm(driver, "off")

        sim2 = asyncio.create_task(simulate_close())
        result = await driver.async_turn_off()
        await sim2

        assert result.status is OperationStatus.OK
        assert not driver.is_open

    async def test_an_unconfirmed_open_fails_rather_than_assuming_success(self, hass):
        """No confirmation must never read as an open valve — the whole safety premise."""
        driver = _zone_driver(hass)
        result = await driver.async_turn_on()
        assert result.status is OperationStatus.FAILED
        assert not driver.is_open

    async def test_repeated_failures_lock_the_valve_in_maintenance(self, hass):
        """After the configured failures the zone is blocked and waits for a human."""
        driver = _zone_driver(hass)
        for _ in range(3):
            await driver.async_turn_on()
        assert driver.is_in_maintenance
        assert driver.state is ValveState.MAINTENANCE

    async def test_maintenance_can_be_cleared(self, hass):
        driver = _zone_driver(hass)
        for _ in range(3):
            await driver.async_turn_on()
        await driver.async_reset_maintenance()
        assert not driver.is_in_maintenance

    async def test_a_command_is_refused_while_in_maintenance(self, hass):
        driver = _zone_driver(hass)
        for _ in range(3):
            await driver.async_turn_on()
        hass.services.async_call.reset_mock()
        result = await driver.async_turn_on()
        assert result.status is OperationStatus.MAINTENANCE
        hass.services.async_call.assert_not_called()

    async def test_unload_is_safe_to_call(self, hass):
        driver = _zone_driver(hass)
        driver.async_unload()
        assert not driver.is_open


class TestDelivery:
    """`deliver()` returns how much water arrived and how truthful that figure is."""

    async def test_nothing_requested_delivers_nothing(self, hass):
        driver = _zone_driver(hass)
        result = await driver.deliver(0.0)
        assert result.liters_delivered == 0.0
        assert result.quality is DeliveryQuality.MEASURED
        hass.services.async_call.assert_not_called()

    async def test_without_a_flow_rate_it_refuses_rather_than_guessing(self, hass):
        """An unknown duration must not become an open valve with no stop condition."""
        driver = _zone_driver(hass, flow_rate=0.0)
        result = await driver.deliver(10.0)
        assert result.liters_delivered == 0.0
        assert result.quality is DeliveryQuality.LOW_CONFIDENCE
        assert result.detail == "no_flow_rate"
        hass.services.async_call.assert_not_called()

    async def test_a_full_run_reports_an_estimated_figure(self, hass):
        """Time-based delivery is an estimate, and says so."""
        driver = _zone_driver(hass, flow_rate=6000.0)  # 0.01 L/s -> 1 L in 0.01 s

        async def simulate():
            await _confirm(driver, "on")
            await asyncio.sleep(0.05)
            driver._on_switch_state(_state_event("off"))

        sim = asyncio.create_task(simulate())
        result = await driver.deliver(1.0)
        await sim

        assert result.quality is DeliveryQuality.ESTIMATED
        assert result.liters_delivered == pytest.approx(1.0, rel=0.2)
        assert result.requested_liters == 1.0

    async def test_an_aborted_run_reports_partial_not_estimated(self, hass):
        """Stopping early must be visible in the quality, not hidden in the number."""
        driver = _zone_driver(hass, flow_rate=6.0)  # 1 L would take 10 s

        aborted = {"now": False}

        async def simulate():
            await _confirm(driver, "on")
            await asyncio.sleep(0.05)
            aborted["now"] = True
            await _yield_loop()
            driver._on_switch_state(_state_event("off"))

        sim = asyncio.create_task(simulate())
        result = await driver.deliver(1.0, should_abort=lambda: aborted["now"])
        await sim

        assert result.quality is DeliveryQuality.PARTIAL
        assert result.liters_delivered < 1.0

    async def test_a_failed_open_delivers_nothing_and_says_why(self, hass):
        driver = _zone_driver(hass, flow_rate=60.0)
        result = await driver.deliver(1.0)
        assert result.liters_delivered == 0.0
        assert result.quality is DeliveryQuality.LOW_CONFIDENCE


class TestMasterDriver:
    """The pump follows aggregate activity — and must not cycle between zones."""

    def _master(self, hass, *, off_delay=0.05) -> MasterDriver:
        return MasterDriver(
            hass,
            "switch.pump",
            off_delay_s=off_delay,
            fsm_config=_fast_fsm(),
            max_retries=0,
            backoff_s=(0.01,),
            name="pump",
        )

    async def test_starts_when_a_zone_becomes_active(self, hass):
        master = self._master(hass)

        async def simulate():
            await _confirm(master, "on")

        sim = asyncio.create_task(simulate())
        await master.follow(any_zone_active=True)
        await sim

        assert master.is_open
        hass.services.async_call.assert_any_call("switch", "turn_on", {"entity_id": "switch.pump"}, blocking=False)

    async def test_stays_on_while_zones_remain_active(self, hass):
        master = self._master(hass)

        async def simulate():
            await _confirm(master, "on")

        sim = asyncio.create_task(simulate())
        await master.follow(any_zone_active=True)
        await sim
        hass.services.async_call.reset_mock()

        await master.follow(any_zone_active=True)
        hass.services.async_call.assert_not_called()

    async def test_closes_after_the_off_delay_once_nothing_is_active(self, hass):
        master = self._master(hass, off_delay=0.02)

        async def simulate_on():
            await _confirm(master, "on")

        sim = asyncio.create_task(simulate_on())
        await master.follow(any_zone_active=True)
        await sim

        await master.follow(any_zone_active=False)
        await asyncio.sleep(0.05)
        master._on_switch_state(_state_event("off"))
        await _yield_loop()

        hass.services.async_call.assert_any_call("switch", "turn_off", {"entity_id": "switch.pump"}, blocking=False)

    async def test_a_new_zone_within_the_delay_cancels_the_close(self, hass):
        """The whole point of the linger: sequential zones must not cycle the pump.

        The field report on GH #95 put it at five seconds — long enough to bridge
        the gap between one zone finishing and the next starting.
        """
        master = self._master(hass, off_delay=0.2)

        async def simulate_on():
            await _confirm(master, "on")

        sim = asyncio.create_task(simulate_on())
        await master.follow(any_zone_active=True)
        await sim
        hass.services.async_call.reset_mock()

        await master.follow(any_zone_active=False)
        await asyncio.sleep(0.02)
        await master.follow(any_zone_active=True)
        await asyncio.sleep(0.3)

        for call in hass.services.async_call.call_args_list:
            assert call.args[1] != "turn_off", "the pump was cycled between two zones"

    async def test_unload_cancels_a_pending_close(self, hass):
        master = self._master(hass, off_delay=0.2)
        await master.follow(any_zone_active=False)
        master.async_unload()
        await asyncio.sleep(0.3)
        for call in hass.services.async_call.call_args_list:
            assert call.args[1] != "turn_off"

    async def test_the_master_knows_nothing_about_liters(self, hass):
        """It is ON/OFF only — no delivery contract, by design."""
        assert not hasattr(self._master(hass), "deliver")


class TestFlowMeterDelivery:
    """Measured delivery: the figure comes from the meter, not from the clock."""

    def _metered(self, hass, **kwargs) -> ZoneDriver:
        return ZoneDriver(
            hass,
            "switch.valve",
            delivery_mode=DeliveryMode.FLOW_METER,
            flow_rate_lpm=60.0,
            flow_meter_sensor=kwargs.pop("meter", "sensor.flow"),
            fsm_config=_fast_fsm(),
            max_retries=0,
            backoff_s=(0.01,),
            name="testzone",
            delivery_timeout_s=1,
            **kwargs,
        )

    async def test_without_a_meter_it_refuses(self, hass):
        driver = self._metered(hass, meter=None)
        result = await driver.deliver(5.0)
        assert result.quality is DeliveryQuality.LOW_CONFIDENCE
        assert result.detail == "no_flow_meter"

    async def test_a_rate_meter_integrates_to_a_measured_figure(self, hass):
        """The payoff over an estimate: what the meter saw, not what the clock implied."""
        driver = self._metered(hass)

        _wire_flowing_valve(hass, rate_lpm=600.0)

        async def simulate():
            await _confirm(driver, "on")
            await asyncio.sleep(0.3)
            driver._on_switch_state(_state_event("off"))

        sim = asyncio.create_task(simulate())
        result = await driver.deliver(1.0)
        await sim

        assert result.liters_delivered > 0
        assert result.quality is DeliveryQuality.MEASURED
        # Guards the harness itself: `fallback_estimate` means the loop never saw
        # an open valve and credited the nominal rate instead of the meter, which
        # is how the first version of this test passed while proving nothing.
        assert result.detail != "fallback_estimate"

    async def test_progress_is_reported_while_water_flows(self, hass):
        """The caller deducts the deficit live, so it needs the running figure."""
        driver = self._metered(hass)
        seen: list[float] = []

        _wire_flowing_valve(hass, rate_lpm=600.0)

        async def simulate():
            await _confirm(driver, "on")
            await asyncio.sleep(0.3)
            driver._on_switch_state(_state_event("off"))

        sim = asyncio.create_task(simulate())
        await driver.deliver(1.0, on_progress=seen.append)
        await sim

        assert seen, "no progress was reported during delivery"
        assert seen == sorted(seen), "progress must not go backwards"

    async def test_a_failed_open_never_reports_water(self, hass):
        driver = self._metered(hass)
        result = await driver.deliver(5.0)
        assert result.liters_delivered == 0.0
        assert result.quality is DeliveryQuality.LOW_CONFIDENCE


class TestLiveness:
    """The active probe — a valve can be unreachable without anyone asking it."""

    async def test_a_responding_entity_is_live(self, hass):
        driver = _zone_driver(hass)
        hass.states.get = MagicMock(return_value=MagicMock(state="off"))
        assert await driver.async_ping() is True

    async def test_an_unavailable_entity_is_not(self, hass):
        driver = _zone_driver(hass)
        hass.states.get = MagicMock(return_value=MagicMock(state="unavailable"))
        assert await driver.async_ping() is False

    async def test_a_missing_entity_is_not(self, hass):
        driver = _zone_driver(hass)
        hass.states.get = MagicMock(return_value=None)
        assert await driver.async_ping() is False

    async def test_a_dedicated_availability_entity_is_preferred(self, hass):
        """A Z2M availability sensor is the cheaper, truer signal when present."""
        driver = _zone_driver(hass, availability_entity="binary_sensor.valve_available")
        asked: list[str] = []

        def states_get(entity_id):
            asked.append(entity_id)
            return MagicMock(state="on")

        hass.states.get = MagicMock(side_effect=states_get)
        assert await driver.async_ping() is True
        assert "binary_sensor.valve_available" in asked

    async def test_an_availability_entity_reading_off_means_unreachable(self, hass):
        driver = _zone_driver(hass, availability_entity="binary_sensor.valve_available")
        hass.states.get = MagicMock(return_value=MagicMock(state="off"))
        assert await driver.async_ping() is False

    async def test_the_probe_can_be_started_and_stopped(self, hass):
        driver = _zone_driver(hass)
        driver.start_liveness_probe(interval_min=0.001)
        await asyncio.sleep(0)
        driver.async_unload()


def _calls_to(hass, domain: str, service: str) -> list:
    """Every service call the driver made to ``domain.service``."""
    return [c for c in hass.services.async_call.await_args_list if tuple(c.args[:2]) == (domain, service)]


class TestHardwareCeiling:
    """The outermost safety layer: the timer written into the device itself.

    It is the only layer that survives Home Assistant dying, which is why it is
    worth covering even though nothing reads its return value. Two properties
    carry it: the value is the caller's, converted but never invented, and the
    write is attempted through whichever channel the installation offers —
    failing quietly, because a valve that opens is better than one refused over
    a timer that could not be written.
    """

    async def test_an_installation_offering_no_channel_writes_nothing(self, hass):
        driver = _zone_driver(hass)
        await driver._set_hw_max_duration()
        assert _calls_to(hass, "number", "set_value") == []
        assert _calls_to(hass, "mqtt", "publish") == []

    async def test_the_entity_channel_writes_the_converted_value(self, hass):
        """The multiplier is a unit conversion, so seconds may reach the device as minutes."""
        driver = _zone_driver(
            hass,
            hw_max_duration_entity="number.valve_max",
            hw_max_duration_s=600.0,
            hw_max_duration_multiplier=1 / 60,
        )
        await driver._set_hw_max_duration()
        calls = _calls_to(hass, "number", "set_value")
        assert len(calls) == 1
        assert calls[0].args[2] == {"entity_id": "number.valve_max", "value": 10.0}

    async def test_it_is_written_once_per_open_cycle(self, hass):
        driver = _zone_driver(hass, hw_max_duration_entity="number.valve_max", hw_max_duration_s=600.0)
        await driver._set_hw_max_duration()
        await driver._set_hw_max_duration()
        assert len(_calls_to(hass, "number", "set_value")) == 1
        # The next cycle starts where the watchdog was cancelled, and writes again.
        driver._cancel_watchdog()
        await driver._set_hw_max_duration()
        assert len(_calls_to(hass, "number", "set_value")) == 2

    async def test_a_failing_entity_falls_back_to_mqtt(self, hass):
        """Both channels configured means the second is a fallback, not a duplicate."""

        async def call(domain, service, *args, **kwargs):
            if domain == "number":
                raise RuntimeError("entity is unavailable")

        hass.services.async_call = AsyncMock(side_effect=call)
        driver = _zone_driver(
            hass,
            hw_max_duration_entity="number.valve_max",
            hw_max_duration_topic="zigbee2mqtt/valve/set",
            hw_max_duration_s=600.0,
        )
        await driver._set_hw_max_duration()
        published = _calls_to(hass, "mqtt", "publish")
        assert len(published) == 1
        assert published[0].args[2]["payload"] == "600.0"

    async def test_the_mqtt_channel_renders_the_payload_template(self, hass):
        driver = _zone_driver(
            hass,
            hw_max_duration_topic="zigbee2mqtt/valve/set",
            hw_max_duration_payload_template='{{"auto_close": {value}}}',
            hw_max_duration_s=600.0,
        )
        await driver._set_hw_max_duration()
        published = _calls_to(hass, "mqtt", "publish")
        assert len(published) == 1
        assert published[0].args[2] == {
            "topic": "zigbee2mqtt/valve/set",
            "payload": '{"auto_close": 600.0}',
        }

    async def test_a_failing_mqtt_channel_never_raises(self, hass):
        """A ceiling that cannot be written must not take the irrigation down with it."""
        hass.services.async_call = AsyncMock(side_effect=RuntimeError("broker is down"))
        driver = _zone_driver(hass, hw_max_duration_topic="zigbee2mqtt/valve/set", hw_max_duration_s=600.0)
        await driver._set_hw_max_duration()  # must not raise

    async def test_without_its_own_value_it_matches_the_watchdog(self, hass):
        """The flat ladder: a zone with nothing to derive a spread from writes the watchdog's value."""
        driver = _zone_driver(hass, hw_max_duration_entity="number.valve_max", max_open_duration_s=900.0)
        await driver._set_hw_max_duration()
        assert _calls_to(hass, "number", "set_value")[0].args[2]["value"] == 900.0

    async def test_a_callable_value_is_read_at_every_cycle(self, hass):
        """The ladder tracks the current deficit, so a snapshot taken at setup would go stale."""
        current = [600.0]
        driver = _zone_driver(
            hass,
            hw_max_duration_entity="number.valve_max",
            hw_max_duration_s=lambda: current[0],
        )
        await driver._set_hw_max_duration()
        current[0] = 1200.0
        driver._cancel_watchdog()
        await driver._set_hw_max_duration()
        written = [c.args[2]["value"] for c in _calls_to(hass, "number", "set_value")]
        assert written == [600.0, 1200.0]


class TestStuckOpenEscalation:
    """A valve that will not close is the one failure that keeps costing water.

    Every other fault stops the irrigation; this one continues it against our
    will, so the response is not a report but an action — stop everything, then
    tell the user in the loudest register available.
    """

    async def test_it_stops_the_integration_and_shouts(self, hass):
        notifier = ValveNotifier(hass)
        driver = _zone_driver(hass, notifier=notifier)
        await driver._escalate_stuck_open()

        assert len(_calls_to(hass, "never_dry", "stop")) == 1
        assert notifier.is_active("testzone", NotificationKind.STUCK_OPEN)
        assert notifier._active[("testzone", NotificationKind.STUCK_OPEN)].severity is Severity.CRITICAL

    async def test_a_failing_emergency_stop_still_reaches_the_user(self, hass):
        """The notification is the last line: it must not depend on the service that just failed."""

        async def call(domain, service, *args, **kwargs):
            if (domain, service) == ("never_dry", "stop"):
                raise RuntimeError("service not registered")

        hass.services.async_call = AsyncMock(side_effect=call)
        notifier = ValveNotifier(hass)
        driver = _zone_driver(hass, notifier=notifier)
        await driver._escalate_stuck_open()  # must not raise

        assert notifier.is_active("testzone", NotificationKind.STUCK_OPEN)

    async def test_without_a_notifier_it_still_stops_the_integration(self, hass):
        driver = _zone_driver(hass)
        await driver._escalate_stuck_open()
        assert len(_calls_to(hass, "never_dry", "stop")) == 1

    async def test_a_leak_that_survives_recovery_escalates_exactly_once(self, hass):
        """The path that reaches it: close reported a leak, and the retry did not clear it."""
        driver = _zone_driver(hass, flow_meter_sensor="sensor.flow")
        leaked = OperationResult(
            status=OperationStatus.FAILED,
            error_detail=FailureKind.CLOSE_LEAK.value,
            retries_used=0,
            duration_ms=1.0,
        )
        driver._run_command = AsyncMock(return_value=leaked)
        driver._attempt_leak_recovery = AsyncMock(return_value=False)

        result = await driver.async_turn_off()

        assert result is leaked
        assert driver._attempt_leak_recovery.await_count == 1
        assert len(_calls_to(hass, "never_dry", "stop")) == 1

    async def test_a_recovered_leak_does_not_escalate(self, hass):
        """Recovery succeeding turns a failure into an OK — nobody is woken up."""
        driver = _zone_driver(hass, flow_meter_sensor="sensor.flow")
        driver._run_command = AsyncMock(
            return_value=OperationResult(
                status=OperationStatus.FAILED,
                error_detail=FailureKind.CLOSE_LEAK.value,
                retries_used=2,
                duration_ms=1.0,
            )
        )
        driver._attempt_leak_recovery = AsyncMock(return_value=True)

        result = await driver.async_turn_off()

        assert result.status is OperationStatus.OK
        assert result.error_detail == "leak_recovered"
        assert result.retries_used == 2
        assert _calls_to(hass, "never_dry", "stop") == []


class TestLeakRecovery:
    """Between a leak and the alarm sits one more attempt, and it must be honest.

    Everything here decides whether the user is woken up. A recovery that
    reports success on a meter it could not read would silence the one failure
    that keeps costing water, so every unreadable answer counts as *not*
    recovered — the direction in which being wrong is survivable.
    """

    async def test_it_re_issues_the_close_before_judging(self, hass):
        """The first command may simply have been lost on the wire."""
        _wire_meter(hass, "0.0", unit="L/min")
        driver = _zone_driver(hass, flow_meter_sensor="sensor.flow")
        await driver._attempt_leak_recovery()
        assert len(_calls_to(hass, "switch", "turn_off")) == 1

    async def test_flow_back_to_zero_is_recovery(self, hass):
        _wire_meter(hass, "0.0", unit="L/min")
        driver = _zone_driver(hass, flow_meter_sensor="sensor.flow")
        assert await driver._attempt_leak_recovery() is True

    async def test_water_still_running_is_not(self, hass):
        """A rate sensor still reading above zero means water is moving."""
        _wire_meter(hass, "4.2", unit="L/min")
        driver = _zone_driver(hass, flow_meter_sensor="sensor.flow")
        assert await driver._attempt_leak_recovery() is False

    async def test_a_counter_that_stopped_climbing_is_recovery(self, hass):
        """The defect this guards: a counter's total is always above any threshold.

        Field case, 2026-08-18: a valve HA had recorded as closed was escalated
        as stuck open because the meter read 646 — its lifetime litre count, not
        a flow. Read as a level it never returns to zero, so recovery could never
        succeed and every close risked an integration-wide emergency stop.
        """
        _wire_meter(hass, "646.0", unit="L")
        driver = _zone_driver(hass, flow_meter_sensor="sensor.flow")
        assert await driver._attempt_leak_recovery() is True

    async def test_a_counter_still_climbing_is_a_real_leak(self, hass):
        """Movement after the second close is the only honest evidence of a leak."""
        meter = _wire_meter(hass, "646.0", unit="L")
        driver = _zone_driver(hass, flow_meter_sensor="sensor.flow")
        # Water keeps running while we re-issue the close: the counter advances.
        driver._call_actuator = AsyncMock(side_effect=lambda **kw: meter.set("652.0"))
        assert await driver._attempt_leak_recovery() is False

    async def test_a_counter_with_no_baseline_is_not_recovery(self, hass):
        """No before-reading is no basis for a verdict, so it is not recovery."""
        meter = _wire_meter(hass, "unavailable", unit="L")
        driver = _zone_driver(hass, flow_meter_sensor="sensor.flow")
        driver._call_actuator = AsyncMock(side_effect=lambda **kw: meter.set("646.0"))
        assert await driver._attempt_leak_recovery() is False

    async def test_a_trickle_under_the_threshold_counts_as_closed(self, hass):
        """The threshold exists because a meter at rest rarely reads exactly zero."""
        _wire_meter(hass, "0.4", unit="L/min")
        driver = _zone_driver(hass, flow_meter_sensor="sensor.flow", flow_zero_threshold=0.5)
        assert await driver._attempt_leak_recovery() is True

    async def test_no_meter_means_no_evidence_of_recovery(self, hass):
        """Without a meter nothing can prove the water stopped, so nothing does."""
        driver = _zone_driver(hass)
        assert await driver._attempt_leak_recovery() is False

    async def test_a_missing_meter_state_is_not_recovery(self, hass):
        driver = _zone_driver(hass, flow_meter_sensor="sensor.flow")
        hass.states.get = MagicMock(return_value=None)
        assert await driver._attempt_leak_recovery() is False

    async def test_an_unreadable_meter_is_not_recovery(self, hass):
        """`unavailable` is not a number, and must not be read as a quiet meter."""
        driver = _zone_driver(hass, flow_meter_sensor="sensor.flow")
        hass.states.get = MagicMock(return_value=MagicMock(state="unavailable"))
        assert await driver._attempt_leak_recovery() is False
