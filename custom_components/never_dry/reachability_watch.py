"""Collecting the silence that ``environment.judge_silence`` knows how to judge.

The criterion has been written and tested for a while; what was missing is the
measurement it consumes. This module is that half, and nothing more: it gathers
how long each valve has been quiet and hands the fleet to the judge.

Two decisions carry it, both from ``docs/design/valve-reachability.md``.

**Each number comes from its own driver.** ``Driver.silence_s()`` answers how
long its actuator's device has been quiet — measured over the union of that
device's entities, because a valve entity nobody flips is silent for days while
its device chats on. The driver is the only layer that knows which entities back
its actuator, so the measurement is its to supply; this module only collects the
fleet and hands it to the judge, because no valve can judge itself.

**Silence that ended is what normal looks like.** The floor comes from observed
cadence, and the only honest sample of "this much quiet happened and was fine"
is a silence that a device broke by speaking again. So each tick records how
long a device had been quiet, and when it finally reports, that peak becomes an
interval the floor can be built from. Nothing is assumed about the mesh's
rhythm; it is learned from the mesh.

``last_reported`` is the raw material because it is the one signal present on
every entity of every integration with nothing to enable — it moves on every
state write, whether or not the value changed.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from homeassistant.core import HomeAssistant

from .environment import Reachability, SilenceVerdict, judge_fleet, silence_floor

#: How many ended silences to keep per device for deriving the floor. The floor
#: is a high quantile, so it needs enough samples to have a tail at all.
INTERVAL_HISTORY: int = 50


@dataclass
class _DeviceWatch:
    """What we remember about one valve's device between ticks."""

    #: Silence seen at the previous tick, to notice when it goes backwards.
    previous_silence_s: float = 0.0
    #: Longest silence observed in the current quiet stretch.
    peak_silence_s: float = 0.0
    #: Silences that ended — the fleet's observed cadence.
    ended_silences: deque[float] = field(default_factory=lambda: deque(maxlen=INTERVAL_HISTORY))


class FleetSilenceWatch:
    """Measures per-valve silence across a fleet and judges it as a whole.

    Judging is delegated to :func:`environment.judge_fleet`, which leaves each
    valve out of its own reference. This class only answers "how long has each
    one been quiet", which is the part that needs Home Assistant.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._watches: dict[str, _DeviceWatch] = {}

    def observe(self, drivers: dict[str, object]) -> dict[str, SilenceVerdict]:
        """Take one reading of the fleet and judge it.

        ``drivers`` maps a key (the zone name) to its driver. Keys whose silence
        cannot be read are omitted rather than guessed at.
        """
        silences: dict[str, float] = {}
        intervals: list[float] = []

        for key, driver in drivers.items():
            silence = driver.silence_s()
            if silence is None:
                continue
            watch = self._watches.setdefault(key, _DeviceWatch())
            if silence < watch.previous_silence_s:
                # Silence went backwards: the device spoke since the last tick,
                # so the stretch that just ended is a sample of quiet that turned
                # out to be fine — the only honest evidence of normal cadence.
                if watch.peak_silence_s > 0:
                    watch.ended_silences.append(watch.peak_silence_s)
                watch.peak_silence_s = 0.0
            watch.previous_silence_s = silence
            watch.peak_silence_s = max(watch.peak_silence_s, silence)
            silences[key] = silence
            intervals.extend(watch.ended_silences)

        if not silences:
            return {}

        floor = silence_floor(intervals)
        if floor is None:
            # Nothing has ever been observed to end, so there is no evidence of
            # what "normally quiet" looks like. Judging now would be guessing.
            return {key: SilenceVerdict(Reachability.UNKNOWN, s) for key, s in silences.items()}

        return judge_fleet(silences, floor_s=floor)

    def diagnostics(self) -> dict:
        """What the watch has learned, for the diagnostics bundle."""
        return {
            key: {
                "silence_samples": len(w.ended_silences),
                "peak_silence_s": round(w.peak_silence_s, 1),
            }
            for key, w in self._watches.items()
        }
