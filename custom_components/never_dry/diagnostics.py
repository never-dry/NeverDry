"""Diagnostics support for NeverDry.

Provides the data bundle downloadable via
Settings → Devices & Services → NeverDry → ⋮ → Download diagnostics.

The bundle includes:
- Tail of the dedicated activity log (never_dry_activity.log)
- State snapshot of all NeverDry entities
- Sanitised config entry data (secrets redacted)
"""

from __future__ import annotations

import os
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN

_LOG_TAIL_LINES = 500
_REDACT_KEYS = {"password", "token", "api_key", "secret", "access_token"}


def _reachability_diagnostics(hass, entry) -> dict:
    """What the fleet-silence watch has learned, if a controller owns one."""
    for key, value in hass.data.get(DOMAIN, {}).items():
        if key.startswith("_controller") and hasattr(value, "_reachability"):
            return value._reachability.diagnostics()
    return {}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict[str, Any]:
    """Return the diagnostics bundle for a NeverDry config entry.

    Called by HA when the user clicks Download diagnostics in the UI.
    """
    # ── 1. Entity states ─────────────────────────────────────────────────────
    registry = er.async_get(hass)
    entities = er.async_entries_for_config_entry(registry, entry.entry_id)
    entity_states: dict[str, Any] = {}
    for ent in entities:
        state = hass.states.get(ent.entity_id)
        if state is not None:
            entity_states[ent.entity_id] = {
                "state": state.state,
                "attributes": dict(state.attributes),
                "last_changed": state.last_changed.isoformat(),
                "last_updated": state.last_updated.isoformat(),
            }

    # ── 2. Activity log tail ──────────────────────────────────────────────────
    log_path = hass.config.path("never_dry_activity.log")
    if os.path.isfile(log_path):
        try:
            with open(log_path, encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
            log_tail = "".join(lines[-_LOG_TAIL_LINES:])
            log_total_lines = len(lines)
        except OSError as exc:
            log_tail = f"<log file unreadable: {exc}>"
            log_total_lines = 0
    else:
        log_tail = "<log file not yet created — restart or reload the integration>"
        log_total_lines = 0

    # ── 3. Sanitised config ───────────────────────────────────────────────────
    config_data = async_redact_data(dict(entry.data), _REDACT_KEYS)

    # ── 4. Valve latency statistics ──────────────────────────────────────────
    operators = hass.data.get(DOMAIN, {}).get(f"_operators_{entry.entry_id}", {})
    valve_latency = {entity_id: op.latency_diagnostics for entity_id, op in operators.items()}

    # ── 5. Flow rate learned from real sessions ──────────────────────────────
    # Reported next to the configured rate on purpose: the gap between them is
    # the whole point, and it is invisible in any single reading.
    session_flow = {
        entity_id: op.session_flow_diagnostics
        for entity_id, op in operators.items()
        if hasattr(op, "session_flow_diagnostics")
    }

    return {
        "domain": DOMAIN,
        "config_entry_id": entry.entry_id,
        "config_entry_title": entry.title,
        "config_data": config_data,
        "entity_states": entity_states,
        "valve_latency": valve_latency,
        "session_flow": session_flow,
        "reachability_watch": _reachability_diagnostics(hass, entry),
        "activity_log": {
            "path": log_path,
            "total_lines": log_total_lines,
            "lines_shown": _LOG_TAIL_LINES,
            "tail": log_tail,
        },
    }
