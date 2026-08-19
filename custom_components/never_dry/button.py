"""Button platform for the NeverDry integration.

Provides per-zone buttons: "Irrigate", "Mark as irrigated", "Stop", and
"Reset valve".
"""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType

from .const import (
    ATTR_ZONE_NAME,
    CONF_ZONE_NAME,
    CONF_ZONES,
    DOMAIN,
    SERVICE_IRRIGATE_ZONE,
    SERVICE_MARK_IRRIGATED,
    SERVICE_RESET_VALVE,
    SERVICE_RESET_YEARLY_RAIN,
    SERVICE_RESET_YEARLY_WATER,
    SERVICE_STOP_ZONE,
    SERVICE_TEST_VALVE,
)


def _zone_device_info(entry_id: str, zone_name: str) -> DeviceInfo:
    """Device info matching the zone device created in sensor.py."""
    slug = zone_name.lower().replace(" ", "_")
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry_id}_{slug}")},
    )


def _hub_device_info(entry_id: str) -> DeviceInfo:
    """Device info matching the NeverDry hub device created in sensor.py."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry_id)},
    )


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities,
    discovery_info=None,
) -> None:
    """Set up the NeverDry buttons from YAML configuration."""
    async_add_entities(_create_buttons(hass, config), True)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the NeverDry buttons from a config entry (UI)."""
    async_add_entities(_create_buttons(hass, dict(entry.data), entry.entry_id), True)


def _create_buttons(hass: HomeAssistant, config: dict, entry_id: str = "yaml") -> list[ButtonEntity]:
    """Create button entities for each configured zone plus the system hub."""
    buttons: list[ButtonEntity] = []
    for zone_conf in config.get(CONF_ZONES, []):
        zone_name = zone_conf[CONF_ZONE_NAME]
        device_info = _zone_device_info(entry_id, zone_name)
        buttons.append(MarkIrrigatedButton(hass, zone_name, device_info))
        buttons.append(IrrigateButton(hass, zone_name, device_info))
        if zone_conf.get("valve"):
            buttons.append(StopButton(hass, zone_name, device_info))
            buttons.append(ResetMaintenanceButton(hass, zone_name, device_info))
            buttons.append(ValveTestButton(hass, zone_name, device_info))
    # System-wide reset buttons live on the NeverDry hub device, not on any
    # single zone: yearly rain is one value for the whole garden, and the
    # water reset fans out across every zone (AI-206).
    hub_device = _hub_device_info(entry_id)
    buttons.append(ResetYearlyRainButton(hass, hub_device))
    buttons.append(ResetYearlyWaterButton(hass, hub_device))
    # Scope every unique_id to the config entry (GH #116) — same rule as
    # sensor._create_entities, covered by the same registry migration.
    for button in buttons:
        button._attr_unique_id = f"{entry_id}_{button._attr_unique_id}"
    return buttons


class MarkIrrigatedButton(ButtonEntity):
    """Button to mark a zone as manually irrigated."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:water-check"

    def __init__(self, hass: HomeAssistant, zone_name: str, device_info: DeviceInfo | None = None) -> None:
        self._hass = hass
        self._zone_name = zone_name
        slug = zone_name.lower().replace(" ", "_")
        self._attr_name = "Mark irrigated"
        self._attr_unique_id = f"mark_irrigated_{slug}"
        if device_info:
            self._attr_device_info = device_info

    async def async_press(self) -> None:
        """Handle the button press — reset zone deficit."""
        await self._hass.services.async_call(
            DOMAIN,
            SERVICE_MARK_IRRIGATED,
            {ATTR_ZONE_NAME: self._zone_name},
        )


class IrrigateButton(ButtonEntity):
    """Button to trigger irrigation for a zone based on current deficit."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:sprinkler"

    def __init__(self, hass: HomeAssistant, zone_name: str, device_info: DeviceInfo | None = None) -> None:
        self._hass = hass
        self._zone_name = zone_name
        slug = zone_name.lower().replace(" ", "_")
        self._attr_name = "Irrigate"
        self._attr_unique_id = f"irrigate_{slug}"
        if device_info:
            self._attr_device_info = device_info

    async def async_press(self) -> None:
        """Handle the button press — start irrigation for this zone."""
        await self._hass.services.async_call(
            DOMAIN,
            SERVICE_IRRIGATE_ZONE,
            {ATTR_ZONE_NAME: self._zone_name},
        )


