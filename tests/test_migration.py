"""Tests for config entry migration (async_migrate_entry)."""

from unittest.mock import MagicMock

import pytest
from never_dry import async_migrate_entry
from never_dry.const import CONFIG_VERSION


def _make_entry(version: int) -> MagicMock:
    """Create a mock ConfigEntry with the given version."""
    entry = MagicMock()
    entry.version = version
    return entry


class TestConfigMigration:
    """Test async_migrate_entry behavior."""

    @pytest.mark.asyncio
    async def test_current_version_succeeds(self, hass_mock):
        """Entry at current CONFIG_VERSION should migrate successfully."""
        entry = _make_entry(CONFIG_VERSION)
        result = await async_migrate_entry(hass_mock, entry)
        assert result is True

    @pytest.mark.asyncio
    async def test_older_version_succeeds(self, hass_mock):
        """Entry older than CONFIG_VERSION should migrate successfully."""
        entry = _make_entry(1)
        result = await async_migrate_entry(hass_mock, entry)
        assert result is True

    @pytest.mark.asyncio
    async def test_newer_version_fails(self, hass_mock):
        """Entry newer than CONFIG_VERSION should fail (downgrade not supported)."""
        entry = _make_entry(CONFIG_VERSION + 1)
        result = await async_migrate_entry(hass_mock, entry)
        assert result is False

    @pytest.mark.asyncio
    async def test_far_future_version_fails(self, hass_mock):
        """Entry from far future should fail gracefully."""
        entry = _make_entry(999)
        result = await async_migrate_entry(hass_mock, entry)
        assert result is False

    @pytest.mark.asyncio
    async def test_v1_to_v2_adds_delivery_mode(self, hass_mock):
        """V1 entry should get delivery_mode added to all zones."""
        entry = _make_entry(1)
        entry.data = {
            "temperature_sensor": "sensor.temp",
            "rain_sensor": "sensor.rain",
            "zones": [
                {"name": "Orto", "valve": "switch.v1"},
                {"name": "Prato", "valve": "switch.v2"},
            ],
        }
        result = await async_migrate_entry(hass_mock, entry)
        assert result is True
        # Check that zones got delivery_mode added
        updated_data = hass_mock.config_entries.async_update_entry.call_args
        new_data = updated_data.kwargs.get("data", updated_data[1].get("data", {}))
        for zone in new_data["zones"]:
            assert zone["delivery_mode"] == "estimated_flow"

    @pytest.mark.asyncio
    async def test_v1_to_v2_preserves_existing_fields(self, hass_mock):
        """V1 migration should not remove existing zone fields."""
        entry = _make_entry(1)
        entry.data = {
            "temperature_sensor": "sensor.temp",
            "rain_sensor": "sensor.rain",
            "zones": [
                {"name": "Orto", "valve": "switch.v1", "area_m2": 20.0},
            ],
        }
        result = await async_migrate_entry(hass_mock, entry)
        assert result is True
        updated_data = hass_mock.config_entries.async_update_entry.call_args
        new_data = updated_data.kwargs.get("data", updated_data[1].get("data", {}))
        assert new_data["zones"][0]["area_m2"] == 20.0
        assert new_data["zones"][0]["name"] == "Orto"


class TestV2ToV3PresetOverrideContract:
    """The dropdown now decides which of a preset/override pair applies.

    Two pairs used to work the other way round: a stored efficiency or manual
    Kc took charge on its own. Writing "custom" into those dropdowns changes
    nothing about how a zone waters — the same number stays in force — it just
    says so. Skip it and the value would be ignored on the next start: a drip
    zone running at 0.55 would silently jump to 0.92 and water less.
    """

    @pytest.mark.asyncio
    async def test_a_stored_efficiency_becomes_the_custom_system_type(self, hass_mock):
        entry = _make_entry(2)
        entry.data = {"zones": [{"name": "Orto", "system_type": "drip", "efficiency": 0.55}]}

        assert await async_migrate_entry(hass_mock, entry) is True

        zone = _migrated_zones(hass_mock)[0]
        assert zone["system_type"] == "custom"
        assert zone["efficiency"] == 0.55

    @pytest.mark.asyncio
    async def test_a_stored_kc_becomes_the_custom_plant_family(self, hass_mock):
        entry = _make_entry(2)
        entry.data = {"zones": [{"name": "Orto", "plant_family": "lawn", "kc": 1.1}]}

        assert await async_migrate_entry(hass_mock, entry) is True

        zone = _migrated_zones(hass_mock)[0]
        assert zone["plant_family"] == "custom"
        assert zone["kc"] == 1.1

    @pytest.mark.asyncio
    async def test_zones_without_overrides_are_untouched(self, hass_mock):
        entry = _make_entry(2)
        entry.data = {"zones": [{"name": "Orto", "system_type": "drip", "plant_family": "lawn"}]}

        assert await async_migrate_entry(hass_mock, entry) is True

        zone = _migrated_zones(hass_mock)[0]
        assert zone["system_type"] == "drip"
        assert zone["plant_family"] == "lawn"

    @pytest.mark.asyncio
    async def test_an_exposure_preset_with_a_leftover_factor_is_left_alone(self, hass_mock):
        """The one case that must NOT be migrated.

        Exposure always let the dropdown decide, so a factor behind a preset
        is already ignored today. Marking it custom would switch it on and
        change the watering — the exact harm this migration exists to avoid.
        The config flow warns about it on the next save instead.
        """
        entry = _make_entry(2)
        entry.data = {"zones": [{"name": "Orto", "exposure": "morning_sun", "microclimate_factor": 0.9}]}

        assert await async_migrate_entry(hass_mock, entry) is True

        zone = _migrated_zones(hass_mock)[0]
        assert zone["exposure"] == "morning_sun"
        assert zone["microclimate_factor"] == 0.9

    @pytest.mark.asyncio
    async def test_an_upgrade_from_v1_passes_through_both_steps(self, hass_mock):
        """Migrations are a chain: skipping a release must not skip a step.

        The mock has to advance ``entry.version`` the way HA's real
        ``async_update_entry`` does, or the second step never runs and this
        test passes while proving nothing.
        """
        entry = _make_entry(1)
        entry.data = {"zones": [{"name": "Orto", "efficiency": 0.55}]}

        def _apply(target, **kwargs):
            if "data" in kwargs:
                target.data = kwargs["data"]
            if "version" in kwargs:
                target.version = kwargs["version"]

        hass_mock.config_entries.async_update_entry.side_effect = _apply

        assert await async_migrate_entry(hass_mock, entry) is True

        zone = _migrated_zones(hass_mock)[0]
        assert zone["delivery_mode"] == "estimated_flow"  # v1 -> v2
        assert zone["system_type"] == "custom"  # v2 -> v3


def _migrated_zones(hass_mock):
    """Zones as written by the last async_update_entry call."""
    call = hass_mock.config_entries.async_update_entry.call_args
    return call.kwargs.get("data", {})["zones"]
