"""Tests for collecting the silence that the judge consumes.

The criterion itself is covered by test_silence_judgement.py. What is exercised
here is the half that was missing until a live installation showed why it
mattered: two valves off the Zigbee mesh, on 2026-08-18, that Home Assistant
still reported as an ordinary `off` and that every direct check called healthy.

Each driver supplies its own silence; this module only collects the fleet and
hands it to the judge, so the tests speak in drivers rather than entities.
"""

from unittest.mock import MagicMock

from never_dry.environment import Reachability
from never_dry.reachability_watch import FleetSilenceWatch


def _driver(silence_s):
    """A stand-in driver that reports a given silence, or None when unreadable."""
    driver = MagicMock()
    driver.silence_s = MagicMock(side_effect=lambda: silence_s() if callable(silence_s) else silence_s)
    return driver


class TestTheFloorIsLearnedFromSilencesThatEnded:
    """Only a silence a device broke by speaking proves that much quiet is fine."""

    def test_a_silence_that_ended_becomes_an_observed_interval(self):
        quiet = {"s": 1800.0}
        watch = FleetSilenceWatch(MagicMock())
        drivers = {"A": _driver(lambda: quiet["s"])}

        watch.observe(drivers)
        assert not watch._watches["A"].ended_silences

        quiet["s"] = 5.0  # the device spoke: silence went backwards
        watch.observe(drivers)

        assert list(watch._watches["A"].ended_silences) == [1800.0]
        assert watch._watches["A"].peak_silence_s == 5.0

    def test_without_any_ended_silence_nothing_is_judged(self):
        """No evidence of normal cadence means no verdict, not a guess."""
        watch = FleetSilenceWatch(MagicMock())
        verdicts = watch.observe({"A": _driver(100.0), "B": _driver(99999.0)})

        assert {v.status for v in verdicts.values()} == {Reachability.UNKNOWN}

    def test_a_driver_that_cannot_be_read_is_left_out(self):
        """Unreadable is not quiet, and must not enter the reference."""
        watch = FleetSilenceWatch(MagicMock())
        verdicts = watch.observe({"A": _driver(None), "B": _driver(60.0)})

        assert "A" not in verdicts


class TestTheFieldCase:
    """Two valves off the mesh, two alive — the shape seen on 2026-08-18."""

    def _fleet(self, quiet_s, live_s):
        return {
            "dead1": _driver(quiet_s),
            "dead2": _driver(quiet_s),
            "live1": _driver(live_s),
            "live2": _driver(live_s),
        }

    def _with_cadence(self, watch, drivers):
        watch.observe(drivers)
        for key in drivers:
            watch._watches[key].ended_silences.extend([60.0, 90.0, 120.0])

    def test_the_quiet_pair_is_flagged_once_cadence_is_known(self):
        watch = FleetSilenceWatch(MagicMock())
        drivers = self._fleet(quiet_s=20 * 3600, live_s=120.0)
        self._with_cadence(watch, drivers)

        verdicts = watch.observe(drivers)

        assert verdicts["dead1"].status == Reachability.SILENT
        assert verdicts["dead2"].status == Reachability.SILENT
        assert verdicts["live1"].status == Reachability.LIVE
        assert verdicts["live2"].status == Reachability.LIVE

    def test_a_fleet_quiet_together_accuses_nobody(self):
        """Night, or a coordinator outage: everyone's silence rises at once."""
        watch = FleetSilenceWatch(MagicMock())
        drivers = self._fleet(quiet_s=8 * 3600, live_s=8 * 3600)
        self._with_cadence(watch, drivers)

        verdicts = watch.observe(drivers)

        assert all(v.status != Reachability.SILENT for v in verdicts.values())


class TestTheDriverSuppliesItsOwnSilence:
    """The measurement belongs to the driver; the judgement to the site."""

    def test_the_watch_asks_every_driver_for_its_silence(self):
        watch = FleetSilenceWatch(MagicMock())
        drivers = {"A": _driver(60.0), "B": _driver(90.0)}

        watch.observe(drivers)

        for driver in drivers.values():
            driver.silence_s.assert_called()
