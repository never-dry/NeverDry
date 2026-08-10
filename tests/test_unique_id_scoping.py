"""Entry-scoped unique_ids (GH #116).

Up to 0.11.0-beta.1 the core sensors used static unique_ids and the
per-zone entities were scoped on the zone slug only: a second config
entry collided ("Platform never_dry does not generate unique IDs") and
HA silently dropped its entities — the second controller was born
without sensors. Every unique_id is now prefixed with the entry_id, and
a registry migration renames existing installations in place.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from never_dry.button import _create_buttons
from never_dry.const import (
    CONF_ZONE_AREA,
    CONF_ZONE_EFFICIENCY,
    CONF_ZONE_NAME,
    CONF_ZONE_SYSTEM_TYPE,
    CONF_ZONES,
    SYSTEM_TYPE_CUSTOM,
)
from never_dry.sensor import _create_entities


@pytest.fixture
def two_zone_config(base_config):
    return {
        **base_config,
        CONF_ZONES: [
            {
                CONF_ZONE_NAME: "Orto",
                "valve": "switch.valve_orto",
                CONF_ZONE_AREA: 20.0,
                CONF_ZONE_SYSTEM_TYPE: SYSTEM_TYPE_CUSTOM,
                CONF_ZONE_EFFICIENCY: 0.90,
            },
            {
                CONF_ZONE_NAME: "Prato",
                CONF_ZONE_AREA: 30.0,
                CONF_ZONE_SYSTEM_TYPE: SYSTEM_TYPE_CUSTOM,
                CONF_ZONE_EFFICIENCY: 0.85,
            },
        ],
    }


class TestSensorUniqueIdScoping:
    def test_all_unique_ids_carry_the_entry_prefix(self, hass_mock, two_zone_config):
        entities, _, _ = _create_entities(hass_mock, two_zone_config, "entryA")
        assert entities, "factory produced no entities"
        for e in entities:
            assert e._attr_unique_id.startswith("entryA_"), e._attr_unique_id

    def test_two_entries_with_identical_config_do_not_collide(self, hass_mock, two_zone_config):
        """The exact GH #116 scenario: same zones on two config entries."""
        a, _, _ = _create_entities(hass_mock, two_zone_config, "entryA")
        b, _, _ = _create_entities(hass_mock, two_zone_config, "entryB")
        ids_a = {e._attr_unique_id for e in a}
        ids_b = {e._attr_unique_id for e in b}
        assert len(ids_a) == len(a), "duplicate unique_ids within one entry"
        assert ids_a.isdisjoint(ids_b), "unique_ids collide across entries"

    def test_core_sensors_no_longer_static(self, hass_mock, two_zone_config):
        """The two ids reported in GH #116 must not appear unprefixed."""
        entities, _, _ = _create_entities(hass_mock, two_zone_config, "entryA")
        ids = {e._attr_unique_id for e in entities}
        assert "et_hourly_estimate" not in ids
        assert "never_dry" not in ids
        assert "entryA_et_hourly_estimate" in ids
        assert "entryA_never_dry" in ids


class TestButtonUniqueIdScoping:
    def test_two_entries_with_identical_zones_do_not_collide(self, hass_mock, two_zone_config):
        a = _create_buttons(hass_mock, two_zone_config, "entryA")
        b = _create_buttons(hass_mock, two_zone_config, "entryB")
        assert a, "factory produced no buttons"
        ids_a = {x._attr_unique_id for x in a}
        ids_b = {x._attr_unique_id for x in b}
        assert all(i.startswith("entryA_") for i in ids_a)
        assert ids_a.isdisjoint(ids_b)


class TestRegistryMigration:
    @pytest.mark.asyncio
    async def test_migration_prefixes_legacy_ids_and_skips_migrated(self, hass_mock):
        """The migration callback prefixes legacy ids exactly once."""
        from never_dry import _async_migrate_unique_ids

        entry = MagicMock()
        entry.entry_id = "entryA"

        captured = {}

        async def _capture(hass, entry_id, cb):
            captured["entry_id"] = entry_id
            captured["cb"] = cb

        with patch(
            "never_dry.er.async_migrate_entries",
            new=AsyncMock(side_effect=_capture),
        ):
            await _async_migrate_unique_ids(hass_mock, entry)

        assert captured["entry_id"] == "entryA"
        cb = captured["cb"]

        legacy = MagicMock()
        legacy.unique_id = "never_dry"
        assert cb(legacy) == {"new_unique_id": "entryA_never_dry"}

        legacy_zone = MagicMock()
        legacy_zone.unique_id = "deficit_zone_orto"
        assert cb(legacy_zone) == {"new_unique_id": "entryA_deficit_zone_orto"}

        migrated = MagicMock()
        migrated.unique_id = "entryA_never_dry"
        assert cb(migrated) is None

    def test_remove_legacy_rain_entities_removes_only_old_rain(self, hass_mock):
        """Cleanup removes the pre-rework rain_zone_ entity, keeps the rest."""
        from never_dry import _async_remove_legacy_rain_entities

        entry = MagicMock()
        entry.entry_id = "entryA"

        def _e(uid, eid):
            m = MagicMock()
            m.unique_id = uid
            m.entity_id = eid
            return m

        entries = [
            _e("entryA_rain_zone_orto", "sensor.orto_rain"),  # legacy -> remove
            _e("entryA_rain_yearly_zone_orto", "sensor.orto_rain_yearly"),  # new -> keep
            _e("entryA_deficit_zone_orto", "sensor.orto_deficit"),  # keep
        ]
        registry = MagicMock()
        removed = []
        registry.async_remove.side_effect = removed.append

        with (
            patch("never_dry.er.async_get", return_value=registry),
            patch("never_dry.er.async_entries_for_config_entry", return_value=entries),
        ):
            _async_remove_legacy_rain_entities(hass_mock, entry)

        assert removed == ["sensor.orto_rain"]
