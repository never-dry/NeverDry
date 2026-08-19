"""Multi-entry service dispatch (GH #105).

With two NeverDry config entries, domain services used to be captured by
whichever controller registered last: zones of the other controller were
"not found" and their valves never switched. The services module now
registers each service once and routes calls to the controller that owns
the target zone; global services fan out to every controller.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from never_dry import services as svc
from never_dry.const import (
    ATTR_ZONE_NAME,
    CONF_ZONE_AREA,
    CONF_ZONE_FLOW_RATE,
    CONF_ZONE_NAME,
    CONF_ZONE_VALVE,
    DOMAIN,
    SERVICE_IRRIGATE_ZONE,
    SERVICE_RESET_YEARLY_RAIN,
    SERVICE_RESET_YEARLY_WATER,
)
from never_dry.controller import IrrigationController
from never_dry.sensor import IrrigationZoneSensor


def _make_zone(hass_mock, di_sensor, name, valve):
    return IrrigationZoneSensor(
        hass_mock,
        {
            CONF_ZONE_NAME: name,
            CONF_ZONE_VALVE: valve,
            CONF_ZONE_AREA: 20.0,
            CONF_ZONE_FLOW_RATE: 8.0,
        },
        di_sensor,
    )


def _call(service="irrigate_zone", **data):
    call = MagicMock()
    call.service = service
    call.data = data
    return call


@pytest.fixture
def two_controllers(hass_mock, di_sensor):
    """Two controllers with one zone each, wired into hass.data like two entries."""
    zone_a = _make_zone(hass_mock, di_sensor, "Hochbeet", "switch.hochbeet")
    zone_b = _make_zone(hass_mock, di_sensor, "Beet Terrasse", "switch.terrasse")
    ctrl_1 = IrrigationController(hass_mock, di_sensor, [zone_a], inter_zone_delay=0)
    ctrl_2 = IrrigationController(hass_mock, di_sensor, [zone_b], inter_zone_delay=0)
    hass_mock.data = {DOMAIN: {"_controller_entry1": ctrl_1, "_controller_entry2": ctrl_2}}
    return ctrl_1, ctrl_2


class TestZoneLookup:
    def test_has_zone_and_zone_names(self, two_controllers):
        ctrl_1, ctrl_2 = two_controllers
        assert ctrl_1.has_zone("Hochbeet") and not ctrl_1.has_zone("Beet Terrasse")
        assert ctrl_2.has_zone("Beet Terrasse") and not ctrl_2.has_zone("Hochbeet")
        assert ctrl_1.zone_names == ["Hochbeet"]

    def test_controllers_discovered_from_hass_data(self, hass_mock, two_controllers):
        assert set(svc._controllers(hass_mock)) == set(two_controllers)


class TestZoneScopedDispatch:
    """Zone-scoped calls reach the controller that owns the zone — GH #105 repro."""

    @pytest.mark.asyncio
    async def test_routes_to_second_controller(self, hass_mock, two_controllers):
        ctrl_1, ctrl_2 = two_controllers
        ctrl_1._handle_irrigate_zone = AsyncMock()
        ctrl_2._handle_irrigate_zone = AsyncMock()

        call = _call(**{ATTR_ZONE_NAME: "Beet Terrasse"})
        await svc._dispatch_zone_scoped(hass_mock, "_handle_irrigate_zone", call)

        ctrl_2._handle_irrigate_zone.assert_awaited_once_with(call)
        ctrl_1._handle_irrigate_zone.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_routes_to_first_controller(self, hass_mock, two_controllers):
        ctrl_1, ctrl_2 = two_controllers
        ctrl_1._handle_stop_zone = AsyncMock()
        ctrl_2._handle_stop_zone = AsyncMock()

        await svc._dispatch_zone_scoped(
            hass_mock, "_handle_stop_zone", _call("stop_zone", **{ATTR_ZONE_NAME: "Hochbeet"})
        )

        ctrl_1._handle_stop_zone.assert_awaited_once()
        ctrl_2._handle_stop_zone.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unknown_zone_logs_all_available(self, hass_mock, two_controllers, caplog):
        import logging

        ctrl_1, ctrl_2 = two_controllers
        ctrl_1._handle_irrigate_zone = AsyncMock()
        ctrl_2._handle_irrigate_zone = AsyncMock()

        with caplog.at_level(logging.ERROR, logger="custom_components.never_dry"):
            await svc._dispatch_zone_scoped(hass_mock, "_handle_irrigate_zone", _call(**{ATTR_ZONE_NAME: "Nope"}))

        ctrl_1._handle_irrigate_zone.assert_not_awaited()
        ctrl_2._handle_irrigate_zone.assert_not_awaited()
        assert any("Hochbeet" in r.getMessage() and "Beet Terrasse" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_no_zone_name_fans_out(self, hass_mock, two_controllers):
        """mark_irrigated / set_deficit without a zone apply to every controller."""
        ctrl_1, ctrl_2 = two_controllers
        ctrl_1._handle_mark_irrigated = AsyncMock()
        ctrl_2._handle_mark_irrigated = AsyncMock()

        await svc._dispatch_zone_scoped(hass_mock, "_handle_mark_irrigated", _call("mark_irrigated"))

        ctrl_1._handle_mark_irrigated.assert_awaited_once()
        ctrl_2._handle_mark_irrigated.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_duplicate_zone_name_uses_first_and_warns(self, hass_mock, di_sensor, caplog):
        import logging

        zone_1 = _make_zone(hass_mock, di_sensor, "Orto", "switch.a")
        zone_2 = _make_zone(hass_mock, di_sensor, "Orto", "switch.b")
        ctrl_1 = IrrigationController(hass_mock, di_sensor, [zone_1], inter_zone_delay=0)
        ctrl_2 = IrrigationController(hass_mock, di_sensor, [zone_2], inter_zone_delay=0)
        ctrl_1._handle_irrigate_zone = AsyncMock()
        ctrl_2._handle_irrigate_zone = AsyncMock()
        hass_mock.data = {DOMAIN: {"_controller_e1": ctrl_1, "_controller_e2": ctrl_2}}

        with caplog.at_level(logging.WARNING, logger="custom_components.never_dry"):
            await svc._dispatch_zone_scoped(hass_mock, "_handle_irrigate_zone", _call(**{ATTR_ZONE_NAME: "Orto"}))

        ctrl_1._handle_irrigate_zone.assert_awaited_once()
        ctrl_2._handle_irrigate_zone.assert_not_awaited()
        assert any("2 controllers" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_no_controllers_logs_error(self, hass_mock, caplog):
        import logging

        hass_mock.data = {DOMAIN: {}}
        with caplog.at_level(logging.ERROR, logger="custom_components.never_dry"):
            await svc._dispatch_zone_scoped(hass_mock, "_handle_irrigate_zone", _call(**{ATTR_ZONE_NAME: "X"}))
        assert any("no NeverDry controller" in r.getMessage() for r in caplog.records)


class TestGlobalDispatch:
    @pytest.mark.asyncio
    async def test_stop_fans_out_to_all(self, hass_mock, two_controllers):
        """Emergency stop must close every valve of every entry."""
        ctrl_1, ctrl_2 = two_controllers
        ctrl_1._handle_stop = AsyncMock()
        ctrl_2._handle_stop = AsyncMock()

        await svc._dispatch_global(hass_mock, "_handle_stop", _call("stop"))

        ctrl_1._handle_stop.assert_awaited_once()
        ctrl_2._handle_stop.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_irrigate_all_fans_out(self, hass_mock, two_controllers):
        ctrl_1, ctrl_2 = two_controllers
        ctrl_1._handle_irrigate_all = AsyncMock()
        ctrl_2._handle_irrigate_all = AsyncMock()

        await svc._dispatch_global(hass_mock, "_handle_irrigate_all", _call("irrigate_all"))

        ctrl_1._handle_irrigate_all.assert_awaited_once()
        ctrl_2._handle_irrigate_all.assert_awaited_once()


class TestRegistrationLifecycle:
    def test_setup_is_idempotent(self, hass_mock):
        """A second entry must NOT re-register (and thus re-bind) the services."""
        hass_mock.data = {DOMAIN: {}}
        hass_mock.services.async_register = MagicMock()

        svc.async_setup_services(hass_mock)
        first_count = hass_mock.services.async_register.call_count
        svc.async_setup_services(hass_mock)

        assert first_count == 11
        assert hass_mock.services.async_register.call_count == first_count

    def test_registers_all_services(self, hass_mock):
        hass_mock.data = {DOMAIN: {}}
        hass_mock.services.async_register = MagicMock()

        svc.async_setup_services(hass_mock)

        registered = {c.args[1] for c in hass_mock.services.async_register.call_args_list}
        assert SERVICE_IRRIGATE_ZONE in registered
        assert SERVICE_RESET_YEARLY_RAIN in registered
        assert SERVICE_RESET_YEARLY_WATER in registered
        assert len(registered) == 11

    def test_unload_keeps_services_while_controllers_remain(self, hass_mock, two_controllers):
        hass_mock.data[DOMAIN][svc._SERVICES_REGISTERED] = True
        hass_mock.services.async_remove = MagicMock()

        svc.async_unload_services(hass_mock)

        hass_mock.services.async_remove.assert_not_called()

    def test_unload_removes_services_when_last_controller_gone(self, hass_mock):
        hass_mock.data = {DOMAIN: {svc._SERVICES_REGISTERED: True}}
        hass_mock.services.async_remove = MagicMock()

        svc.async_unload_services(hass_mock)

        assert hass_mock.services.async_remove.call_count == 11
        assert svc._SERVICES_REGISTERED not in hass_mock.data[DOMAIN]

    @pytest.mark.asyncio
    async def test_registered_handler_dispatches(self, hass_mock, two_controllers):
        """End-to-end: the handler registered for irrigate_zone routes by zone."""
        _ctrl_1, ctrl_2 = two_controllers
        ctrl_2._handle_irrigate_zone = AsyncMock()
        registered = {}
        hass_mock.services.async_register = MagicMock(
            side_effect=lambda domain, name, handler: registered.__setitem__(name, handler)
        )
        hass_mock.data[DOMAIN].pop(svc._SERVICES_REGISTERED, None)

        svc.async_setup_services(hass_mock)
        call = _call(**{ATTR_ZONE_NAME: "Beet Terrasse"})
        await registered[SERVICE_IRRIGATE_ZONE](call)

        ctrl_2._handle_irrigate_zone.assert_awaited_once_with(call)
