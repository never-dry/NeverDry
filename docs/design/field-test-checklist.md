# Field-Test Checklist — Valve Robustness Refactor

*Valve robustness refactor cluster (incl. volume_preset fix). Date prepared: 2026-05-23.*

This checklist is for **a human operator on a real installation**.
Each step is a discrete observable behaviour: tick the box, record
the outcome and (when something goes wrong) attach the diagnostic
bundle described at the bottom of the page.

## What changed (operator-facing summary)

| Change | What you will notice |
|---|---|
| Per-valve finite-state machine (FSM) | Internal — no UI change |
| `ValveOperator` wraps every switch call | Open/close failures now produce a *persistent notification* in HA, deduplicated per valve and per kind |
| Unified notifier (`ValveNotifier`) | Notification IDs follow ``never_dry_<zone>_<kind>`` — easy to find / filter |
| `volume_preset` bug fix | Smart valves that previously **did not open** when you pressed *irrigate* now open and dispense the configured volume |
| Manual valve listener (race) | No more spurious "manual irrigation detected" right after a controller-driven cycle |
| Emergency stop | Closes every valve **in parallel** (was sequential) — faster when you have multiple valves |
| Partial irrigation `last_irrigated` fix | The UI now updates the *last irrigated* timestamp even when delivery was partial (e.g. timeout reached before target volume) |
| Leak recovery + escalation | On `CLOSE_LEAK` the operator now re-issues a direct `switch.turn_off` and re-checks the flow. If the leak persists it calls `never_dry.stop` (closing every other valve) and raises a CRITICAL persistent notification telling you to shut off the mains water valve. |
| Real-time deficit update (flow-metered modes) | While `flow_meter`/`flow_rate` deliveries are running, the zone's `deficit_mm` now drops live every 2 s (poll interval). On a crash mid-cycle the partial progress is preserved (before this change a crash lost every mm we had already delivered). The end-of-cycle settle is snapshot-based and idempotent with the intermediate writes (no double-counting). |

## Quick sanity check — automated smoke tests

Before running the manual steps below, run the automated smoke tests against
the live installation to verify the integration loads and the core services
respond correctly:

```bash
cd sw_artifacts
python tests/e2e/smoke.py --no-valves
```

All 7 tests must pass. If any fail, fix the issue before proceeding.
See `sw_artifacts/docs/developer_manual.md §7.1` for full setup instructions.

---

## Setup before the test

- [ ] HACS-install NeverDry from the branch that contains the new
      `valve_operator.py`, `valve_fsm.py`, `valve_notifier.py` files.
- [ ] Restart Home Assistant.
- [ ] Confirm the integration is loaded: **Settings → Devices & Services
      → NeverDry → "1 service" badge** is visible.
- [ ] Confirm every zone you intend to irrigate has its
      *delivery mode* set (estimated_flow / flow_meter / volume_preset).
- [ ] For `volume_preset` zones: confirm the *volume entity*
      (`number.<valve>_volume` or equivalent) is reachable from
      Developer Tools → States.
- [ ] Open *Developer Tools → Logbook* in another tab — easier to
      follow what's happening live.

## Test A — `estimated_flow` zone (the simple path)

Pre-conditions: one zone configured with `delivery_mode = estimated_flow`
and a deficit > threshold (or force it via a dummy `mark_irrigated`
then a long enough wait, or set the deficit manually in the developer
panel).

- [ ] **A.1** — Call `never_dry.irrigate_zone` with the zone name.
- [ ] **A.2** — Within 1–2 s the valve switch turns *on* in HA.
- [ ] **A.3** — At the end of the calculated duration the valve turns
      *off* automatically.
- [ ] **A.4** — Zone sensor `deficit_mm` drops to 0 (or close to it).
- [ ] **A.5** — `last_irrigated` attribute updates to the time of the
      run.
- [ ] **A.6** — **No persistent notifications** were created.

If any step fails: stop, grab the log bundle (see bottom), open a
GitHub issue with the bundle attached and the step number in the
title.

## Test B — `flow_meter` zone with a real flow sensor

Pre-conditions: one zone with `delivery_mode = flow_meter` and a
working flow sensor entity (cumulative L or L/min — both are fine).

