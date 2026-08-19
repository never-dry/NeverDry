#!/usr/bin/env python3
"""Print what your Zigbee2MQTT valves expose, ready to paste into a valve report.

Standalone on purpose: it imports nothing from NeverDry and is useful whether or
not you run the integration. It reads one thing — Zigbee2MQTT's own device list,
published on ``zigbee2mqtt/bridge/devices`` — and reports what it finds.

Two ways to run it:

    # ask Home Assistant (needs `pip install websockets`)
    python3 tools/valve_report.py --ha-url http://homeassistant.local:8123 --token <long-lived token>

    # or, with no dependencies at all, from a saved payload
    #   copy the `zigbee2mqtt/bridge/devices` message out of MQTT Explorer / mosquitto_sub
    python3 tools/valve_report.py --from-file bridge_devices.json

Filter to a subset with ``--match giardino``. Your token is used for one
WebSocket call and is never written anywhere.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

TOPIC = "zigbee2mqtt/bridge/devices"

#: Z2M access bits. Only 1 (published in the state payload) makes a plain
#: readable HA entity; a value that lacks it has to be asked for with a `/get`.
ACCESS_PUBLISHED = 0b001
ACCESS_SET = 0b010
ACCESS_GET = 0b100

VOLUME_HINTS = ("volume", "flow", "irrigation_amount", "capacity")


def flatten(exposes: list[dict], depth: int = 0) -> list[tuple[int, dict]]:
    """Walk the exposes tree, keeping depth so nesting stays visible."""
    out: list[tuple[int, dict]] = []
    for entry in exposes or []:
        out.append((depth, entry))
        if entry.get("features"):
            out.extend(flatten(entry["features"], depth + 1))
        item = entry.get("item_type")
        if isinstance(item, dict):
            out.append((depth + 1, item))
            out.extend(flatten(item.get("features") or [], depth + 2))
    return out


def describe(device: dict) -> str:
    definition = device.get("definition") or {}
    exposes = definition.get("exposes") or []
    flat = flatten(exposes)

    lines = [
        f"### {definition.get('vendor') or device.get('manufacturer')} "
        f"{definition.get('model') or device.get('model_id')}",
        "",
        f"- friendly name : {device.get('friendly_name')}",
        f"- model id      : {device.get('model_id')}",
        f"- firmware      : {device.get('software_build_id')}",
        f"- date code     : {device.get('date_code')}",
        f"- power source  : {device.get('power_source')}",
        f"- via           : Zigbee2MQTT (supported={device.get('supported')})",
        "",
    ]

    def is_measurement(entry: dict) -> bool:
        return entry.get("type") == "numeric" and any(h in (entry.get("name") or "") for h in VOLUME_HINTS)

    # Depth is not decoration: only a top-level expose becomes an entity you can
    # pick in a form. The same name nested inside a composite is a *field of a
    # command*, and reporting it as a measurement would put rows in the
    # compatibility table for numbers nobody can read.
    top_level = [e for depth, e in flat if depth == 0 and is_measurement(e)]
    nested = [e for depth, e in flat if depth > 0 and is_measurement(e)]

    if top_level:
        lines.append("Volume / flow measurements (selectable entities):")
        for e in top_level:
            access = e.get("access") or 0
            note = "flat HA entity" if access & ACCESS_PUBLISHED else "NOT published — needs a /get"
            if access & ACCESS_GET and access & ACCESS_PUBLISHED:
                note += ", refresh with /get"
            lines.append(f"  - {e.get('name')} [{e.get('unit') or 'no unit'}] access={access} → {note}")
    else:
        lines.append("Volume / flow measurements: NONE — this valve only opens and closes.")
    lines.append("")

    if nested:
        lines.append("Volume-ish fields buried inside composites — NOT entities, ignore when reporting:")
        for e in nested:
            lines.append(f"  - {e.get('name')} [{e.get('unit') or 'no unit'}]")
        lines.append("")

    composites = [
        e.get("name") or e.get("property") for depth, e in flat if e.get("type") in ("composite", "list") and depth == 0
    ]
    if composites:
        lines.append("Composites (these never become plain HA entities — commanded via MQTT):")
        for name in composites:
            lines.append(f"  - {name}")
        lines.append("")

    blobs = [e.get("name") for depth, e in flat if e.get("type") == "text" and depth == 0]
    if blobs:
        lines.append("Text blobs at top level (often on-device history):")
        for name in blobs:
            lines.append(f"  - {name}")
        lines.append("")

    settable_unit = [
        e for _d, e in flat if (e.get("name") or "").endswith("unit") and (e.get("access") or 0) & ACCESS_SET
    ]
    for entry in settable_unit:
        lines.append(
            f"⚠️  `{entry.get('name')}` is SETTABLE (options: {entry.get('values')}) — "
            "this device can change the unit of its own readings. Note which unit is active."
        )
    if settable_unit:
        lines.append("")

    return "\n".join(lines)


async def fetch_from_ha(ha_url: str, token: str) -> list[dict]:
    try:
        import websockets
    except ImportError:
        sys.exit("This mode needs `pip install websockets`. Or use --from-file (no dependencies).")

    url = ha_url.rstrip("/").replace("https://", "wss://").replace("http://", "ws://") + "/api/websocket"
    async with websockets.connect(url, max_size=32_000_000) as ws:
        await ws.recv()  # auth_required
        await ws.send(json.dumps({"type": "auth", "access_token": token}))
        if json.loads(await ws.recv()).get("type") != "auth_ok":
            sys.exit("Home Assistant rejected the token.")
        await ws.send(json.dumps({"id": 1, "type": "mqtt/subscribe", "topic": TOPIC}))
        while True:
            message = json.loads(await asyncio.wait_for(ws.recv(), timeout=45))
            if message.get("type") == "event" and message["event"].get("topic", "").endswith("bridge/devices"):
                return json.loads(message["event"]["payload"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ha-url", help="e.g. http://homeassistant.local:8123")
    parser.add_argument("--token", help="a long-lived access token")
    parser.add_argument("--from-file", help=f"a saved `{TOPIC}` payload")
    parser.add_argument("--match", default="", help="only devices whose friendly name contains this")
    args = parser.parse_args()

    if args.from_file:
        with open(args.from_file, encoding="utf-8") as handle:
            devices = json.load(handle)
    elif args.ha_url and args.token:
        devices = asyncio.run(fetch_from_ha(args.ha_url, args.token))
    else:
        # `parser.error` exits, but saying so keeps both the reader and the
        # static analyser from wondering whether `devices` can be unbound.
        parser.error("give either --from-file, or both --ha-url and --token")
        return

    reported = 0
    for device in devices:
        if device.get("type") == "Coordinator":
            continue
        name = device.get("friendly_name") or ""
        if args.match and args.match.lower() not in name.lower():
            continue
        exposes = (device.get("definition") or {}).get("exposes") or []
        flat = flatten(exposes)
        # A valve is anything that can be switched. Cheap heuristic, and if it
        # over-matches you simply get an extra block you can delete.
        if not any(e.get("type") in ("switch", "valve") or e.get("name") == "state" for _d, e in flat):
            continue
        print(describe(device))
        reported += 1

    if not reported:
        print("No switchable device found. Try --match, or check you are on the right bridge.", file=sys.stderr)
        sys.exit(1)
    print("---")
    print("Paste the above into a valve report: https://github.com/never-dry/NeverDry/issues/new/choose")
    print("What this cannot tell you is the limit of detection — that one has to be measured.")


if __name__ == "__main__":
    main()
