"""Every field bug names the test that reproduces it, and the name is checked.

A fix without a test that fails on the old code is a fix that will be undone,
and the undoing is silent. This registry makes the link between a bug and its
reproduction a *checked* fact: rename or delete one of these tests and this file
fails, naming the bug that just lost its guard.

It matters most during a refactor. While the domain model is being wired, tests
move between files and classes; the ones that pin field behaviour are exactly
the ones that must survive the move, and they are the easiest to lose because
nothing else points at them.

Deliberately not a coverage metric. It says "this bug has a reproduction", not
"this bug cannot come back" — the second is not something a test suite can
promise. Entries carry the symptom rather than the fix, because the symptom is
what a future reader will recognise.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

TESTS = pathlib.Path(__file__).resolve().parent

#: bug key -> (what the user saw, the tests that reproduce it)
#:
#: The key is the GitHub issue where there is one. Field bugs found by audit
#: rather than by report get a slug: they are no less real, and several of the
#: worst ones were never reported because nothing visibly broke.
REGRESSIONS: dict[str, tuple[str, tuple[str, ...]]] = {
    "gh-99": (
        "the zone card was not registered, so the dashboard had no card to add",
        (
            "test_frontend_registration.py::test_missing_lovelace_data_logs_fallback",
            "test_frontend_registration.py::test_storage_mode_detected_via_resource_mode",
        ),
    ),
    "gh-105": (
        "with two config entries, one controller captured every service call and "
        "the other's zones were 'not found' — their valves never switched",
        (
            "test_controller.py::TestControllerState::test_register_services_does_not_register_ha_services",
            "test_multi_controller_services.py::TestZoneScopedDispatch::test_routes_to_first_controller",
            "test_multi_controller_services.py::TestZoneScopedDispatch::test_routes_to_second_controller",
            "test_multi_controller_services.py::TestZoneScopedDispatch::test_unknown_zone_logs_all_available",
            "test_multi_controller_services.py::TestZoneScopedDispatch::test_no_zone_name_fans_out",
            "test_multi_controller_services.py::TestZoneScopedDispatch::test_duplicate_zone_name_uses_first_and_warns",
        ),
    ),
    "gh-116": (
        "a second config entry was born with no entities at all: the unique_ids "
        "collided and Home Assistant dropped the duplicates in silence",
        (
            "test_unique_id_scoping.py::TestSensorUniqueIdScoping::test_core_sensors_no_longer_static",
            "test_unique_id_scoping.py::TestSensorUniqueIdScoping::test_two_entries_with_identical_config_do_not_collide",
        ),
    ),
    "gh-123": (
        "rain counted more than once on accumulator sensors, so zones were "
        "under-watered; and a new zone inherited a deficit it had never earned",
        (
            "test_never_dry_sensor.py::TestRollingWindowRainSensor::test_midnight_reset_credits_increments_not_the_drop",
            "test_environment.py::TestYearlyRain::test_ignores_a_decrease",
            "test_zone_deficit_sensor.py::TestZoneDeficitSeeding::test_new_zone_starts_at_zero_ignoring_dryness_index",
            "test_zone_deficit_sensor.py::TestZoneDeficitSeeding::test_restore_overrides_seed",
            "test_architecture.py::test_domain_enums_mirror_const",
        ),
    ),
    "gh-139": (
        "imperial users saw deficits rounded to whole inches, and a zone edited "
        "twice drifted a little further each time",
        (
            "test_sensor_device_classes.py::TestDisplayPrecision::test_dryness_index_precision",
            "test_sensor_device_classes.py::TestDisplayPrecision::test_et_sensor_precision",
            "test_sensor_device_classes.py::TestDisplayPrecision::test_zone_deficit_precision",
            "test_sensor_device_classes.py::TestDisplayPrecision::test_zone_threshold_precision",
            "test_unit_conversions.py::TestImperialReconfigureRoundTrip::test_threshold_reconfigure_is_idempotent",
            "test_unit_conversions.py::TestImperialReconfigureRoundTrip::test_d_max_reconfigure_is_idempotent",
            "test_unit_conversions.py::TestImperialReconfigureRoundTrip::test_threshold_roundtrip_drift_bounded",
        ),
    ),
    "gh-144": (
        "in soil-probe mode every zone's Rain Yearly stayed at zero forever",
        ("test_never_dry_sensor.py::TestVWCMode::test_vwc_mode_accrues_yearly_rain_after_rain",),
    ),
    "gh-165": (
        "an efficiency override set by accident could never be cleared again: "
        "emptying the field silently restored the old value",
        ("test_zone_override_clearing.py::TestOverridesAreClearable::test_overrides_are_offered_not_reinjected",),
    ),
    "gh-170": (
        "a probe reporting percentages pinned the deficit at zero for ever, so "
        "the zone never watered and nothing said why",
        (
            "test_never_dry_sensor.py::TestVWCMode::test_vwc_percentage_reading_waters_the_zone",
            "test_water_balance_model.py::TestVWCToFraction::test_exactly_one_is_saturation_not_one_percent",
            "test_water_balance_model.py::TestVWCToFraction::test_reads_both_scales",
            "test_water_balance_model.py::TestVWCToFraction::test_rejects_what_is_not_a_water_content",
        ),
    ),
    "gh-173": (
        "a flow meter that stopped counting kept the valve open for the whole "
        "hour, on a zone with five minutes of work to do",
        (
            "test_delivery_modes.py::TestStalledFlowMeter::test_stalled_meter_no_longer_runs_for_the_whole_hour",
            "test_delivery_modes.py::TestStalledFlowMeter::test_the_bound_follows_the_job",
            "test_expected_duration.py::TestDeliveryTimeoutScaling::test_short_job_gets_a_short_timeout",
            "test_expected_duration.py::TestDeliveryTimeoutScaling::test_configured_value_caps_a_long_job",
            "test_expected_duration.py::TestDeliveryTimeoutScaling::test_no_guard_flow_stays_at_floor",
            "test_expected_duration.py::TestDeliveryTimeoutScaling::test_ignores_live_rate",
        ),
    ),
    "reload-leaves-the-old-setup-running": (
        "every options-flow save left the previous copy subscribed: two hubs "
        "advancing two water balances, and a second watchdog on every valve",
        (
            "test_unload_releases_subscriptions.py::TestEntityListenersAreReleasedOnRemoval::test_dryness_index",
            "test_unload_releases_subscriptions.py::TestControllerStopDetachesOperators::test_unloads_every_operator",
            "test_unload_releases_subscriptions.py::TestControllerStopDetachesOperators::test_reload_leaves_only_the_successor_subscribed",
            "test_unload_releases_subscriptions.py::TestControllerStopDetachesOperators::test_watchdog_of_a_stopped_operator_cannot_fire",
        ),
    ),
    "partial-delivery-skipped-the-year-roll": (
        "a session that stopped short on 1 January added to last year's "
        "Irrigated Yearly, and the jump reached long-term statistics",
        (
            "test_zone_settle_wiring.py::TestYearlyTotalRollsOnAPartialDelivery::test_a_new_year_clears_the_previous_total",
            "test_zone_settle_wiring.py::TestYearlyTotalRollsOnAPartialDelivery::test_the_lifetime_total_is_preserved",
            "test_zone_settle_wiring.py::TestManualSessionSettlesThroughTheZone::test_a_new_year_rolls_here_too",
        ),
    ),
}

#: Closed issues that deliberately have no reproduction here, and why. Being on
#: this list is a decision, not an oversight — which is the point of writing it.
NO_REPRODUCTION: dict[str, str] = {
    "gh-74": "a direction discussion, not a defect",
    "gh-126": "an RFC on configurable field capacity, not a defect",
    "gh-96": "superseded by gh-99, which covers the same registration path",
    "gh-142": "a feature (the hub reset buttons), covered by its own tests",
}


# ── Helpers ───────────────────────────────────────────────────────────


def _collected_node_ids() -> set[str]:
    """Every test in the suite, as ``file::[Class::]name``.

    Parsed rather than run: this file must be able to say "that test is gone"
    without executing anything, and be cheap enough to never be skipped.
    """
    found: set[str] = set()
    for path in sorted(TESTS.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for top in tree.body:
            if isinstance(top, ast.FunctionDef | ast.AsyncFunctionDef) and top.name.startswith("test_"):
                found.add(f"{path.name}::{top.name}")
            elif isinstance(top, ast.ClassDef):
                for sub in top.body:
                    if isinstance(sub, ast.FunctionDef | ast.AsyncFunctionDef) and sub.name.startswith("test_"):
                        found.add(f"{path.name}::{top.name}::{sub.name}")
    return found


# ── The registry holds ────────────────────────────────────────────────


@pytest.mark.parametrize("bug", sorted(REGRESSIONS))
def test_every_registered_bug_still_has_its_reproduction(bug):
    """The named tests exist. A rename that loses one fails here, by name."""
    symptom, node_ids = REGRESSIONS[bug]
    missing = sorted(set(node_ids) - _collected_node_ids())
    assert not missing, f"{bug} ({symptom}) lost its reproduction: {missing}"


@pytest.mark.parametrize("bug", sorted(REGRESSIONS))
def test_every_registered_bug_names_at_least_one_test(bug):
    _, node_ids = REGRESSIONS[bug]
    assert node_ids, f"{bug} is registered with no reproduction at all"


@pytest.mark.parametrize("bug", sorted(REGRESSIONS))
def test_every_registered_bug_describes_the_symptom(bug):
    """Not the fix — the symptom, which is what a future reader recognises."""
    symptom, _ = REGRESSIONS[bug]
    assert len(symptom) > 30, f"{bug} needs a symptom a reader would recognise"


def test_a_bug_is_either_reproduced_or_explicitly_not():
    """No key may sit in both lists, and none may sit in neither by accident."""
    both = sorted(set(REGRESSIONS) & set(NO_REPRODUCTION))
    assert not both, f"listed as both reproduced and not: {both}"


@pytest.mark.parametrize("bug", sorted(NO_REPRODUCTION))
def test_an_unreproduced_bug_says_why(bug):
    assert len(NO_REPRODUCTION[bug]) > 15, f"{bug} needs a reason, not a placeholder"
