"""Tests for MarkIrrigatedButton and IrrigateButton entities."""

from unittest.mock import AsyncMock

import pytest
from never_dry.button import (
    IrrigateButton,
    MarkIrrigatedButton,
    ResetMaintenanceButton,
    ResetYearlyRainButton,
    ResetYearlyWaterButton,
    StopButton,
    ValveTestButton,
    _create_buttons,
)
from never_dry.const import (
    ATTR_ZONE_NAME,
    CONF_ZONE_NAME,
    CONF_ZONES,
    DOMAIN,
    SERVICE_IRRIGATE_ZONE,
    SERVICE_MARK_IRRIGATED,
    SERVICE_RESET_YEARLY_RAIN,
    SERVICE_RESET_YEARLY_WATER,
    SERVICE_STOP_ZONE,
)

# Every config grows two system-wide hub buttons (reset yearly rain + water).
HUB_BUTTONS = 2


class TestButtonCreation:
    """Test button entity creation."""

    def test_creates_two_buttons_per_zone(self, hass_mock):
        config = {
            CONF_ZONES: [
                {CONF_ZONE_NAME: "Orto"},
                {CONF_ZONE_NAME: "Prato"},
            ]
        }
        buttons = _create_buttons(hass_mock, config)
        assert len(buttons) == 4 + HUB_BUTTONS  # MarkIrrigated + Irrigate per zone + hub

    def test_button_types(self, hass_mock):
        config = {CONF_ZONES: [{CONF_ZONE_NAME: "Orto"}]}
        buttons = _create_buttons(hass_mock, config)
        assert isinstance(buttons[0], MarkIrrigatedButton)
        assert isinstance(buttons[1], IrrigateButton)

    def test_only_hub_buttons_without_zones(self, hass_mock):
        buttons = _create_buttons(hass_mock, {})
        assert len(buttons) == HUB_BUTTONS

    def test_only_hub_buttons_empty_zones(self, hass_mock):
        buttons = _create_buttons(hass_mock, {CONF_ZONES: []})
        assert len(buttons) == HUB_BUTTONS

    def test_valve_zone_gets_stop_and_reset_buttons(self, hass_mock):
        config = {CONF_ZONES: [{CONF_ZONE_NAME: "Orto", "valve": "switch.valve_orto"}]}
        buttons = _create_buttons(hass_mock, config)
        # Mark + Irrigate + Stop + Reset + Valve test, plus hub
        assert len(buttons) == 5 + HUB_BUTTONS
        assert any(isinstance(b, StopButton) for b in buttons)
        assert any(isinstance(b, ResetMaintenanceButton) for b in buttons)
        assert any(isinstance(b, ValveTestButton) for b in buttons)

    def test_hub_reset_buttons_always_created(self, hass_mock):
        config = {CONF_ZONES: [{CONF_ZONE_NAME: "Orto"}]}
        buttons = _create_buttons(hass_mock, config)
        assert any(isinstance(b, ResetYearlyRainButton) for b in buttons)
        assert any(isinstance(b, ResetYearlyWaterButton) for b in buttons)


class TestButtonProperties:
    """Test button entity attributes."""

    def test_name(self, hass_mock):
        btn = MarkIrrigatedButton(hass_mock, "Orto")
        assert btn._attr_name == "Mark irrigated"

    def test_unique_id(self, hass_mock):
        btn = MarkIrrigatedButton(hass_mock, "Orto")
        assert btn._attr_unique_id == "mark_irrigated_orto"

    def test_unique_id_with_spaces(self, hass_mock):
        btn = MarkIrrigatedButton(hass_mock, "Vegetable Garden")
        assert btn._attr_unique_id == "mark_irrigated_vegetable_garden"

    def test_icon(self, hass_mock):
        btn = MarkIrrigatedButton(hass_mock, "Orto")
        assert btn._attr_icon == "mdi:water-check"


class TestButtonPress:
    """Test button press behavior."""

    @pytest.mark.asyncio
    async def test_press_calls_mark_irrigated_service(self, hass_mock):
        hass_mock.services.async_call = AsyncMock()
        btn = MarkIrrigatedButton(hass_mock, "Orto")

        await btn.async_press()

        hass_mock.services.async_call.assert_called_once_with(
            DOMAIN,
            SERVICE_MARK_IRRIGATED,
            {ATTR_ZONE_NAME: "Orto"},
        )

    @pytest.mark.asyncio
    async def test_press_passes_correct_zone_name(self, hass_mock):
        hass_mock.services.async_call = AsyncMock()
        btn = MarkIrrigatedButton(hass_mock, "Vegetable Garden")

        await btn.async_press()

        call_args = hass_mock.services.async_call.call_args
        assert call_args.args[2][ATTR_ZONE_NAME] == "Vegetable Garden"


