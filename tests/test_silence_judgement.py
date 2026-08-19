"""Judging a quiet valve against its siblings.

The rule has to survive the fleet sizes and the failure shapes that actually
occur, not just the happy one. Each case below is one that would otherwise be
found in someone's garden — and the table at the bottom is the comparison that
decided the estimator, kept as a test so the decision stays checkable.
"""

from __future__ import annotations

import pytest
from never_dry.environment import (
    Reachability,
    judge_fleet,
    judge_silence,
    mad,
    silence_floor,
)

MINUTE = 60.0
HOUR = 3600.0
FLOOR = 30 * MINUTE  # a bench value: it makes both halves of the bar bite


# ── The case it exists for ────────────────────────────────────────────────


class TestOneValveGoneQuiet:
    """A battery dies mid-season: the valve stops answering and nothing says so."""

    def test_the_dead_one_is_singled_out(self):
        verdict = judge_silence(3 * HOUR, [4 * MINUTE, 4 * MINUTE, 5 * MINUTE], floor_s=FLOOR)
        assert verdict.status is Reachability.SILENT

    def test_the_verdict_carries_the_numbers_it_used(self):
        """So the warning can explain itself instead of just asserting."""
        verdict = judge_silence(3 * HOUR, [4 * MINUTE, 4 * MINUTE, 5 * MINUTE], floor_s=FLOOR)
        assert verdict.reference_s == 4 * MINUTE
        assert verdict.threshold_s == FLOOR  # tight fleet: the floor decides
        assert verdict.silence_s == 3 * HOUR

    def test_the_healthy_siblings_are_not(self):
        fleet = {"pino": 3 * HOUR, "ortensia": 4 * MINUTE, "melograno": 4 * MINUTE, "melino": 5 * MINUTE}
        verdicts = judge_fleet(fleet, floor_s=FLOOR)
        assert verdicts["pino"].is_silent
        assert not any(verdicts[z].is_silent for z in ("ortensia", "melograno", "melino"))


# ── Leave-one-out is what makes small fleets work ─────────────────────────


class TestTheSubjectIsLeftOutOfItsOwnReference:
    def test_two_valves_one_dead(self):
        """Included in its own reference, the dead valve acquits itself."""
        verdicts = judge_fleet({"dead": 4 * HOUR, "alive": 5 * MINUTE}, floor_s=FLOOR, min_peers=1)
        assert verdicts["dead"].is_silent
        assert verdicts["dead"].reference_s == 5 * MINUTE

    def test_a_single_valve_cannot_be_judged(self):
        assert judge_fleet({"only": 6 * HOUR}, floor_s=FLOOR)["only"].status is Reachability.UNKNOWN

    def test_too_few_peers_is_unknown_not_fine(self):
        """The distinction the whole enum exists for."""
        verdict = judge_silence(6 * HOUR, [5 * MINUTE], floor_s=FLOOR, min_peers=2)
        assert verdict.status is Reachability.UNKNOWN
        assert not verdict.is_silent
        assert verdict.reference_s is None


# ── The MAD: the bar widens when the fleet is genuinely irregular ─────────


class TestDispersionWidensTheBar:
    def test_an_irregular_fleet_does_not_accuse_its_slow_member(self):
        """Peers scattered from 10 to 120 min: 150 min is not out of character."""
        verdict = judge_silence(150 * MINUTE, [10 * MINUTE, 60 * MINUTE, 120 * MINUTE], floor_s=FLOOR)
        assert verdict.status is Reachability.LIVE
        assert verdict.threshold_s > 2 * HOUR  # widened well past the median

    def test_the_same_silence_is_a_fault_in_a_regular_fleet(self):
        """Identical candidate, disciplined peers: now it stands out."""
        verdict = judge_silence(150 * MINUTE, [10 * MINUTE, 11 * MINUTE, 10 * MINUTE], floor_s=FLOOR)
        assert verdict.status is Reachability.SILENT

    def test_mad_is_zero_for_identical_peers(self):
        assert mad([5.0, 5.0, 5.0]) == 0.0

    def test_mad_ignores_a_single_wild_value(self):
        """Where the standard deviation would be dragged out of shape."""
        assert mad([10.0, 10.0, 12.0, 5000.0]) < 10.0

    def test_mad_of_nothing_is_zero(self):
        assert mad([]) == 0.0


# ── The floor stops noise from becoming alarms ────────────────────────────


class TestTheFloor:
    def test_a_tiny_reference_does_not_make_jitter_a_fault(self):
        """Right after a restart everything is seconds old, and the MAD is zero."""
        assert judge_silence(90.0, [30.0, 30.0, 30.0], floor_s=FLOOR).status is Reachability.LIVE

    def test_a_slow_fleet_raises_the_bar_above_the_floor(self):
        verdict = judge_silence(5 * HOUR, [1 * HOUR, 1 * HOUR, 70 * MINUTE], floor_s=FLOOR)
        assert verdict.status is Reachability.SILENT
        assert verdict.threshold_s > FLOOR

    def test_exactly_at_the_threshold_is_not_a_fault(self):
        assert judge_silence(FLOOR, [1.0, 1.0, 1.0], floor_s=FLOOR).status is Reachability.LIVE


# ── Fleet-wide situations ─────────────────────────────────────────────────