class ValveTestButton(ButtonEntity):
    """Run the supervised one-minute test on this zone.

    Diagnostic category on purpose: it is not part of watering, and it puts water
    on the ground — it belongs where a user goes deliberately, not next to the
    buttons they press every day.
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:stopwatch-start"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hass: HomeAssistant, zone_name: str, device_info: DeviceInfo | None = None) -> None:
        self._hass = hass
        self._zone_name = zone_name
        slug = zone_name.lower().replace(" ", "_")
        self._attr_name = "Valve test (1 min)"
        self._attr_unique_id = f"valve_test_{slug}"
        if device_info:
            self._attr_device_info = device_info

    async def async_press(self) -> None:
        await self._hass.services.async_call(
            DOMAIN,
            SERVICE_TEST_VALVE,
            {ATTR_ZONE_NAME: self._zone_name},
        )


class StopButton(ButtonEntity):
    """Button to stop irrigation for a zone and close its valve."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:stop"

    def __init__(self, hass: HomeAssistant, zone_name: str, device_info: DeviceInfo | None = None) -> None:
        self._hass = hass
        self._zone_name = zone_name
        slug = zone_name.lower().replace(" ", "_")
        self._attr_name = "Stop"
        self._attr_unique_id = f"stop_{slug}"
        if device_info:
            self._attr_device_info = device_info

    async def async_press(self) -> None:
        """Handle the button press — stop this zone and close its valve."""
        await self._hass.services.async_call(
            DOMAIN,
            SERVICE_STOP_ZONE,
            {ATTR_ZONE_NAME: self._zone_name},
        )


class ResetMaintenanceButton(ButtonEntity):
    """Button to reset a valve FSM from MAINTENANCE back to IDLE."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:lock-reset"

    def __init__(self, hass: HomeAssistant, zone_name: str, device_info: DeviceInfo | None = None) -> None:
        self._hass = hass
        self._zone_name = zone_name
        slug = zone_name.lower().replace(" ", "_")
        self._attr_name = "Reset valve"
        self._attr_unique_id = f"reset_valve_{slug}"
        if device_info:
            self._attr_device_info = device_info

    async def async_press(self) -> None:
        """Handle the button press — clear valve MAINTENANCE lock."""
        await self._hass.services.async_call(
            DOMAIN,
            SERVICE_RESET_VALVE,
            {ATTR_ZONE_NAME: self._zone_name},
        )


class ResetYearlyRainButton(ButtonEntity):
    """Hub button to clear the year-to-date rain total (system-wide)."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:weather-rainy"

    def __init__(self, hass: HomeAssistant, device_info: DeviceInfo | None = None) -> None:
        self._hass = hass
        self._attr_name = "Reset yearly rain"
        self._attr_unique_id = "reset_yearly_rain"
        if device_info:
            self._attr_device_info = device_info

    async def async_press(self) -> None:
        """Handle the button press — reset the system-wide yearly rain total."""
        await self._hass.services.async_call(DOMAIN, SERVICE_RESET_YEARLY_RAIN, {})


class ResetYearlyWaterButton(ButtonEntity):
    """Hub button to clear every zone's year-to-date irrigated-water total."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:water-off"

    def __init__(self, hass: HomeAssistant, device_info: DeviceInfo | None = None) -> None:
        self._hass = hass
        self._attr_name = "Reset yearly water"
        self._attr_unique_id = "reset_yearly_water"
        if device_info:
            self._attr_device_info = device_info

    async def async_press(self) -> None:
        """Handle the button press — reset yearly water delivered for all zones."""
        await self._hass.services.async_call(DOMAIN, SERVICE_RESET_YEARLY_WATER, {})
