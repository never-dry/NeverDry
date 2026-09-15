"""The compatibility table in the docs must be what the CSV says it is.

``docs/valve-compatibility.csv`` holds the facts a reporter established; the table in
``docs/valve-compatibility.md`` is rendered from it by ``tools/build_valve_table.py``, and
the verdict column is derived rather than typed.

Without this test the arrangement is decoration: someone edits the markdown by hand because
it is right there, the CSV stays behind, and the page a person uses to choose what to buy
stops matching the data the project actually holds. It is the same failure this repository
just found between ``strings.json`` and ``en.json``: two documents, no reconciliation, and
the same remedy.
"""

from __future__ import annotations

import csv
import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_CSV = _ROOT / "docs" / "valve-compatibility.csv"
_MD = _ROOT / "docs" / "valve-compatibility.md"


def _builder():
    """Import ``tools/build_valve_table.py`` without making ``tools`` a package."""
    spec = importlib.util.spec_from_file_location("build_valve_table", _ROOT / "tools" / "build_valve_table.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_rendered_table_matches_the_csv():
    assert _builder().main.__module__  # import guard: a broken generator fails here, not silently
    module = _builder()
    doc = _MD.read_text(encoding="utf-8")
    assert module._BEGIN in doc and module._END in doc, "generated-table markers missing from the document"

    current = doc.split(module._BEGIN, 1)[1].split(module._END, 1)[0].strip()
    assert current == module.render().strip(), (
        "docs/valve-compatibility.md is out of step with valve-compatibility.csv: "
        "edit the CSV and run tools/build_valve_table.py, do not edit the table by hand"
    )


def test_every_row_declares_the_columns_the_verdict_rule_reads():
    """A row missing a field would take a verdict from a default nobody chose."""
    required = {
        "vendor",
        "model",
        "firmware",
        "via",
        "valve_domain",
        "flow_rate",
        "volume_session",
        "volume_aggregate",
        "history",
        "needs_config",
        "caveat",
        "meter_update_s",
        "meter_update_kind",
        "reported_by",
    }
    rows = list(csv.DictReader(_CSV.open(encoding="utf-8")))
    assert rows, "the compatibility CSV is empty"

    problems: list[str] = []
    for index, row in enumerate(rows, start=2):
        missing = required - set(row)
        if missing:
            problems.append(f"line {index}: columns absent: {sorted(missing)}")
        for field in ("vendor", "model", "firmware", "via", "valve_domain", "reported_by"):
            if not (row.get(field) or "").strip():
                problems.append(f"line {index}: '{field}' is empty, and it is not optional")
        if row.get("valve_domain") not in ("switch", "valve"):
            problems.append(f"line {index}: valve_domain '{row.get('valve_domain')}' is neither switch nor valve")
    assert not problems, "malformed rows in valve-compatibility.csv:\n  " + "\n  ".join(problems)


def test_a_row_with_no_measurement_is_timer_only_not_bad():
    """The bottom tier says what NeverDry does with the device, not that it is poor.

    An on/off valve is driven on a clock, which is a supported mode. A table that called it
    "bad" would be telling someone not to buy hardware that works.
    """
    module = _builder()
    barebones = {
        "flow_rate": "no",
        "volume_session": "no",
        "volume_aggregate": "",
        "history": "no",
        "needs_config": "none",
        "caveat": "",
    }
    tier, reason = module._verdict(barebones)
    assert tier == "timer-only"
    assert "clock" in reason or "delivered" in reason


def test_a_meter_that_speaks_on_a_clock_cannot_be_top_tier():
    """The row that caused the field failure of 2026-09-08 must not read "good".

    A SONOFF SWV-ZFE reports every 300 s whatever the flow. Everything else about it
    looks excellent on this table -- a session counter, an hourly total -- and on those
    columns alone the old rule rated it top tier. Someone choosing hardware from that
    row would buy a valve NeverDry cannot supervise, which is the failure this page
    exists to prevent.
    """
    module = _builder()
    on_a_clock = {
        "flow_rate": "no",
        "volume_session": "yes",
        "volume_aggregate": "hourly",
        "history": "on_request",
        "needs_config": "history",
        "caveat": "",
        "meter_update_s": "300",
        "meter_update_kind": "periodic",
    }
    tier, reason = module._verdict(on_a_clock)
    assert tier == "partial"
    assert "300" in reason, "the reason must name the cadence that caused it"

    prompt = {**on_a_clock, "meter_update_s": "14", "meter_update_kind": "volume"}
    assert module._verdict(prompt)[0] == "good", "a prompt meter must not be dragged down with it"


def test_an_unmeasured_cadence_is_not_read_as_a_fast_one():
    """Unknown is not a synonym for fine, and it must not silently become one."""
    module = _builder()
    assert module._guards_openings({"meter_update_s": ""}) is None
    assert module._guards_openings({"meter_update_s": "300"}) is False
    assert module._guards_openings({"meter_update_s": "14"}) is True


def test_a_caveat_recorded_in_the_csv_always_reaches_the_reader():
    """A fact held and not shown is the one failure a register cannot afford.

    The verdict rule used to return for a row with nothing to measure *before* it had
    looked at the caveat column. So a valve that closes on its own preset runtime could
    record exactly that, the CSV would hold it, and the page would print the generic
    sentence instead. Nothing failed, because nothing compared the two. This does, for
    every row and every tier, and it also catches the quieter version of the same bug: a
    token misspelt in the CSV, which today would simply do nothing.
    """
    module = _builder()
    problems: list[str] = []
    for index, row in enumerate(csv.DictReader(_CSV.open(encoding="utf-8")), start=2):
        tokens = module._caveat_tokens(row)
        if not tokens:
            continue
        tier, reason = module._verdict(row)
        for token in tokens:
            if token in module._COMMAND_CAVEATS:
                if module._COMMAND_CAVEATS[token] not in reason:
                    problems.append(f"line {index}: caveat '{token}' is recorded and never rendered")
            elif token in module._METER_CAVEATS:
                if tier == "timer-only":
                    problems.append(f"line {index}: caveat '{token}' describes a meter, and this row has none")
                elif module._METER_CAVEATS[token] not in reason:
                    problems.append(f"line {index}: caveat '{token}' is recorded and never rendered")
            else:
                problems.append(f"line {index}: caveat '{token}' is a token no rule implements")
    assert not problems, "caveats that never reach the page:\n  " + "\n  ".join(problems)


def test_a_device_that_caps_its_own_run_says_so_whatever_it_measures():
    """The cap is about the command, so it survives the tier, including the bottom one.

    A valve NeverDry drives on a clock, on hardware that takes that clock back after five
    minutes, is the case this column exists for: the tier stays 'timer-only' -- the cap is
    not a metering fault and must not be read as one -- while the reason has to name it.
    """
    module = _builder()
    no_meter = {
        "flow_rate": "no",
        "volume_session": "no",
        "volume_aggregate": "no",
        "history": "no",
        "needs_config": "none",
        "caveat": "device_runtime_cap",
    }
    tier, reason = module._verdict(no_meter)
    assert tier == "timer-only", "a runtime cap is not a metering fault and must not change the tier"
    assert "preset runtime" in reason

    with_meter = {
        **no_meter,
        "flow_rate": "m3/h",
        "volume_session": "yes",
        "meter_update_s": "14",
        "meter_update_kind": "volume",
    }
    tier, reason = module._verdict(with_meter)
    assert tier == "partial", "a device that ends the run early cannot be top tier"
    assert "preset runtime" in reason
