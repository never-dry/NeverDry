"""Tests for the Lovelace card frontend registration in __init__.py."""

import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from never_dry import (
    _CARD_URL,
    _FRONTEND_REGISTERED,
    _STATIC_URL,
    _async_register_frontend,
)
from never_dry.const import DOMAIN


@pytest.fixture
def frontend_stubs(monkeypatch):
    """Stub homeassistant.components.http and .frontend for the duration of a test.

    Returns the ``add_extra_js_url`` mock so tests can assert on it.
    """
    http_mod = ModuleType("homeassistant.components.http")
    http_mod.StaticPathConfig = lambda url, path, cache_headers=False: (url, path, cache_headers)

    frontend_mod = ModuleType("homeassistant.components.frontend")
    frontend_mod.add_extra_js_url = MagicMock()

    components_mod = sys.modules.get("homeassistant.components") or ModuleType("homeassistant.components")
    monkeypatch.setattr(components_mod, "frontend", frontend_mod, raising=False)

    monkeypatch.setitem(sys.modules, "homeassistant.components", components_mod)
    monkeypatch.setitem(sys.modules, "homeassistant.components.http", http_mod)
    monkeypatch.setitem(sys.modules, "homeassistant.components.frontend", frontend_mod)

    return frontend_mod.add_extra_js_url


def _make_hass():
    hass = MagicMock()
    hass.data = {DOMAIN: {}}
    hass.http = MagicMock()
    hass.http.async_register_static_paths = AsyncMock()

    # Runs the job instead of returning a mock: the card URL now carries a hash
    # of the card file, read in an executor because setup is on the event loop.
    # A mock here would make every test that registers the frontend fail on an
    # await, which is how this arrived.
    async def _executor(func, *args):
        return func(*args)

    hass.async_add_executor_job = _executor
    return hass


async def test_registers_static_path_and_card(frontend_stubs):
    """Happy path: registers the static dir and adds the card JS URL once."""
    add_extra_js_url = frontend_stubs
    hass = _make_hass()

    await _async_register_frontend(hass)

    hass.http.async_register_static_paths.assert_awaited_once()
    add_extra_js_url.assert_called_once()
    called_url = add_extra_js_url.call_args.args[1]
    assert called_url.startswith(f"{_CARD_URL}?v=")
    assert hass.data[DOMAIN][_FRONTEND_REGISTERED] is True

    # The static path serves the bundled www/ directory under _STATIC_URL.
    static_arg = hass.http.async_register_static_paths.call_args.args[0][0]
    assert static_arg[0] == _STATIC_URL


async def test_skips_when_already_registered(frontend_stubs):
    """Early return when the frontend was already registered for this instance."""
    add_extra_js_url = frontend_stubs
    hass = _make_hass()
    hass.data[DOMAIN][_FRONTEND_REGISTERED] = True

    await _async_register_frontend(hass)

    hass.http.async_register_static_paths.assert_not_awaited()
    add_extra_js_url.assert_not_called()


async def test_does_not_raise_on_failure(frontend_stubs):
    """A frontend hiccup must never block integration setup."""
    hass = _make_hass()
    hass.http.async_register_static_paths.side_effect = RuntimeError("boom")

    # Should swallow the exception and leave the integration un-flagged.
    await _async_register_frontend(hass)

    assert hass.data[DOMAIN].get(_FRONTEND_REGISTERED) is not True


def _make_resources(items=None, loaded=True):
    """Fake lovelace ResourceStorageCollection."""
    resources = SimpleNamespace(loaded=loaded)
    resources._items = items or []
    resources.async_items = lambda: resources._items
    resources.async_load = AsyncMock()
    resources.async_create_item = AsyncMock()
    resources.async_update_item = AsyncMock()
    return resources


def _with_lovelace(hass, mode="storage", resources=None, mode_attr="mode"):
    """Inject a fake LovelaceData into hass.data.

    ``mode_attr`` selects the dataclass field name: "mode" mirrors HA ≤ 2026.1,
    "resource_mode" mirrors HA ≥ 2026.2 (renamed in core PR #158265).
    """
    hass.data["lovelace"] = SimpleNamespace(**{mode_attr: mode, "resources": resources or _make_resources()})
    return hass.data["lovelace"].resources