class TestStopButton:
    """Test the per-zone Stop button."""

    def test_name(self, hass_mock):
        btn = StopButton(hass_mock, "Orto")
        assert btn._attr_name == "Stop"

    def test_unique_id(self, hass_mock):
        btn = StopButton(hass_mock, "Vegetable Garden")
        assert btn._attr_unique_id == "stop_vegetable_garden"

    @pytest.mark.asyncio
    async def test_press_calls_stop_zone_service(self, hass_mock):
        hass_mock.services.async_call = AsyncMock()
        btn = StopButton(hass_mock, "Orto")

        await btn.async_press()

        hass_mock.services.async_call.assert_called_once_with(
            DOMAIN,
            SERVICE_STOP_ZONE,
            {ATTR_ZONE_NAME: "Orto"},
        )


class TestButtonDeviceInfo:
    """Test device_info grouping."""

    def test_zone_buttons_have_zone_device_hub_buttons_have_hub_device(self, hass_mock):
        config = {CONF_ZONES: [{CONF_ZONE_NAME: "Orto"}]}
        buttons = _create_buttons(hass_mock, config, entry_id="test_entry")
        for btn in buttons:
            assert hasattr(btn, "_attr_device_info")
            identifiers = btn._attr_device_info["identifiers"]
            if isinstance(btn, (ResetYearlyRainButton, ResetYearlyWaterButton)):
                assert (DOMAIN, "test_entry") in identifiers  # hub device
            else:
                assert (DOMAIN, "test_entry_orto") in identifiers  # zone device

    def test_buttons_without_entry_id_have_yaml_device(self, hass_mock):
        config = {CONF_ZONES: [{CONF_ZONE_NAME: "Orto"}]}
        buttons = _create_buttons(hass_mock, config)
        for btn in buttons:
            assert hasattr(btn, "_attr_device_info")
            identifiers = btn._attr_device_info["identifiers"]
            if isinstance(btn, (ResetYearlyRainButton, ResetYearlyWaterButton)):
                assert (DOMAIN, "yaml") in identifiers  # hub device
            else:
                assert (DOMAIN, "yaml_orto") in identifiers  # zone device


class TestIrrigateButton:
    """Test irrigate button entity."""

    def test_name(self, hass_mock):
        btn = IrrigateButton(hass_mock, "Orto")
        assert btn._attr_name == "Irrigate"

    def test_unique_id(self, hass_mock):
        btn = IrrigateButton(hass_mock, "Orto")
        assert btn._attr_unique_id == "irrigate_orto"

    def test_icon(self, hass_mock):
        btn = IrrigateButton(hass_mock, "Orto")
        assert btn._attr_icon == "mdi:sprinkler"

    @pytest.mark.asyncio
    async def test_press_calls_irrigate_zone_service(self, hass_mock):
        hass_mock.services.async_call = AsyncMock()
        btn = IrrigateButton(hass_mock, "Orto")

        await btn.async_press()

        hass_mock.services.async_call.assert_called_once_with(
            DOMAIN,
            SERVICE_IRRIGATE_ZONE,
            {ATTR_ZONE_NAME: "Orto"},
        )


class TestResetYearlyRainButton:
    """The hub button that clears the system-wide yearly rain total."""

    def test_unique_id(self, hass_mock):
        assert ResetYearlyRainButton(hass_mock)._attr_unique_id == "reset_yearly_rain"

    def test_name(self, hass_mock):
        assert ResetYearlyRainButton(hass_mock)._attr_name == "Reset yearly rain"

    @pytest.mark.asyncio
    async def test_press_calls_reset_yearly_rain_service(self, hass_mock):
        hass_mock.services.async_call = AsyncMock()
        btn = ResetYearlyRainButton(hass_mock)

        await btn.async_press()

        hass_mock.services.async_call.assert_called_once_with(
            DOMAIN,
            SERVICE_RESET_YEARLY_RAIN,
            {},
        )


class TestResetYearlyWaterButton:
    """The hub button that clears every zone's yearly irrigated-water total."""

    def test_unique_id(self, hass_mock):
        assert ResetYearlyWaterButton(hass_mock)._attr_unique_id == "reset_yearly_water"

    def test_name(self, hass_mock):
        assert ResetYearlyWaterButton(hass_mock)._attr_name == "Reset yearly water"

    @pytest.mark.asyncio
    async def test_press_calls_reset_yearly_water_service(self, hass_mock):
        hass_mock.services.async_call = AsyncMock()
        btn = ResetYearlyWaterButton(hass_mock)

        await btn.async_press()

        hass_mock.services.async_call.assert_called_once_with(
            DOMAIN,
            SERVICE_RESET_YEARLY_WATER,
            {},
        )