- [ ] **B.1** — Trigger irrigation.
- [ ] **B.2** — Valve opens, flow sensor begins to climb.
- [ ] **B.3** — When the delivered volume reaches the target the
      valve closes by itself.
- [ ] **B.4** — `last_volume_delivered` ≈ target volume (within a
      few percent).
- [ ] **B.5** — `last_irrigated` updated.

**Edge case to provoke**: physically close the upstream water tap
just before triggering. Expected behaviour:
- [ ] **B.6** — The valve opens (switch state goes on)…
- [ ] **B.7** — …but the flow stays at 0 → after the flow-verify
      timeout (10 s default) the operator switches the valve off and
      records `ACTUATION_FAILED`.
- [ ] **B.8** — A persistent notification appears:
      title ≈ *"\[WARNING\] Valve command failed"*; message names the
      zone and `error_detail=actuation_failed`.
- [ ] **B.9** — Re-open the water tap. The next irrigation succeeds
      and the notification clears the next time the operator emits
      a non-failure update for that zone.

## Test C — `volume_preset` zone (the bug fix)

This is the headline of the cluster. Pre-conditions: a Sonoff SWV (or
similar) configured with `delivery_mode = volume_preset` and the
corresponding `volume_entity` (the `number.*` exposed by ZHA).

- [ ] **C.1** — Trigger irrigation. Within the first 3 s:
       - the volume number entity should update to the calculated
         target;
       - the valve switch may either turn *on* by itself (firmware with
         dose-and-go) *or* remain *off* until the grace window expires.
- [ ] **C.2** — After ≤ 3 s the valve **is open** (state = `on`).
      If you had to wait for the grace window, look in the log for
      *"smart valve did not auto-open within 3.0s, sending switch.turn_on"*.
- [ ] **C.3** — Water flows for the duration needed to deliver the
      configured volume.
- [ ] **C.4** — Valve closes itself (state = `off`).
- [ ] **C.5** — `last_irrigated` and `last_volume_delivered` update.

**Failure-mode probe**: temporarily disable the valve in ZHA (mark it
unavailable). Then trigger:
- [ ] **C.6** — A *"\[CRITICAL\] Zone disabled"* persistent
      notification appears after the third consecutive failure, or
      *"Valve command failed"* appears before that.

## Test D — Emergency stop with multiple zones

Pre-conditions: at least two zones with valves, both with a positive
deficit.

- [ ] **D.1** — Call `never_dry.irrigate_all` (or trigger from the
      button entity).
- [ ] **D.2** — While the first zone is being irrigated, call
      `never_dry.stop`.
- [ ] **D.3** — Both valves transition to *off* within 2 s — note
      that they should close roughly **simultaneously**, not one
      after the other (parallel emergency-stop).
