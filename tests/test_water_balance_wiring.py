"""The entity layer computes nothing of its own any more.

The ET rate lived in five places — the ET sensor, the hub's live integrator, a
standalone helper used by tests, and twice inside the recorder backfill — and
the VWC deficit in two. All of them now call the model, so there is one place to
be right and one place to change.

The second half is quieter and matters more later: the deficit each zone carries
is tagged with the frame it was actually measured in. Nothing reads that tag
yet, which is precisely why it has to be correct now — the first reader will
inherit whatever we write today.
"""

from __future__ import annotations

from never_dry.const import (
    CONF_ALPHA,
    CONF_FIELD_CAPACITY,
    CONF_RAIN_SENSOR,
    CONF_ROOT_DEPTH,
    CONF_T_BASE,
    CONF_TEMP_SENSOR,
    CONF_VWC_SENSOR,
    CONF_ZONE_AREA,
    CONF_ZONE_NAME,
)
from never_dry.sensor import DrynessIndexSensor, ETSensor, IrrigationZoneSensor
from never_dry.water_balance_model import ETModel, ReferenceFrame, vwc_deficit_mm

# ── Helpers ───────────────────────────────────────────────────────────


def _config(**extra):
    cfg = {
        CONF_TEMP_SENSOR: "sensor.temp",
        CONF_RAIN_SENSOR: "sensor.rain",
        CONF_ALPHA: 0.22,
        CONF_T_BASE: 9.0,
    }
    cfg.update(extra)
    return cfg


def _zone(hass, di):
    return IrrigationZoneSensor(hass, {CONF_ZONE_NAME: "Orto", CONF_ZONE_AREA: 10.0}, di)


# ── One formula, reached from the entity ──────────────────────────────


class TestTheEntityUsesTheModel:
    def test_et_sensor_matches_the_model(self, hass_mock, make_event):
        sensor = ETSensor(hass_mock, _config())
        sensor._on_temp_change(make_event("24.0"))
        assert sensor.native_value == round(ETModel.et_hourly(24.0, alpha=0.22, t_base=9.0), 4)

    def test_below_base_temperature_there_is_no_demand(self, hass_mock, make_event):
        """The clamp at zero is the model's, not a second one on this side."""
        sensor = ETSensor(hass_mock, _config())
        sensor._on_temp_change(make_event("3.0"))
        assert sensor.native_value == 0.0

    def test_a_custom_alpha_reaches_the_model(self, hass_mock, make_event):
        sensor = ETSensor(hass_mock, _config(**{CONF_ALPHA: 0.5, CONF_T_BASE: 5.0}))
        sensor._on_temp_change(make_event("20.0"))
        assert sensor.native_value == round(ETModel.et_hourly(20.0, alpha=0.5, t_base=5.0), 4)

    def test_the_vwc_deficit_matches_the_model(self, hass_mock, make_state):
        di = DrynessIndexSensor(
            hass_mock,
            _config(**{CONF_VWC_SENSOR: "sensor.vwc", CONF_FIELD_CAPACITY: 0.30, CONF_ROOT_DEPTH: 0.30}),
        )
        hass_mock.states.get = lambda eid: make_state("0.18")

        di._update_from_vwc()

        assert di.deficit == vwc_deficit_mm(0.18, field_capacity=0.30, root_depth=0.30)

    def test_wetter_than_field_capacity_is_still_clamped_at_zero(self, hass_mock, make_state):
        """The model returns the negative; holding the floor stays the caller's job."""
        di = DrynessIndexSensor(
            hass_mock,
            _config(**{CONF_VWC_SENSOR: "sensor.vwc", CONF_FIELD_CAPACITY: 0.30, CONF_ROOT_DEPTH: 0.30}),
        )
        hass_mock.states.get = lambda eid: make_state("0.45")

        di._update_from_vwc()

        assert di.deficit == 0.0
        assert vwc_deficit_mm(0.45, field_capacity=0.30, root_depth=0.30) < 0


# ── The deficit carries the frame it was measured in ──────────────────


class TestTheDeficitIsTaggedWithItsFrame:
    """A `Deficit` exists to stop two numbers from different frames being
    compared. It can only do that if the tag is true."""

    def test_et_mode_tags_et(self, hass_mock):
        di = DrynessIndexSensor(hass_mock, _config())
        assert _zone(hass_mock, di)._zone.deficit.frame is ReferenceFrame.ET

    def test_a_soil_probe_tags_vwc_system(self, hass_mock):
        """With a probe configured the deficit is a measurement, not an integration."""
        di = DrynessIndexSensor(hass_mock, _config(**{CONF_VWC_SENSOR: "sensor.vwc"}))
        assert _zone(hass_mock, di)._zone.deficit.frame is ReferenceFrame.VWC_SYSTEM

    def test_the_frame_survives_a_credit(self, hass_mock):
        """Arithmetic must not quietly re-tag the value it produces."""
        di = DrynessIndexSensor(hass_mock, _config(**{CONF_VWC_SENSOR: "sensor.vwc"}))
        zone = _zone(hass_mock, di)
        zone._zone_deficit = 5.0

        zone.credit_delivery(10.0)

        assert zone._zone.deficit.frame is ReferenceFrame.VWC_SYSTEM

    def test_zones_of_one_site_share_a_frame(self, hass_mock):
        """The system probe is shared, so its frame is comparable across zones."""
        di = DrynessIndexSensor(hass_mock, _config(**{CONF_VWC_SENSOR: "sensor.vwc"}))
        a, b = _zone(hass_mock, di), _zone(hass_mock, di)
        assert a._zone.deficit.is_comparable_to(b._zone.deficit)
