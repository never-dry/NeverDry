"""NeverDry — Home Assistant Custom Integration.

Calculates cumulative soil water deficit based on real-time
evapotranspiration and precipitation, following a simplified FAO-56
water balance model.  Directly controls irrigation valves.

Supports both YAML configuration and UI-based config flow.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import pathlib

import homeassistant.helpers.config_validation as cv
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.typing import ConfigType

from .const import CONF_VWC_SENSOR, CONF_ZONE_NAME, CONF_ZONE_VWC_SENSOR, CONF_ZONES, CONFIG_VERSION, DOMAIN
from .services import async_unload_services


def zone_slug(zone_name: str) -> str:
    """Slug used to build a zone device identifier.

    Must stay in sync with the slug used in sensor.py / button.py
    DeviceInfo identifiers: ``(DOMAIN, f"{entry_id}_{slug}")``.
    """
    return zone_name.lower().replace(" ", "_")


def zone_device_identifier(entry_id: str, zone_name: str) -> tuple[str, str]:
    """Device-registry identifier for a zone device."""
    return (DOMAIN, f"{entry_id}_{zone_slug(zone_name)}")


_LOGGER = logging.getLogger(__name__)

_ACTIVITY_LOG_MAX_BYTES = 5 * 1024 * 1024  # 5 MB per file
_ACTIVITY_LOG_BACKUP_COUNT = 2
_INTEGRATION_VERSION: str = json.loads((pathlib.Path(__file__).parent / "manifest.json").read_text(encoding="utf-8"))[
    "version"
]


def _setup_file_logger(hass: HomeAssistant) -> logging.Handler:
    """Attach a rotating file handler to the never_dry logger namespace.

    All modules under custom_components.never_dry use _LOGGER = logging.getLogger(__name__),
    which inherits from this namespace. Attaching once here captures every
    INFO/DEBUG line across controller, sensor, valve_operator, etc.

    File: <ha_config_dir>/never_dry_activity.log (5 MB x 3 files).
    """
    log_path = hass.config.path("never_dry_activity.log")
    handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=_ACTIVITY_LOG_MAX_BYTES,
        backupCount=_ACTIVITY_LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    nd_logger = logging.getLogger("custom_components.never_dry")
    nd_logger.setLevel(logging.DEBUG)
    nd_logger.addHandler(handler)
    _LOGGER.info(
        "NeverDry %s — activity log -> %s (%.0f MB x %d)",
        _INTEGRATION_VERSION,
        log_path,
        _ACTIVITY_LOG_MAX_BYTES / 1024 / 1024,
        _ACTIVITY_LOG_BACKUP_COUNT + 1,
    )
    return handler


def _teardown_file_logger(handler: logging.Handler) -> None:
    """Remove the rotating file handler from the never_dry logger and close it."""
    nd_logger = logging.getLogger("custom_components.never_dry")
    nd_logger.removeHandler(handler)
    nd_logger.setLevel(logging.NOTSET)
    handler.close()


PLATFORMS = ["sensor", "button"]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

_FRONTEND_REGISTERED = "_frontend_registered"
_CARD_FILENAME = "never-dry-zone-card.js"
# Serve the whole www/ DIRECTORY (StaticPathConfig serves directories reliably,
# single files can 404), then reference the card file under it.
_STATIC_URL = f"/{DOMAIN}_static"
_CARD_URL = f"{_STATIC_URL}/{_CARD_FILENAME}"


async def _async_register_lovelace_resource(hass: HomeAssistant, url: str) -> bool:
    """Register (or version-refresh) the card as a Lovelace resource.

    Lovelace resources are fetched dynamically by the frontend, so they keep
    working even when the service worker serves a stale cached index.html —
    the failure mode that makes frontend.add_extra_js_url unreliable after
    install/upgrade (GH issue #96). Only possible in storage mode; returns
    False for YAML-managed dashboards so the caller can fall back to
    add_extra_js_url.
    """
    lovelace = hass.data.get("lovelace")
    if lovelace is None:
        _LOGGER.warning("NeverDry: Lovelace data not available, falling back to add_extra_js_url")
        return False
    # HA 2026.2 renamed LovelaceData.mode -> resource_mode; support both.
    mode = getattr(lovelace, "resource_mode", None) or getattr(lovelace, "mode", None)
    if mode != "storage":
        _LOGGER.info(
            "NeverDry: Lovelace resources managed in %s mode, falling back to add_extra_js_url",
            mode,
        )
        return False

    resources = lovelace.resources
    if not resources.loaded:
        await resources.async_load()
        resources.loaded = True

    for item in resources.async_items():
        if item.get("url", "").partition("?")[0] == _CARD_URL:
            if item["url"] != url:  # stale ?v= from a previous version: bust the browser cache
                await resources.async_update_item(item["id"], {"url": url})
                _LOGGER.info("NeverDry Zone Card Lovelace resource updated to %s", url)
            else:
                _LOGGER.debug("NeverDry Zone Card Lovelace resource already current (%s)", url)
            return True

    await resources.async_create_item({"res_type": "module", "url": url})
    _LOGGER.info("NeverDry Zone Card added to Lovelace resources (%s)", url)
    return True


async def _async_card_version(hass: HomeAssistant) -> str:
    """Cache-busting token for the card URL: release version plus a content hash.

    The release version alone is not enough, and the gap is not academic. The
    browser keeps the file under this exact URL and Home Assistant's service
    worker keeps it harder still, so a card edited between releases is served
    from cache for ever — a reload does not help, because nothing about the
    address changed. That cost an evening of "the card has not been updated"
    when it had.

    Hashing the file makes the address follow the content: any edit is a new
    URL, and no edit is not. Read in an executor because setup runs on the event
    loop and this touches the disk.
    """

    def _hash() -> str:
        import hashlib

        card = pathlib.Path(__file__).parent / "www" / "never-dry-zone-card.js"
        try:
            return hashlib.sha1(card.read_bytes(), usedforsecurity=False).hexdigest()[:8]
        except OSError:
            # No card, no cache to bust; the version alone is a valid answer.
            return ""

    digest = await hass.async_add_executor_job(_hash)
    return f"{_INTEGRATION_VERSION}-{digest}" if digest else _INTEGRATION_VERSION


async def _async_register_frontend(hass: HomeAssistant) -> None:
    """Serve and auto-load the NeverDry Zone Lovelace card.

    Registers the bundled www/ folder as a static path, then exposes the card
    JS to the frontend so it appears in the "Add card" picker without manual
    resource setup. Storage-mode dashboards get a real Lovelace resource
    (robust against the service-worker-cached index, GH #96); YAML-mode
    dashboards fall back to add_extra_js_url. Runs once per HA instance.
    """
    if hass.data[DOMAIN].get(_FRONTEND_REGISTERED):
        _LOGGER.debug("NeverDry: frontend already registered, skipping")
        return

    www_dir = str(pathlib.Path(__file__).parent / "www")
    url = f"{_CARD_URL}?v={await _async_card_version(hass)}"
    try:
        from homeassistant.components.http import StaticPathConfig

        _LOGGER.info("NeverDry: registering static path %s -> %s", _STATIC_URL, www_dir)
        await hass.http.async_register_static_paths([StaticPathConfig(_STATIC_URL, www_dir, cache_headers=False)])

        if not await _async_register_lovelace_resource(hass, url):
            from homeassistant.components import frontend

            frontend.add_extra_js_url(hass, url)
        hass.data[DOMAIN][_FRONTEND_REGISTERED] = True
        _LOGGER.info("NeverDry Zone Card registered and auto-loaded (%s)", url)
    except Exception:  # never block integration setup on a frontend hiccup
        _LOGGER.exception("NeverDry: failed to register Lovelace card at %s", url)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the NeverDry integration."""
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate config entry to the current schema version.

    Called automatically by HA when entry.version < ConfigFlow.VERSION.
    Add migration steps here when CONFIG_VERSION is bumped.
    """
    _LOGGER.debug(
        "Migrating NeverDry config entry from version %s to %s",
        entry.version,
        CONFIG_VERSION,
    )

    if entry.version > CONFIG_VERSION:
        _LOGGER.error(
            "Config entry version %s is newer than supported (%s)",
            entry.version,
            CONFIG_VERSION,
        )
        return False

    if entry.version == 1:
        new_data = {**entry.data}
        for zone in new_data.get("zones", []):
            zone.setdefault("delivery_mode", "estimated_flow")
        hass.config_entries.async_update_entry(entry, data=new_data, version=2)

    if entry.version == 2:
        # The dropdown now decides which of a preset/override pair applies,
        # and the override is read only behind the "custom" entry. Two pairs
        # used to work the other way round: a stored efficiency or manual Kc
        # took charge on its own, while the dropdown went on displaying a
        # choice that no longer had any effect.
        #
        # Writing "custom" into those dropdowns changes nothing about how the
        # zone waters — the same number stays in charge — it just says so.
        # Without it the value would be ignored on the next start and the
        # zone would silently jump to its preset: a drip zone running at 0.55
        # would go to 0.92, watering less, with nobody having touched it.
        #
        # The exposure pair is deliberately NOT migrated. There the dropdown
        # already decided, so a factor sitting behind a real preset is
        # ignored today; marking it custom would switch it on and change the
        # watering, which is the exact harm this migration exists to prevent.
        # Those leftovers are the config flow's business: it warns about them
        # on the next save.
        #
        # Keep this step forever. Migrations are a chain — someone upgrading
        # from an old release still has to pass through 2 -> 3.
        new_data = {**entry.data}
        for zone in new_data.get("zones", []):
            if "efficiency" in zone:
                zone["system_type"] = "custom"
            if "kc" in zone:
                zone["plant_family"] = "custom"
        hass.config_entries.async_update_entry(entry, data=new_data, version=3)

    if entry.version == 3:
        # The soil probe was declared once for the whole installation and drove
        # every zone. That is wrong and has been for a while: a probe measures
        # one patch of soil, with one planting above it and its own watering
        # history, so its reading is not transferable to a zone watered
        # independently.
        #
        # Where the answer is unambiguous it is applied. One zone means the
        # probe is in that zone — there is nothing to ask.
        #
        # With several zones only the user knows where it is buried, so nothing
        # is guessed and nothing is deleted: the binding stays exactly where it
        # is, the installation keeps behaving as it did, and a repair issue asks
        # the question. Deleting would have degraded those zones to an estimate
        # in silence and thrown away an entity the user had already supplied.
        new_data = {**entry.data}
        probe = new_data.get(CONF_VWC_SENSOR)
        zones = new_data.get(CONF_ZONES, [])
        if probe and len(zones) == 1:
            zones[0] = {**zones[0], CONF_ZONE_VWC_SENSOR: probe}
            new_data.pop(CONF_VWC_SENSOR, None)
            _LOGGER.info(
                "Soil probe %s moved to zone '%s': with one zone there is nothing to ask",
                probe,
                zones[0].get(CONF_ZONE_NAME),
            )
        hass.config_entries.async_update_entry(entry, data=new_data, version=4)

    _LOGGER.info(
        "Migration of NeverDry config entry to version %s successful",
        CONFIG_VERSION,
    )
    return True


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the integration when config entry data changes (e.g. zone added)."""
    _LOGGER.info("Config entry data changed — reloading integration")
    await hass.config_entries.async_reload(entry.entry_id)


async def _async_migrate_unique_ids(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Prefix legacy unique_ids with the config entry id.

    Up to 0.11.0-beta.1 the core sensors used static unique_ids
    ("et_hourly_estimate", "never_dry") and the per-zone entities were
    scoped on the zone slug only, so a second config entry collided and
    HA silently dropped its entities (GH #116). Every unique_id is now
    ``<entry_id>_<legacy_id>``; this migration renames the registry
    entries of THIS config entry in place, preserving entity_id, history
    and user customisations. Idempotent: already-prefixed ids are left
    untouched.
    """
    prefix = f"{entry.entry_id}_"

    @callback
    def _migrate(reg_entry: er.RegistryEntry) -> dict[str, str] | None:
        if reg_entry.unique_id.startswith(prefix):
            return None
        return {"new_unique_id": f"{prefix}{reg_entry.unique_id}"}

    await er.async_migrate_entries(hass, entry.entry_id, _migrate)


@callback
def _async_remove_legacy_rain_entities(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove the pre-rework per-zone rain entities.

    The per-zone rain sensor moved from lifetime millimetres (device_class
    precipitation) to yearly litres (device_class water) and got a new
    unique_id (``rain_zone_`` -> ``rain_yearly_zone_``). Deleting the old
    entity here means the user never sees an ``unavailable`` orphan, and the
    new entity starts with no statistics so Home Assistant's "unit of
    measurement changed" repair never fires. The old lifetime-mm history
    (a field install read 6418 mm) is intentionally discarded.
    """
    registry = er.async_get(hass)
    for reg_entry in list(er.async_entries_for_config_entry(registry, entry.entry_id)):
        uid = reg_entry.unique_id or ""
        if "rain_zone_" in uid and "rain_yearly_zone_" not in uid:
            registry.async_remove(reg_entry.entity_id)
            _LOGGER.info(
                "Removed legacy rain entity %s (replaced by Rain Yearly)",
                reg_entry.entity_id,
            )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up NeverDry from a config entry (UI)."""
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = entry.data
    await _async_migrate_unique_ids(hass, entry)
    handler = await hass.async_add_executor_job(_setup_file_logger, hass)
    hass.data[DOMAIN][f"_log_handler_{entry.entry_id}"] = handler
    # After the file logger is attached, so the removals are visible in the
    # NeverDry activity log; still before platforms create the new entities.
    _async_remove_legacy_rain_entities(hass, entry)

    # Checked at every setup, not once at migration: adding a zone later turns
    # an unambiguous installation into one that needs asking, and answering by
    # editing the zone directly should clear the issue without anyone answering
    # the question twice.
    from .repairs import async_check_soil_probe

    async_check_soil_probe(hass, entry)

    await _async_register_frontend(hass)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    controller = hass.data.get(DOMAIN, {}).pop(f"_controller_{entry.entry_id}", None)
    if controller is not None:
        await controller.async_stop()
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        handler = hass.data[DOMAIN].pop(f"_log_handler_{entry.entry_id}", None)
        if handler is not None:
            await hass.async_add_executor_job(_teardown_file_logger, handler)
        hass.data[DOMAIN].pop(entry.entry_id, None)
        hass.data[DOMAIN].pop(f"_operators_{entry.entry_id}", None)
        # Drop the domain services when the last controller is gone.
        async_unload_services(hass)
    return unload_ok


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    entry: ConfigEntry,
    device: dr.DeviceEntry,
) -> bool:
    """Allow manual deletion of stale zone devices from the UI.

    Returning True re-enables the "Delete device" button in Home Assistant.
    We allow removal of any device whose identifier no longer maps to a
    currently configured zone (orphans left behind after a zone was removed).
    The hub device and devices belonging to still-configured zones are kept.
    """
    valid_identifiers = {(DOMAIN, entry.entry_id)}  # hub device
    for zone in entry.data.get(CONF_ZONES, []):
        valid_identifiers.add(zone_device_identifier(entry.entry_id, zone[CONF_ZONE_NAME]))

    # Removable only if NONE of the device identifiers match a live zone/hub.
    return not any(identifier in valid_identifiers for identifier in device.identifiers)