class TestWholeFleetSituations:
    def test_everything_fresh_after_a_restart_accuses_nobody(self):
        """The startup false positive, answered by the shape of the rule itself."""
        fleet = dict.fromkeys(("a", "b", "c", "d"), 20.0)
        assert not any(v.is_silent for v in judge_fleet(fleet, floor_s=FLOOR).values())

    def test_the_whole_mesh_down_accuses_nobody(self):
        """Correct: not a fault of a valve, and the coordinator reports it itself."""
        fleet = dict.fromkeys(("a", "b", "c", "d"), 9 * HOUR)
        assert not any(v.is_silent for v in judge_fleet(fleet, floor_s=FLOOR).values())

    def test_two_dead_out_of_four_are_both_found(self):
        fleet = {"a": 6 * HOUR, "b": 6 * HOUR, "c": 3 * MINUTE, "d": 4 * MINUTE}
        verdicts = judge_fleet(fleet, floor_s=FLOOR)
        assert verdicts["a"].is_silent and verdicts["b"].is_silent
        assert not verdicts["c"].is_silent and not verdicts["d"].is_silent

    def test_a_wild_sibling_does_not_blind_the_rule(self):
        """One valve back after a week away must not hide a dead one.

        This is the case Tukey's fence misses: the wild value inflates the IQR
        and the fence opens wide enough to swallow the fault.
        """
        fleet = {"dead": 3 * HOUR, "ok": 3 * MINUTE, "ok2": 4 * MINUTE, "rejoined": 83 * HOUR}
        assert judge_fleet(fleet, floor_s=FLOOR)["dead"].is_silent

    def test_a_majority_dead_hides_them(self):
        """An honest limit, written down rather than discovered later.

        Once the quiet ones are the majority they *are* the reference. A relative
        measure cannot do better; only an absolute floor low enough to be noisy
        would catch this.
        """
        fleet = {"a": 6 * HOUR, "b": 6 * HOUR, "c": 6 * HOUR, "d": 4 * MINUTE}
        assert not any(v.is_silent for v in judge_fleet(fleet, floor_s=FLOOR).values())


# ── Deriving the floor from cadence ───────────────────────────────────────


class TestSilenceFloor:
    """The floor comes from the upper tail, because reporting is bursty."""

    def test_bursty_reporting_is_not_described_by_its_median(self):
        """The measurement that decided this, in miniature.

        A live fleet gave a median gap of one minute and legitimate silences of
        many hours: a floor on the median would have been three minutes and
        would have called every sleeping valve dead.
        """
        bursty = [1 * MINUTE] * 40 + [9 * HOUR, 12 * HOUR, 16 * HOUR]
        floor = silence_floor(bursty)
        assert floor >= 9 * HOUR

    def test_a_genuinely_regular_fleet_gets_a_tight_floor(self):
        intervals = [10 * MINUTE] * 40
        assert silence_floor(intervals) == 10 * MINUTE

    def test_below_the_sample_size_the_longest_seen_is_used(self):
        """A quantile over four points is a fiction; the longest real one is not."""
        assert silence_floor([1 * MINUTE, 2 * MINUTE, 3 * HOUR]) == 3 * HOUR

    def test_the_value_returned_actually_occurred(self):
        """Nearest-rank, not interpolated: no invented number becomes a threshold."""
        intervals = [float(i) for i in range(1, 101)]
        assert silence_floor(intervals) in intervals

    def test_no_observation_means_no_derived_floor(self):
        assert silence_floor([]) is None

    def test_zero_and_negative_intervals_are_ignored(self):
        """A restart can produce a zero delta; it says nothing about cadence."""
        assert silence_floor([0.0, -5.0, 10 * MINUTE, 30 * MINUTE]) == 30 * MINUTE

    def test_only_unusable_values_means_none(self):
        assert silence_floor([0.0, 0.0]) is None


# ── The comparison that chose the estimator ───────────────────────────────


@pytest.mark.parametrize(
    ("case", "fleet", "subject", "expected"),
    [
        ("one dead of four", {"a": 3 * HOUR, "b": 2.5 * MINUTE, "c": 2.5 * MINUTE, "d": 2.5 * MINUTE}, "a", True),
        ("one dead 40 min", {"a": 40 * MINUTE, "b": 2.5 * MINUTE, "c": 2.5 * MINUTE, "d": 2.5 * MINUTE}, "a", True),
        ("two dead of four", {"a": 3 * HOUR, "b": 3 * HOUR, "c": 2.5 * MINUTE, "d": 2.5 * MINUTE}, "a", True),
        ("three valves, one dead", {"a": 3 * HOUR, "b": 2.5 * MINUTE, "c": 2.5 * MINUTE}, "a", True),
        ("wild sibling", {"a": 3 * HOUR, "b": 3 * MINUTE, "c": 4 * MINUTE, "d": 83 * HOUR}, "a", True),
        ("all fresh", {"a": 30.0, "b": 30.0, "c": 30.0, "d": 30.0}, "a", False),
        ("mesh down", {"a": 5 * HOUR, "b": 5 * HOUR, "c": 5 * HOUR, "d": 5 * HOUR}, "a", False),
    ],
)
def test_the_decision_table(case, fleet, subject, expected):
    """Every row Tukey's fence was measured against. It missed the last three
    positives; this estimator takes them, and agrees on the two negatives."""
    assert judge_fleet(fleet, floor_s=FLOOR)[subject].is_silent is expected, case
