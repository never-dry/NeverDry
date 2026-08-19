"""Repairs: the questions only the user can answer.

Home Assistant's repair flow exists for exactly this shape of problem — the
integration knows something is wrong, and cannot fix it because the missing
piece is knowledge the user has and the software does not.

The one here is the soil probe. It used to be declared once for the whole
installation and drove every zone, which is a design error rather than a
shortcut: a probe measures one patch of soil, with one kind of planting above
it and its own watering history, so its reading is not transferable to a zone
watered independently.

Where the answer is unambiguous the migration applies it: one zone means the
probe is in that zone. With several zones only the user knows where it is
buried, and the three ways of not asking are all worse:

* **guessing** puts a measurement on the wrong patch of ground, and a wrong
  measurement is more convincing than a wrong estimate;
* **deleting** degrades those zones to an estimate in silence, and throws away
  an entity the user already supplied;
* **a notification** is dismissed and forgotten, and nothing remembers.

A repair issue is none of those. It stays until it is answered, the
installation goes on behaving exactly as before while it waits, and the question
is asked in the one place where the user is looking for things to fix.
"""

from __future__ import annotations

import voluptuous as vol
from homeassistant import data_entry_flow
from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.helpers import selector

from .const import CONF_VWC_SENSOR, CONF_ZONE_NAME, CONF_ZONE_VWC_SENSOR, CONF_ZONES, DOMAIN

ISSUE_PROBE_ZONE = "soil_probe_needs_a_zone"


class SoilProbeZoneFlow(RepairsFlow):
    """Ask which zone the installation-wide soil probe is buried in."""

    def __init__(self, entry_id: str) -> None:
        """Remember which entry raised the issue: an installation may have several."""
        self._entry_id = entry_id

    async def async_step_init(self, user_input: dict | None = None) -> data_entry_flow.FlowResult:
        """Single step: pick the zone, or leave it as it is."""
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input: dict | None = None) -> data_entry_flow.FlowResult:
        """Move the probe into the chosen zone and drop the site-wide binding."""
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None:
            return self.async_abort(reason="entry_gone")

        zones = entry.data.get(CONF_ZONES, [])
        names = [z.get(CONF_ZONE_NAME, "") for z in zones]

        if user_input is not None:
            chosen = user_input["zone"]
            probe = entry.data.get(CONF_VWC_SENSOR)
            new_zones = [{**z, CONF_ZONE_VWC_SENSOR: probe} if z.get(CONF_ZONE_NAME) == chosen else z for z in zones]
            new_data = {**entry.data, CONF_ZONES: new_zones}
            new_data.pop(CONF_VWC_SENSOR, None)
            self.hass.config_entries.async_update_entry(entry, data=new_data)
            return self.async_create_entry(title="", data={})

        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema(
                {
                    vol.Required("zone"): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=names, mode="dropdown")
                    )
                }
            ),
            description_placeholders={
                "probe": entry.data.get(CONF_VWC_SENSOR, ""),
                "zones": ", ".join(names),
            },
        )


async def async_create_fix_flow(hass: HomeAssistant, issue_id: str, data: dict | None) -> RepairsFlow:
    """Build the flow for a raised issue."""
    return SoilProbeZoneFlow((data or {}).get("entry_id", ""))


def async_check_soil_probe(hass: HomeAssistant, entry) -> None:
    """Raise or clear the issue, depending on whether the probe still needs a home.

    Called at every setup rather than once at migration: a user can add zones
    after upgrading, which turns an unambiguous installation into one that needs
    asking, and can answer the question by editing the zone directly, which
    should make the issue disappear without being answered.
    """
    from homeassistant.helpers import issue_registry as ir

    zones = entry.data.get(CONF_ZONES, [])
    needs_asking = bool(entry.data.get(CONF_VWC_SENSOR)) and len(zones) > 1

    if needs_asking:
        ir.async_create_issue(
            hass,
            DOMAIN,
            f"{ISSUE_PROBE_ZONE}_{entry.entry_id}",
            is_fixable=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_PROBE_ZONE,
            translation_placeholders={"probe": entry.data[CONF_VWC_SENSOR]},
            data={"entry_id": entry.entry_id},
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, f"{ISSUE_PROBE_ZONE}_{entry.entry_id}")