async def test_storage_mode_creates_lovelace_resource(frontend_stubs):
    """Storage mode: the card is registered as a real Lovelace resource, not extra_js_url."""
    add_extra_js_url = frontend_stubs
    hass = _make_hass()
    resources = _with_lovelace(hass)

    await _async_register_frontend(hass)

    resources.async_create_item.assert_awaited_once()
    created = resources.async_create_item.call_args.args[0]
    assert created["res_type"] == "module"
    assert created["url"].startswith(f"{_CARD_URL}?v=")
    add_extra_js_url.assert_not_called()
    assert hass.data[DOMAIN][_FRONTEND_REGISTERED] is True


async def test_storage_mode_refreshes_stale_resource_version(frontend_stubs):
    """An existing resource with an old ?v= cache-buster is updated in place."""
    hass = _make_hass()
    stale = {"id": "abc123", "url": f"{_CARD_URL}?v=0.0.1"}
    resources = _with_lovelace(hass, resources=_make_resources(items=[stale]))

    await _async_register_frontend(hass)

    resources.async_create_item.assert_not_awaited()
    resources.async_update_item.assert_awaited_once()
    item_id, updates = resources.async_update_item.call_args.args
    assert item_id == "abc123"
    assert updates["url"].startswith(f"{_CARD_URL}?v=")
    assert updates["url"] != stale["url"]


async def test_storage_mode_leaves_current_resource_untouched(frontend_stubs):
    """A resource already pointing at the current version is neither duplicated nor updated."""

    hass = _make_hass()
    # Built the way production builds it: the token is the release version plus
    # a hash of the card file, so a URL carrying only the version is by
    # definition stale and *should* be rewritten.
    from never_dry import _async_card_version

    current = {"id": "abc123", "url": f"{_CARD_URL}?v={await _async_card_version(hass)}"}
    resources = _with_lovelace(hass, resources=_make_resources(items=[current]))

    await _async_register_frontend(hass)

    resources.async_create_item.assert_not_awaited()
    resources.async_update_item.assert_not_awaited()
    assert hass.data[DOMAIN][_FRONTEND_REGISTERED] is True


async def test_storage_mode_loads_resources_when_not_loaded(frontend_stubs):
    """The resource collection is loaded on demand before being inspected."""
    hass = _make_hass()
    resources = _with_lovelace(hass, resources=_make_resources(loaded=False))

    await _async_register_frontend(hass)

    resources.async_load.assert_awaited_once()
    assert resources.loaded is True
    resources.async_create_item.assert_awaited_once()


async def test_yaml_mode_falls_back_to_extra_js_url(frontend_stubs):
    """YAML-managed dashboards (read-only resources) fall back to add_extra_js_url."""
    add_extra_js_url = frontend_stubs
    hass = _make_hass()
    resources = _with_lovelace(hass, mode="yaml")

    await _async_register_frontend(hass)

    resources.async_create_item.assert_not_awaited()
    add_extra_js_url.assert_called_once()
    assert hass.data[DOMAIN][_FRONTEND_REGISTERED] is True


async def test_storage_mode_detected_via_resource_mode(frontend_stubs):
    """Regression GH #99: HA 2026.2 renamed LovelaceData.mode to resource_mode.

    On HA ≥ 2026.2 the old attribute is gone; the storage-mode detection must
    read resource_mode, otherwise every install silently falls back to the
    unreliable add_extra_js_url and the card is never loaded.
    """
    add_extra_js_url = frontend_stubs
    hass = _make_hass()
    resources = _with_lovelace(hass, mode_attr="resource_mode")

    await _async_register_frontend(hass)

    resources.async_create_item.assert_awaited_once()
    add_extra_js_url.assert_not_called()
    assert hass.data[DOMAIN][_FRONTEND_REGISTERED] is True


async def test_yaml_mode_via_resource_mode_falls_back(frontend_stubs):
    """YAML mode expressed through the renamed attribute also falls back."""
    add_extra_js_url = frontend_stubs
    hass = _make_hass()
    resources = _with_lovelace(hass, mode="yaml", mode_attr="resource_mode")

    await _async_register_frontend(hass)

    resources.async_create_item.assert_not_awaited()
    add_extra_js_url.assert_called_once()


async def test_missing_lovelace_data_logs_fallback(frontend_stubs, caplog):
    """The fallback path must be diagnosable from the logs (gap behind GH #99)."""
    add_extra_js_url = frontend_stubs
    hass = _make_hass()  # no "lovelace" key in hass.data

    with caplog.at_level("WARNING", logger="never_dry"):
        await _async_register_frontend(hass)

    add_extra_js_url.assert_called_once()
    assert any("falling back to add_extra_js_url" in r.message for r in caplog.records)
