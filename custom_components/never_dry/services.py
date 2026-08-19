"""Domain-level service registration with multi-entry dispatch (GH #105).

Every config entry builds its own :class:`IrrigationController`, but Home
Assistant services are registered per *domain*: with two entries, whichever
controller registered last silently captured every ``never_dry.*`` call, so
zones belonging to the other controller were "not found" and their valves
never switched.

This module registers each service exactly once and dispatches calls to the
right controller:

- **Zone-scoped services** (``irrigate_zone``, ``stop_zone``,
  ``mark_irrigated``, ``reset_valve``, ``set_deficit``) look the zone up
  across *all* controllers and route the call to the one that owns it.
  Called without a zone name, the services that support "all zones"
  semantics fan out to every controller.
- **Global services** (``stop``, ``irrigate_all``, ``reset``) fan out to
  every controller — an emergency stop must close every valve of every
  configured system.
"""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant, ServiceCall

from .const import (
    ATTR_ZONE_NAME,
    DOMAIN,
    SERVICE_IRRIGATE_ALL,
    SERVICE_IRRIGATE_ZONE,
    SERVICE_MARK_IRRIGATED,
    SERVICE_RESET,
    SERVICE_RESET_VALVE,
    SERVICE_RESET_YEARLY_RAIN,
    SERVICE_RESET_YEARLY_WATER,
    SERVICE_SET_DEFICIT,
    SERVICE_STOP,
    SERVICE_STOP_ZONE,
    SERVICE_TEST_VALVE,
)

_LOGGER = logging.getLogger(__name__)

_SERVICES_REGISTERED = "_services_registered"
_CONTROLLER_KEY_PREFIX = "_controller_"

# service name → controller handler method name
_ZONE_SCOPED: dict[str, str] = {
    SERVICE_IRRIGATE_ZONE: "_handle_irrigate_zone",
    SERVICE_STOP_ZONE: "_handle_stop_zone",
    SERVICE_MARK_IRRIGATED: "_handle_mark_irrigated",
    SERVICE_RESET_VALVE: "_handle_reset_valve",
    SERVICE_TEST_VALVE: "_handle_test_valve",
    SERVICE_SET_DEFICIT: "_handle_set_deficit",
}
_GLOBAL: dict[str, str] = {
    SERVICE_STOP: "_handle_stop",
    SERVICE_IRRIGATE_ALL: "_handle_irrigate_all",
    SERVICE_RESET: "_handle_reset",
    SERVICE_RESET_YEARLY_RAIN: "_handle_reset_yearly_rain",
    SERVICE_RESET_YEARLY_WATER: "_handle_reset_yearly_water",
}


def _controllers(hass: HomeAssistant) -> list:
    """Return every live IrrigationController across all config entries."""
    domain_data = hass.data.get(DOMAIN, {})
    return [v for k, v in domain_data.items() if isinstance(k, str) and k.startswith(_CONTROLLER_KEY_PREFIX)]


def _all_zone_names(controllers: list) -> list[str]:
    return [name for c in controllers for name in c.zone_names]


async def _dispatch_zone_scoped(hass: HomeAssistant, handler_name: str, call: ServiceCall) -> None:
    """Route a zone-scoped call to the controller that owns the zone.

    Without a zone name the call fans out to every controller — the
    handlers that support "all zones" semantics (mark_irrigated,
    set_deficit) apply it per controller, the others log their own error.
    """
    controllers = _controllers(hass)
    if not controllers:
        _LOGGER.error("%s: no NeverDry controller is loaded", call.service)
        return

    zone_name = call.data.get(ATTR_ZONE_NAME)
    if not zone_name:
        for controller in controllers:
            await getattr(controller, handler_name)(call)
        return

    matches = [c for c in controllers if c.has_zone(zone_name)]
    if not matches:
        _LOGGER.error(
            "%s: zone '%s' not found in any controller. Available: %s",
            call.service,
            zone_name,
            _all_zone_names(controllers),
        )
        return
    if len(matches) > 1:
        _LOGGER.warning(
            "%s: zone '%s' exists in %d controllers — using the first; rename the zones to disambiguate",
            call.service,
            zone_name,
            len(matches),
        )
    await getattr(matches[0], handler_name)(call)


async def _dispatch_global(hass: HomeAssistant, handler_name: str, call: ServiceCall) -> None:
    """Fan a global call out to every controller."""
    controllers = _controllers(hass)
    if not controllers:
        _LOGGER.error("%s: no NeverDry controller is loaded", call.service)
        return
    for controller in controllers:
        await getattr(controller, handler_name)(call)


def async_setup_services(hass: HomeAssistant) -> None:
    """Register the domain services once, idempotently."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    if domain_data.get(_SERVICES_REGISTERED):
        return
    domain_data[_SERVICES_REGISTERED] = True

    def _zone_handler(handler_name: str):
        async def handler(call: ServiceCall) -> None:
            await _dispatch_zone_scoped(hass, handler_name, call)

        return handler

    def _global_handler(handler_name: str):
        async def handler(call: ServiceCall) -> None:
            await _dispatch_global(hass, handler_name, call)

        return handler

    for service, handler_name in _ZONE_SCOPED.items():
        hass.services.async_register(DOMAIN, service, _zone_handler(handler_name))
    for service, handler_name in _GLOBAL.items():
        hass.services.async_register(DOMAIN, service, _global_handler(handler_name))


def async_unload_services(hass: HomeAssistant) -> None:
    """Remove the domain services when the last controller is gone."""
    domain_data = hass.data.get(DOMAIN, {})
    if not domain_data.get(_SERVICES_REGISTERED):
        return
    if _controllers(hass):
        return  # another entry still needs them
    for service in (*_ZONE_SCOPED, *_GLOBAL):
        hass.services.async_remove(DOMAIN, service)
    domain_data.pop(_SERVICES_REGISTERED, None)
