"""The supervised one-minute valve test: measure instead of asking.

Every number this produces exists because its declared counterpart turned out to
be wrong on a real garden. Two zones on the same installation were both declared
at 100 L/h; measured, one ran at 264 L/h and the other at 24 L/h — an elevenfold
spread between values a user had typed in good faith. Nothing in the software
could notice, because in the estimated mode the loop is closed on itself: the
duration comes from the declared rate and the credited volume is that same rate
times the elapsed time, so no contradiction can ever arise.

Hence the one rule this module must never break:

    **The test uses no configured flow rate in any of its own arithmetic.**

It exists to discover that the declared number is wrong. A test that leant on it
would confirm its own assumption, and would have found nothing on either of the
two zones above.

What it can answer depends on the hardware, so it answers in tiers:

* with a volume counter — delivered volume, real flow, the counter's resolution
  and how often it reports (that pair is the limit of detection);
* with only a flow-rate sensor — real flow and cadence; resolution unknowable;
* with neither — open and close latency, and whether the valve answered at all.

Deliberately *supervised*: it puts water on the ground. It refuses to run while
anything else is irrigating, and it closes the valve in a ``finally`` so that an
exception, a cancellation or a reload cannot leave it open.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from homeassistant.core import HomeAssistant

from . import flow_utils

_LOGGER = logging.getLogger(__name__)


class _AbortRun(Exception):
    """Internal: stop the run early. The ``finally`` still closes the valve."""


#: Default supervised duration. A minute was the original choice, on the
#: assumption that a coarse counter steps at least once in that time. The field
#: refuted it: on three of four zones a one-minute run recorded ``updates=0``,
#: and on the fourth a single 28 L step produced a rate that was an artifact of
#: the counter's resolution. Five minutes is the shortest run that describes a
#: slow zone; the user can still ask for less and is told what it costs.
DEFAULT_TEST_DURATION_S = 300

#: How often the meter is read during the run. Not a device poll — just a look at
#: whatever state the integration last published, so it costs nothing on the radio.
SAMPLE_INTERVAL_S = 2.0

#: How long to wait for the valve to confirm it moved before giving up on the
#: latency figure. The run continues either way: an unconfirmed open is itself a
#: result worth reporting.
CONFIRM_TIMEOUT_S = 20.0


@dataclass
class ValveTestResult:
    """What one supervised run established. Absent fields mean *not measurable*.

    Nothing here is inferred: every field is either a clock reading, a sensor
    reading, or arithmetic over the two. `None` is used rather than a plausible
    default, because a plausible default is exactly how a declared number
    becomes indistinguishable from a measured one.
    """

    zone_name: str
    duration_s: float
    #: Wall-clock seconds from the command to the entity reporting open/closed.
    open_confirm_s: float | None = None
    close_confirm_s: float | None = None
    #: The entity that was read, and what shape it turned out to be.
    meter_entity: str | None = None
    meter_is_rate: bool | None = None
    meter_unit: str | None = None
    #: Volume, in litres, from the counter alone — never from a configured rate.
    volume_l: float | None = None
    #: Litres per minute implied by that volume over the measured elapsed time.
    measured_lpm: float | None = None
    #: The smallest change ever observed. With the cadence below, this is the LoD.
    smallest_step: float | None = None
    #: How many times the reading changed during the run.
    updates: int = 0
    #: Every distinct reading, as (seconds from open, value) — the raw material.
    samples: list[tuple[float, float]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def measured_lph(self) -> float | None:
        """Litres per hour, which is the unit the zone form speaks."""
        return None if self.measured_lpm is None else round(self.measured_lpm * 60.0, 1)

    def as_dict(self) -> dict[str, Any]:
        """Flat form for entity attributes and for the report block users paste."""
        return {
            "zone_name": self.zone_name,
            "duration_s": round(self.duration_s, 1),
            "open_confirm_s": None if self.open_confirm_s is None else round(self.open_confirm_s, 2),
            "close_confirm_s": None if self.close_confirm_s is None else round(self.close_confirm_s, 2),
            "meter_entity": self.meter_entity,
            "meter_is_rate": self.meter_is_rate,
            "meter_unit": self.meter_unit,
            "volume_l": None if self.volume_l is None else round(self.volume_l, 2),
            "measured_lpm": None if self.measured_lpm is None else round(self.measured_lpm, 3),
            "measured_lph": self.measured_lph,
            "smallest_step": self.smallest_step,
            "updates": self.updates,
            "notes": self.notes,
        }


async def run_valve_test(
    hass: HomeAssistant,
    *,
    zone_name: str,
    valve_entity: str,
    meter_entity: str | None,
    duration_s: float = DEFAULT_TEST_DURATION_S,
    open_valve,
    close_valve,
    read_level,
) -> ValveTestResult:
    """Open a valve for ``duration_s``, watch what moves, and close it.

    The three callables are injected rather than imported so that this module
    names no valve service and no entity domain: that knowledge lives in one
    place (``driver.ValveCommandAdapter``) and a second copy here would be the
    defect that shipped `valve.*` support half-working. They are also what makes
    the whole routine testable without a valve.
    """
    result = ValveTestResult(zone_name=zone_name, duration_s=duration_s)

    is_rate = flow_utils.is_flow_rate_sensor(hass, meter_entity) if meter_entity else None
    if meter_entity:
        result.meter_entity = meter_entity
        result.meter_is_rate = is_rate
        result.meter_unit = flow_utils.get_flow_meter_unit(hass, meter_entity)

    def read() -> float | None:
        if not meter_entity:
            return None
        if is_rate:
            return flow_utils.read_flow_rate_lpm(hass, meter_entity)
        return flow_utils.read_volume_liters(hass, meter_entity)

    # Pre-flight. Water must not run when nothing can be observed: the first live
    # failure of this feature opened a valve for a full minute right after a
    # restart, when neither the valve nor the meter had reported yet — so the
    # command was lost, no water moved, and nothing was learned. The refusal
    # below is cheaper than the minute, and far cheaper than a minute that DOES
    # water while blind.
    level = read_level()
    if level != "off":
        result.notes.append(
            f"refused: the valve reads '{level}', not a confirmed closed state. It has probably not "
            "reported since startup — wait for it to speak, or open and close it once by hand, then retry"
        )
        return result

    baseline = read()
    if meter_entity and baseline is None:
        result.notes.append(
            "refused: the meter has no reading yet, so a volume could not be measured. Wait for the "
            "meter to report and retry — running now would put water down and learn nothing"
        )
        return result

    started = time.monotonic()
    # Initialised before the try so that an early abort cannot leave the
    # arithmetic below reading a variable that was never assigned — the first
    # version did exactly that, and only the abort path reached it.
    last = baseline
    measured_elapsed = 0.0
    aborted = False
    try:
        await open_valve()
        deadline = started + CONFIRM_TIMEOUT_S
        while time.monotonic() < deadline:
            if read_level() == "on":
                result.open_confirm_s = time.monotonic() - started
                break
            await asyncio.sleep(0.5)
        if result.open_confirm_s is None:
            # Abort rather than wait out the duration. An unconfirmed open is
            # usually a command that never reached the valve, and there is nothing
            # to measure — but it IS a result: it says the radio lost a command,
            # which is exactly what a supervised test should surface.
            result.notes.append(
                f"valve never confirmed open within {CONFIRM_TIMEOUT_S:.0f}s — the command was probably "
                "lost on the radio. Aborted: nothing could be measured"
            )
            raise _AbortRun

        opened_at = time.monotonic()
        while True:
            elapsed = time.monotonic() - opened_at
            if elapsed >= duration_s:
                break
            await asyncio.sleep(min(SAMPLE_INTERVAL_S, duration_s - elapsed))
            value = read()
            if value is None or value == last:
                continue
            result.samples.append((round(time.monotonic() - opened_at, 1), value))
            if last is not None:
                step = abs(round(value - last, 6))
                if step > 0 and (result.smallest_step is None or step < result.smallest_step):
                    result.smallest_step = step
            result.updates += 1
            last = value

        measured_elapsed = time.monotonic() - opened_at
    except _AbortRun:
        aborted = True
    finally:
        # Unconditional. An exception, a cancellation or a reload must not be able
        # to leave water running: this is the one line that makes the feature safe
        # enough to exist.
        close_started = time.monotonic()
        try:
            await close_valve()
            deadline = close_started + CONFIRM_TIMEOUT_S
            while time.monotonic() < deadline:
                if read_level() == "off":
                    result.close_confirm_s = time.monotonic() - close_started
                    break
                await asyncio.sleep(0.5)
            if result.close_confirm_s is None:
                result.notes.append(f"valve never confirmed closed within {CONFIRM_TIMEOUT_S:.0f}s")
        except Exception:
            _LOGGER.exception("Zone '%s': the valve test could not close the valve", zone_name)
            result.notes.append("CLOSE FAILED — check the valve immediately")

    if aborted:
        # Nothing was measured, so nothing is computed. The first version fell
        # through to the arithmetic below and produced `volume_l = 0.0` — a
        # *measurement of zero litres* where there had been no measurement at all,
        # breaking the rule this module states in its own docstring. Absence is
        # not zero, and a zero here would also have dragged in a bogus
        # "below the limit of detection" note.
        result.notes.append(
            "no retry was attempted: a real irrigation retries a lost command several times, "
            "so this says the first attempt was lost, not that the valve is broken"
        )
        return result

    if meter_entity and baseline is not None and last is not None:
        if is_rate:
            # A rate integrated over the run. Its accuracy is set by how often the
            # device reported, which is why `updates` is published beside it.
            mean_lpm = sum(v for _t, v in result.samples) / len(result.samples) if result.samples else last
            result.measured_lpm = round(mean_lpm, 3)
            result.volume_l = round(mean_lpm * measured_elapsed / 60.0, 2)
            result.notes.append("volume integrated from a rate — accuracy limited by reporting cadence")
        else:
            delta = last - baseline
            if delta < 0:
                # A counter that reset mid-run (midnight, hourly, per-session).
                # A decrease is never delivery, so the run measured nothing.
                result.notes.append("counter decreased during the run — it reset; volume not measurable")
            else:
                result.volume_l = round(delta, 2)
                if measured_elapsed > 0:
                    result.measured_lpm = round(delta / (measured_elapsed / 60.0), 3)

    if result.updates <= 1 and result.volume_l is not None:
        result.notes.append(
            "the meter changed at most once — this installation cannot describe a run this short "
            "(the reading is below its limit of detection)"
        )
    return result
