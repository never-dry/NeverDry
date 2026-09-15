#!/usr/bin/env python3
"""Render the compatibility table in ``docs/valve-compatibility.md`` from the CSV.

``docs/valve-compatibility.csv`` holds **facts only**: what the device exposes, how it
is reached, who established the row. The verdict is *derived* here, every time, from
those facts.

That split is the whole point. A hand-written verdict beside hand-written columns is two
sources of truth with nobody reconciling them: the day a firmware row gains a counter and
the verdict stays where it was, the table starts lying and no test notices. Deriving it
means the two cannot disagree, and it means the rule is written down (below) instead of
living in whoever filled the row.

Run with ``--check`` to verify the document matches the CSV without rewriting it; that is
what the test does.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

_DOCS = Path(__file__).resolve().parent.parent / "docs"
_CSV = _DOCS / "valve-compatibility.csv"
_MD = _DOCS / "valve-compatibility.md"

_BEGIN = "<!-- BEGIN GENERATED TABLE: edit valve-compatibility.csv, then run tools/build_valve_table.py -->"
_END = "<!-- END GENERATED TABLE -->"

# ── The verdict rule ──────────────────────────────────────────────────────
#
# The tier answers one question: **how well does this device serve flow-meter mode?**
# Timer mode needs nothing but a valve entity, so every listed device can do it, which is
# why the bottom tier is "timer-only" and not "bad". A valve that opens and closes reliably
# and reports nothing is not a bad valve; it is a valve NeverDry drives on a clock, which is
# a supported mode, not a degraded one. Calling it "bad" would misinform the person reading
# the table to decide what to buy.
_TIERS = {
    "good": "delivery measurement available with no extra setup",
    "partial": "delivery measurement available, but with a documented caveat or extra step",
    "timer-only": "no delivery measurement, so NeverDry runs it on a clock",
}

# The longest window still worth waiting for before a guard stops being a guard, mirroring
# FLOW_VERIFY_MAX_S in driver.py. A meter may only supervise an opening if a full reporting
# interval, plus the margin the driver applies, fits inside it.
_MAX_USEFUL_WINDOW_S = 180.0
_WINDOW_MARGIN = 1.5


def _guards_openings(row: dict[str, str]) -> bool | None:
    """Whether this meter can tell a dry pipe from a counter that has not spoken yet.

    ``None`` while the cadence is unmeasured, which is not the same as "no": it means
    the quantity that decides has not been established for this firmware.

    Not a column anyone types. A meter's ability to supervise an opening follows from
    how often it reports, and a row where the two disagreed would be the hand-written
    verdict this generator exists to abolish.
    """
    raw = (row.get("meter_update_s") or "").strip().lstrip("~")
    if not raw:
        return None
    try:
        cadence = float(raw)
    except ValueError:
        return None
    return cadence * _WINDOW_MARGIN <= _MAX_USEFUL_WINDOW_S


# -- What a caveat is about ------------------------------------------------
#
# Two families, and the split is not cosmetic. A *meter* caveat qualifies the number a
# device reports, so it says something only on a row that reports one. A *command* caveat
# qualifies what happens when NeverDry asks the valve to open, and that holds whether or
# not anything measures. On a row with no meter it is the only thing left to say, and it is
# also the thing that matters most there: running on a clock is the only mode such a valve
# has, and this is the device taking that clock back.
#
# Both are tokens in the CSV's ``caveat`` column, space-separated when a row carries more
# than one. Naming them here rather than inline is what makes a token nobody implemented
# fail a test instead of evaporating on the way to the page.
_METER_CAVEATS = {
    "unit_change": "the firmware can change its own counter units",
}
_COMMAND_CAVEATS = {
    "device_runtime_cap": (
        "it closes on its own preset runtime, so a run ends when the device decides rather than when NeverDry does"
    ),
}


def _caveat_tokens(row: dict[str, str]) -> list[str]:
    """The caveat column as tokens, in the order the reporter wrote them."""
    return (row.get("caveat") or "").replace(",", " ").split()


def _phrases(tokens: list[str], catalogue: dict[str, str]) -> list[str]:
    """The sentences those tokens stand for, skipping the ones this family does not own."""
    return [catalogue[token] for token in tokens if token in catalogue]


def _reporting(row: dict[str, str]) -> str:
    """How this meter speaks, as one cell: the cadence and what kind of clock it is."""
    cadence = (row.get("meter_update_s") or "").strip()
    kind = (row.get("meter_update_kind") or "").strip()
    step = (row.get("meter_resolution_l") or "").strip()
    if kind == "volume":
        return f"per volume ({step} L steps)" if step else "per volume"
    if kind == "periodic":
        return f"on a clock, ~{cadence} s" if cadence else "on a clock"
    return "-"


def _verdict(row: dict[str, str]) -> tuple[str, str]:
    """Return ``(tier, reason)`` for one CSV row."""
    has_flow = row["flow_rate"] not in ("", "no")
    has_session = row["volume_session"] == "yes"
    has_aggregate = row["volume_aggregate"] not in ("", "no")
    # Read before the tier is decided, and deliberately so. This function used to return on
    # the next line for a row with nothing to measure, above every line that looks at the
    # caveat column -- so a valve that closes on its own preset runtime recorded exactly
    # that in the CSV and the page printed the generic sentence. Nothing failed: the fact
    # was held and not shown, which is the one failure a register cannot afford.
    command_caveats = _phrases(_caveat_tokens(row), _COMMAND_CAVEATS)

    if not (has_flow or has_session or has_aggregate):
        reason = "on/off only, and nothing reports what was delivered"
        if command_caveats:
            reason += "; and " + "; and ".join(command_caveats)
        return "timer-only", reason

    evidence = []
    if has_flow:
        evidence.append(f"flow rate in {row['flow_rate']}")
    if has_session:
        evidence.append("session counter")
    if has_aggregate and not has_session:
        evidence.append(f"{row['volume_aggregate']} counter only")

    # A caveat that touches the delivery measurement is not a footnote: it is the difference
    # between a number you can trust and one you cannot. It costs the row its top tier, and
    # a row can carry more than one, so they accumulate rather than shadow each other.
    caveats = []
    if _guards_openings(row) is False:
        # A meter reporting on a clock delivers its count late and in coarse jumps, so it can
        # neither supervise an opening nor stop a dose precisely. That is what the field
        # failure of 2026-09-08 cost, and a table calling such a row "good" would repeat it.
        cadence = (row.get("meter_update_s") or "").strip()
        caveats.append(
            f"it reports every ~{cadence} s on a clock, too late to supervise an opening, "
            f"and the dose lands in steps that size"
        )
    caveats += _phrases(_caveat_tokens(row), _METER_CAVEATS)
    if not has_session and has_aggregate:
        caveats.append("it is subject to the calendar-reset caveat")
    if row["needs_config"] not in ("", "none") and row["needs_config"] != "history":
        caveats.append("it is reachable only after a documented step")

    # Last, so the meter's own story is told before what the device does to the command.
    caveats += command_caveats

    if caveats:
        return "partial", f"{', '.join(evidence)}, but {'; and '.join(caveats)}"
    return "good", ", ".join(evidence)


_MARK = {"yes": "✅", "no": "❌", "": "-", "on_request": "⚠️ on request", "none": "❌ none"}


def _cell(value: str) -> str:
    return _MARK.get(value, value)


def render() -> str:
    rows = list(csv.DictReader(_CSV.open(encoding="utf-8")))
    out = [
        "| Vendor / model | Firmware | Via | Valve | Flow rate | Volume counters | Meter reports | "
        "History | Needs config? | Verdict | Why | LoD | By |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        tier, reason = _verdict(row)
        counters = []
        if row["volume_session"] == "yes":
            counters.append("session")
        if row["volume_aggregate"] not in ("", "no"):
            counters.append(row["volume_aggregate"])
        datecode = f" ({row['datecode']})" if row["datecode"] else ""
        out.append(
            f"| {row['vendor']} **{row['model']}** "
            f"| {row['firmware']}{datecode} "
            f"| {row['via']} | `{row['valve_domain']}.*` "
            f"| {_cell(row['flow_rate'])} "
            f"| {'✅ ' + ' + '.join(counters) if counters else '❌'} "
            f"| {_reporting(row)} "
            f"| {_cell(row['history'])} | {_cell(row['needs_config'])} "
            f"| **{tier}** | {reason} | {_cell(row['lod'])} | {row['reported_by']} |"
        )
    legend = " · ".join(f"*{k}* = {v}" for k, v in _TIERS.items())
    out += ["", "**Verdict**, derived from the columns, never typed: " + legend]
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify without writing")
    args = parser.parse_args()

    doc = _MD.read_text(encoding="utf-8")
    if _BEGIN not in doc or _END not in doc:
        print(f"markers not found in {_MD.name}", file=sys.stderr)
        return 2
    head, rest = doc.split(_BEGIN, 1)
    _, tail = rest.split(_END, 1)
    rebuilt = f"{head}{_BEGIN}\n{render()}\n{_END}{tail}"

    if args.check:
        if rebuilt != doc:
            print(f"{_MD.name} is out of step with {_CSV.name}: run tools/build_valve_table.py", file=sys.stderr)
            return 1
        return 0
    _MD.write_text(rebuilt, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