- [ ] **D.4** — `is_running` returns to `False` (visible in the
      integration's diagnostic entities or via developer tools).
- [ ] **D.5** — No partial delivery is registered for the
      zones that hadn't started.

## Test E — Manual valve operation (no race)

Pre-conditions: a valve with a flow meter, no irrigation currently
running.

- [ ] **E.1** — Open the valve **manually** from HA (e.g. from the
      switch domain).
- [ ] **E.2** — Let it run for ~30 s, then close it manually.
- [ ] **E.3** — Within seconds you should see an
      `EVENT_IRRIGATION_COMPLETE` in the event bus and the zone's
      `deficit_mm` should drop by the measured volume.
- [ ] **E.4** — `last_irrigation_source = "manual"`.

**Race check**: now trigger an *automatic* irrigation. While it's
running:
- [ ] **E.5** — In the **last 2 seconds** of the cycle (when the
      controller has set `_running=False` but the valve is still
      finishing the close), **no** manual-irrigation event is fired
      spuriously. (Before the fix this could falsely log a manual
      irrigation.) Confirm in Logbook that the only
      `EVENT_IRRIGATION_COMPLETE` for that zone has
      `source = "automatic"`.

## Test F — Partial irrigation `last_irrigated` fix

Pre-conditions: a zone with `delivery_mode = flow_meter` and a flow
sensor that delivers more slowly than the configured *delivery
timeout* would allow for the full target. Easiest reproduction: set a
very short `delivery_timeout` (e.g. 15 s) for the zone, then trigger.

- [ ] **F.1** — Trigger irrigation.
- [ ] **F.2** — The cycle times out before reaching the full volume
      → a *partial* delivery is recorded in the log.
- [ ] **F.3** — **The zone's `last_irrigated` attribute updates** to
      the time of the partial delivery (this was the regression).
- [ ] **F.4** — `last_volume_delivered` matches what the flow meter
      reported.

## Test H — CLOSE_LEAK recovery and escalation *(critical path)*

**This is the most important test in the cluster.** It exercises the
"valve stuck open" failure mode, which is the one that can flood the
garden if the integration does not react correctly.

### H.1 — Recovery succeeds (simulated transient leak)

Pre-conditions: a zone with a flow meter. The flow meter must be
readable through HA (Developer Tools → States gives a number).

Setup: arrange a controllable "physical leak" you can clear quickly.
Easiest approximation if you cannot induce a real leak: manually
pre-set the flow sensor's value via a `homeassistant.set_state` call
right before the operator polls.

- [ ] **H.1.a** — Run a normal irrigation cycle until close. While the
      close cycle is verifying flow→0, **force the flow sensor to
      report a non-zero value** for ~10 s (set state via developer
      tools, or open a parallel water tap on the same flow meter).
- [ ] **H.1.b** — In the HA log you should see:
      *"Valve '\<zone\>' CLOSE_LEAK detected — attempting recovery
      (direct switch.turn_off + recheck)"*.
- [ ] **H.1.c** — Within ~10 s **clear the simulated leak** (close
      the parallel tap, or restore the flow sensor to 0).
- [ ] **H.1.d** — Log shows:
      *"Valve '\<zone\>' leak recovery succeeded (flow=0.000)"*.
- [ ] **H.1.e** — The irrigation cycle finishes with the zone reset
      and `last_irrigated` updated.
- [ ] **H.1.f** — **No** persistent notification was created (the
      recovery handled the leak silently).

### H.2 — Recovery fails → emergency stop + CRITICAL notification

Pre-conditions: same flow-meter zone. This test exercises the
escalation path, so keep at least one other zone with a valve so you
can verify that `never_dry.stop` propagates.

- [ ] **H.2.a** — Trigger irrigation. While close-verification is in
      progress, force the flow sensor to a non-zero value and **keep
      it there for >25 s** (10 s leak timeout + 10 s recovery window +
      buffer).
- [ ] **H.2.b** — Log shows the recovery attempt (as in H.1.b).
- [ ] **H.2.c** — Log shows:
      *"Valve '\<zone\>' leak recovery failed (flow=…)"* followed by
      *"stuck-open confirmed after recovery; calling never_dry.stop"*.
- [ ] **H.2.d** — **Every other valve switch goes off** within ~2 s
      (the operator called `never_dry.stop`).
- [ ] **H.2.e** — A persistent notification appears under *Settings →
      System → Notifications*. Title contains *"\[CRITICAL\] Valve
      stuck open"*. Body explicitly tells you to **shut off the main
      water valve**.
- [ ] **H.2.f** — Clear the simulated leak. Manually dismiss the
      notification. Run another irrigation on the same zone: it
      should work normally — recovery state is per-call, not sticky.

### H.3 — Recovery is attempted only once per close()

- [ ] **H.3.a** — Repeat H.2 (force a persistent leak).
- [ ] **H.3.b** — In the HA log, search for the *"attempting
      recovery"* line for that specific zone+timestamp. Confirm there
      is **exactly one** occurrence per `close()` call (not two, not
      three).

If recovery is attempted more than once per call, file a regression
issue with the log bundle (see *Diagnostic bundle* below) — that
would indicate a flag-reset bug in the operator.

## Test I — Real-time deficit drop during a flow-metered cycle

Pre-conditions: a zone with `delivery_mode = flow_meter` and a working
flow sensor. Take note of the zone's `deficit_mm` and `last_volume_delivered`
on the entity card before triggering.

- [ ] **I.1** — Trigger irrigation on this zone.
- [ ] **I.2** — Open the entity card or the zone-deficit sensor in HA
      and watch the value. Every 2 s (poll interval) the `deficit_mm`
      should **drop** by `flow_delta * efficiency / area`. You should
      see it move multiple times during the cycle, not only at the end.
- [ ] **I.3** — When the cycle completes, the final value matches
      `deficit_at_start - delivered_total * efficiency / area`
      (or 0 if the full target was delivered).
- [ ] **I.4** — **Crash test (optional but valuable)**. While the cycle
      is running, restart HA from Settings → System → Restart. After
      restart, the zone's `deficit_mm` should reflect the *last*
      real-time value written before the restart — not the original
      pre-irrigation value. Some mm of irrigation is preserved even
      on an unclean shutdown.

If you do **not** see incremental drops, double-check:
- The delivery mode is flow_meter or flow_rate (estimated_flow has no
  metering and the deficit only updates at end).
- The flow sensor entity is reading a numeric value (Developer Tools
  → States).

## Test G — Notifications deduplication

Pre-conditions: force a transient failure on a `flow_meter` zone
(unplug the flow meter; trigger irrigation; plug it back in).

- [ ] **G.1** — The first failure produces a persistent notification.
- [ ] **G.2** — Repeating the same trigger with the flow meter still
      unplugged does **not** stack a second identical notification —
      the same `notification_id` is reused (visible in *Settings →
      System → Notifications*, only one entry per `<zone, kind>`).
- [ ] **G.3** — Once the meter is back and a successful cycle
      completes, the persistent notification should be dismissable
      and not reappear.

---

## Test J — Logging, reload robustness, and controller lifecycle *(session 2026-06-19)*

### J.1 — Version visible in activity log on startup

- [ ] **J.1.a** — After any HA restart or integration reload, open
      `/config/never_dry_activity.log`. The first NeverDry line must
      read:
      `NeverDry <version> — activity log -> /config/never_dry_activity.log (5 MB x 3)`
      (e.g. *NeverDry 0.9.1 — …*). If the version is missing, the
      `manifest.json` read in `_setup_file_logger` failed.

### J.2 — INFO messages survive integration reload

Background: HA sets the `custom_components.never_dry` logger level to
WARNING after a reload, filtering INFO messages unless we explicitly
call `setLevel(DEBUG)` in `_setup_file_logger`.

- [ ] **J.2.a** — While the integration is running, trigger a reload
      (Settings → NeverDry → ⋮ → Reload).
- [ ] **J.2.b** — Immediately after, the activity log must show the
      startup banner (J.1.a) **and** the `Mode B (scheduled): zone=…`
      INFO lines. If only WARNING/ERROR appear, the `setLevel` fix is
      missing.

### J.3 — External valve close aborts `estimated_flow` session immediately

- [ ] **J.3.a** — Start irrigation on an `estimated_flow` zone with
      enough deficit for a session lasting several minutes.
- [ ] **J.3.b** — While the valve is open, close it directly from the
      Zigbee/ZHA interface (not from the NeverDry UI).
- [ ] **J.3.c** — Within 1–2 s the activity log must show:
      `Valve '<entity>' closed externally after Xs — aborting estimated_flow wait`
- [ ] **J.3.d** — Within the same second:
      `SESSION_RESULT zone=… volume_delivered_L=<proportional> deficit_mm_post=<reduced>`
      The deficit must reflect only the water actually delivered (not
      the full planned amount).
- [ ] **J.3.e** — The `CLOSE_VERIFICATION_FAILED` ERROR that follows is
      expected (controller tries to close an already-closed valve) and
      is **not** a regression.

### J.4 — Graceful stop when config changes during irrigation

- [ ] **J.4.a** — Start irrigation on an `estimated_flow` zone (session
      long enough to give you 30+ s to act).
- [ ] **J.4.b** — While the valve is open, edit any zone parameter
      (e.g. area m²) via Settings → NeverDry → Configure.
- [ ] **J.4.c** — The activity log must show, within ~1 s of saving:
      ```
      DEBUG  Config updated via edit_zone — zone edited: <zone>
      INFO   Config entry data changed — reloading integration
      INFO   Irrigation stopped by user after 0 zones   ← graceful stop
      INFO   SESSION_RESULT zone=… volume_delivered_L=<partial>
      INFO   NeverDry <version> — activity log → …      ← new controller
      ```
- [ ] **J.4.d** — The valve is **closed** after the reload (not left
      open). Verify in HA entity state.
- [ ] **J.4.e** — The deficit is updated proportionally to the water
      delivered before the stop — **not** zeroed by the new controller's
      manual-detection logic.

### J.5 — No spurious "manual irrigation detected" after NeverDry-initiated close

Background: before the `_controller_closing` fix, the close event fired
by `operator.close()` was processed by `_on_valve_state_change` after
the operator reached IDLE, triggering a false manual-irrigation reset.

- [ ] **J.5.a** — Start and complete a short `estimated_flow` irrigation
      (let it run to natural completion, do not close manually).
- [ ] **J.5.b** — In the activity log, confirm there is **no**
      `Manual irrigation detected` line in the 2 s following the
      `Valve '<entity>' close latency …` line.
- [ ] **J.5.c** — `deficit_mm` after the cycle matches
      `deficit_pre - delivered_mm * efficiency` (not 0).

### J.6 — Config change suppressed when data is unchanged

- [ ] **J.6.a** — Open Settings → NeverDry → Configure → edit a zone →
      change nothing → save.
- [ ] **J.6.b** — The activity log must **not** show
      `Config entry data changed — reloading integration`.
      No reload must occur. The integration keeps running without
      interruption.

---

## Diagnostic bundle — how to capture and download the HA add-on log

Use this whenever a step above fails or you see a notification you
don't understand. The bundle goes onto a GitHub issue or, for personal
debugging, into the project's `~/.code_reader/` inbox.

### One-time setup
- [ ] **Settings → System → Logs** → set log level for the integration:
      paste this into *Developer Tools → Services* and call once:
      ```yaml
      service: logger.set_level
      data:
        custom_components.never_dry: debug
      ```
      This stays in effect until HA restarts.

### After the failed step
1. **Reproduce the failure** with the debug log level active.
2. **Download the full Home Assistant log**:
   - *Settings → System → Logs → "Load full Home Assistant log"*
     (top of page) → **Download** button (top right). Saves a
     `home-assistant.log` file.
3. **If running Home Assistant OS / Supervised**, also download the
   **Core add-on log**:
   - *Settings → Add-ons → Home Assistant Core → "Log" tab → arrow
     icon (top right of the log pane) → "Download log"*.
4. **Capture the integration diagnostics**:
   - *Settings → Devices & Services → NeverDry → ⋮ menu → "Download
     diagnostics"*. Saves a JSON file with the integration config
     (secrets are scrubbed automatically).
5. **Capture a screenshot of the persistent notifications panel** if
   any notification is part of the failure.
6. **Note**, in the same order:
   - The HA version and NeverDry version.
   - Which test step failed (e.g. *Test C, step C.6*).
   - The time of the failure (UTC + local), so we can grep the log.
7. **Bundle** all of the above (`home-assistant.log`, add-on log,
   diagnostics JSON, screenshot, notes) into a single `.zip`:
   - File name pattern: `neverdry_test_YYYYMMDD_HHMM_step.zip`.
8. **Attach** the zip to a GitHub issue (`never-dry/NeverDry/issues/new`)
   with a title like *"Field test — step C.6 failure"*.

### CLI shortcut for power users

If you have SSH access to the HAOS host (Advanced SSH & Web Terminal
add-on):

```sh
# Full log of the Home Assistant Core service
ha core logs --no-color > home-assistant.log

# Last 1000 lines only (often enough for a recent failure)
ha core logs --lines 1000 --no-color > recent.log

# Search for our integration during a given hour
grep -i "never_dry\|valve" home-assistant.log | grep "14:3[0-9]:"
```

The `ha` CLI also exposes:

```sh
ha supervisor logs    # supervisor-level events (zigbee restarts etc.)
ha host logs          # OS-level (HAOS only)
```

---

## After-test wrap-up

Once every test box above is ticked:

- [ ] Mark the cluster items closed on the project tracker.
- [ ] Update the README and `info.md` if any user-visible behaviour
      changed (the `volume_preset` fix and the notifier strings will
      probably justify a short release note).
- [ ] Open the next cluster (absolute watchdog, restart
      recovery, zone health counter) — they all build on the
      FSM/operator and on the leak-recovery path now in production.

If any test failed and the issue is structural, **do not roll back the
whole cluster** — the most likely fault is in the operator's
state-resolution or in one specific delivery mode. The FSM unit tests
already cover the pure logic; ship a targeted fix.

---

## Revision history

| Date | Change |
|---|---|
| 2026-05 | Initial — manual field-test checklist. |
