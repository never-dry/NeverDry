"""Per-zone flow rate learned from the sessions the zone already runs.

The declared flow rate is a number somebody typed once; the real one is a
property of pipe, emitters and mains pressure, and two months of field data
say the two drift apart by a factor of 1.75 on a healthy zone. A supervised
test can measure it, but a test is expensive: it spends water to learn what
every irrigation session already demonstrates for free.

So the sample is taken from the session itself — the meter before the valve
opens, the meter again after it closes, the elapsed time in between:

    lpm = (meter_after - meter_before) / session_minutes

Two details carry the accuracy, and both come from the field.

The reading *after* the close is deliberately delayed by ``SETTLE_DELAY_S``.
A Zigbee counter reports on its own cadence, so the last tick of a session
routinely lands after the valve is already shut; sampling at the instant of
closing silently loses it. (The same late tick, read as an instantaneous
rate, is what makes a closed valve look like it is still leaking.)

Sessions shorter than ``MIN_SESSION_S`` are refused rather than averaged in.
On a counter whose smallest step is a whole liter, a short run is mostly
quantization error, and one bad sample is worth less than no sample.

Nothing here changes what the zone delivers. The learned figure is published
for the user to see next to the configured one and to apply deliberately;
this module only remembers what the water did.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

#: How many session samples to keep. Flow genuinely varies with mains
#: pressure, so this is a moving picture of the zone, not a constant.
WINDOW_SIZE: int = 20
#: Below this, report nothing: a median of one or two sessions is an anecdote.
MIN_SAMPLES: int = 3
#: Grace after the valve closes before reading the meter, so a counter that
#: reports late still gets counted. Not part of the measured duration.
SETTLE_DELAY_S: float = 30.0
#: Shorter sessions are dominated by the counter's own resolution.
MIN_SESSION_S: float = 60.0

_STORAGE_VERSION: int = 1


@dataclass
class SessionFlowWindow:
    """Rolling window of flow-rate samples, in liters per minute."""

    _samples: deque[float] = field(default_factory=lambda: deque(maxlen=WINDOW_SIZE), repr=False)

    def record(self, lpm: float) -> None:
        """Add one session's measured flow rate."""
        self._samples.append(lpm)

    @property
    def sample_count(self) -> int:
        return len(self._samples)

    def median_lpm(self) -> float | None:
        """Median of the window, or ``None`` before ``MIN_SAMPLES``.

        Median rather than mean on purpose: a session cut short by a timeout
        or a meter that stalled mid-run lands far from the truth, and the
        median ignores it where an average would follow it.
        """
        n = len(self._samples)
        if n < MIN_SAMPLES:
            return None
        ordered = sorted(self._samples)
        mid = n // 2
        if n % 2:
            return ordered[mid]
        return (ordered[mid - 1] + ordered[mid]) / 2.0

    def as_dict(self) -> dict[str, Any]:
        """Diagnostics view: the median plus the spread it came from."""
        n = len(self._samples)
        if n == 0:
            return {"sample_count": 0, "median_lpm": None}
        ordered = sorted(self._samples)
        return {
            "sample_count": n,
            "median_lpm": round(m, 3) if (m := self.median_lpm()) is not None else None,
            "median_lph": round(m * 60.0, 1) if m is not None else None,
            "min_lpm": round(ordered[0], 3),
            "max_lpm": round(ordered[-1], 3),
            "min_samples_required": MIN_SAMPLES,
        }


class SessionFlowTracker:
    """Persists one zone's learned flow rate and meter resolution.

    The resolution rides along with the flow rate because the two are only
    useful together: ``resolution / flow`` is the time before the counter can
    possibly move, which is what a flow-verification window has to be built
    from and what decides whether a supervised test is even practicable.
    """

    def __init__(self, hass: HomeAssistant, switch_entity_id: str) -> None:
        safe_id = switch_entity_id.replace(".", "_").replace("/", "_")
        self._store: Store = Store(hass, _STORAGE_VERSION, f"never_dry.session_flow.{safe_id}")
        self.window = SessionFlowWindow()
        #: Smallest non-zero counter increment ever seen — the meter's limit of
        #: detection. Learned by watching deliveries, so it needs no test.
        self.resolution_l: float | None = None

    def observe_step(self, step: float) -> bool:
        """Record a counter increment; returns True when it lowers the estimate.

        Takes the minimum rather than an average: a counter that reports 1 L
        sometimes and 3 L other times has a resolution of 1 L and a cadence
        that skips, and the smallest step is the one that bounds detection.
        """
        if step <= 0:
            return False
        if self.resolution_l is None or step < self.resolution_l:
            self.resolution_l = step
            return True
        return False

    async def async_load(self) -> None:
        """Load persisted samples from HA storage."""
        data = await self._store.async_load()
        if not data:
            return
        for s in data.get("samples", []):
            try:
                self.window.record(float(s))
            except (TypeError, ValueError):
                continue
        try:
            if (res := data.get("resolution_l")) is not None:
                self.resolution_l = float(res)
        except (TypeError, ValueError):
            # A corrupt stored resolution is not worth failing a reload over:
            # leaving it unset means the flow-verification window falls back to
            # its conservative default and the next delivery relearns the step.
            pass

    async def async_save(self) -> None:
        """Persist the current window to HA storage."""
        await self._store.async_save({"samples": list(self.window._samples), "resolution_l": self.resolution_l})

    def median_lpm(self) -> float | None:
        return self.window.median_lpm()

    def as_dict(self) -> dict[str, Any]:
        return {**self.window.as_dict(), "meter_resolution_l": self.resolution_l}
