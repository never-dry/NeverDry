/**
 * NeverDry Zone Card — single-file custom Lovelace card.
 *
 * Vanilla custom element (no build step, no Lit/CDN dependency) so it can be
 * served and auto-registered directly by the Python integration.
 *
 * Shows every entity of ONE NeverDry zone-device, grouped by time horizon:
 *   - At-a-glance status chips (valve state / irrigating / maintenance) + last source
 *   - Deficit-vs-threshold bar (current state)
 *   - Next session (planned volume / duration)
 *   - Last session (last irrigated / duration / volume / session water)
 *   - Totals (yearly water / rain cumulative)
 *   - Parameters (threshold / flow rate / area / kc / efficiency / mode / time)
 *   - Actions (Irrigate / Mark irrigated / Reset valve) as real buttons
 *
 * Entity resolution prefers each entity's stable `unique_id` prefix (fetched
 * once from the entity registry) and falls back to the entity_id suffix, so
 * user renames of entity_ids don't break the card. Labels come from the
 * localized friendly_name; values via formatEntityState (language + units).
 */

const CARD_VERSION = "0.1.8";

// Static UI strings that are NOT backed by an entity (everything else is read
// from the entity's localized friendly_name / formatEntityState, so it follows
// the integration's own translations and the user's language automatically).
const I18N = {
  en: {
    selectZone: "Select a zone in the card editor.",
    noEntities: "No NeverDry entities found for this device.",
    noZones: "No NeverDry zones found.",
    zone: "Zone",
    selectPlaceholder: "Select a zone…",
    due: "irrigation due",
    barUnavailable: "deficit / threshold unavailable",
    irrigateNow: "Irrigate now",
    irrigating: "Irrigating",
    maintenance: "Maintenance",
    unreachable: "Valve not responding",
    unreachableHint: "check the radio link or the batteries",
    valve: "Valve",
    secNext: "Next session",
    secLast: "Last session",
    secTotals: "Totals",
    secParams: "Parameters",
  },
  it: {
    selectZone: "Seleziona una zona nell'editor della scheda.",
    noEntities: "Nessuna entità NeverDry trovata per questo dispositivo.",
    noZones: "Nessuna zona NeverDry trovata.",
    zone: "Zona",
    selectPlaceholder: "Seleziona una zona…",
    due: "irrigazione necessaria",
    barUnavailable: "deficit / soglia non disponibili",
    irrigateNow: "Irriga ora",
    irrigating: "In irrigazione",
    maintenance: "Manutenzione",
    unreachable: "Valvola non raggiungibile",
    unreachableHint: "controlla il collegamento radio o le batterie",
    valve: "Valvola",
    secNext: "Prossima sessione",
    secLast: "Ultima sessione",
    secTotals: "Totali",
    secParams: "Parametri",
  },
};

// Localized human labels for the valve FSM state (valve_fsm.py ValveState).
const VALVE_STATE_I18N = {
  en: {
    idle: "idle",
    closed: "closed",
    open: "open",
    open_verified: "open (verified)",
    req_open: "opening…",
    req_close: "closing…",
    maintenance: "maintenance",
    unreachable: "not responding",
  },
  it: {
    idle: "ferma",
    closed: "chiusa",
    open: "aperta",
    open_verified: "aperta ✓",
    req_open: "apertura…",
    req_close: "chiusura…",
    maintenance: "manutenzione",
    unreachable: "non risponde",
  },
};

function t(hass, key) {
  const lang = ((hass && hass.language) || "en").split("-")[0];
  return (I18N[lang] && I18N[lang][key]) || I18N.en[key] || key;
}

function valveStateLabel(hass, state) {
  if (!state) return "—";
  const lang = ((hass && hass.language) || "en").split("-")[0];
  const m = VALVE_STATE_I18N[lang] || VALVE_STATE_I18N.en;
  return m[state] || state;
}

/** Icon + color for a valve FSM state. */
function valveMeta(state) {
  switch (state) {
    case "open":
    case "open_verified":
      return { color: "var(--success-color, #43a047)", icon: "mdi:valve-open" };
    case "req_open":
    case "req_close":
      return { color: "var(--warning-color, #ffa600)", icon: "mdi:valve" };
    case "maintenance":
      return { color: "var(--error-color, #db4437)", icon: "mdi:wrench-clock" };
    case "unreachable":
      return { color: "var(--warning-color, #ffa600)", icon: "mdi:access-point-network-off" };
    default: // idle / closed / unknown
      return { color: "var(--secondary-text-color)", icon: "mdi:valve-closed" };
  }
}

// Map of role -> entity_id suffix (the English _attr_name slug, stable for
// un-renamed entities). `hass.entities` exposes entity_id/device_id/platform but
// NOT original_name, so we match on the object_id suffix instead. Longest suffix
// wins (see _zoneEntities) so "_last_volume" beats "_volume", etc.
const ROLE_SUFFIX = {
  volume: "_volume",
  deficit: "_deficit",
  rain: "_rain_yearly",
  threshold: "_threshold",
  sessionWater: "_session_water",
  yearlyWater: "_yearly_water",
  lastVolume: "_last_volume",
  flowRate: "_flow_rate",
  duration: "_duration",
  lastDuration: "_last_duration",
  lastIrrigated: "_last_irrigated",
  lastSource: "_last_source",
  irrigationMode: "_irrigation_mode",
  irrigationTime: "_irrigation_time",
  kc: "_kc",
  area: "_area",
  efficiency: "_efficiency",
  // buttons
  btnIrrigate: "_irrigate",
  btnMark: "_mark_irrigated",
  btnStop: "_stop",
  btnReset: "_reset_valve",
};

// Preferred mapping: role -> unique_id prefix (hardcoded in sensor.py/button.py,
// stable and identical across zones even when the user renames entity_ids).
// unique_id is fetched once via the entity registry; ROLE_SUFFIX is the fallback.
const UID_PREFIX = {
  volume: "irrigation_zone_",
  deficit: "deficit_zone_",
  rain: "rain_yearly_zone_",
  threshold: "threshold_zone_",
  sessionWater: "session_water_zone_",
  yearlyWater: "yearly_water_zone_",
  lastVolume: "last_volume_zone_",
  flowRate: "flow_rate_zone_",
  duration: "duration_zone_",
  lastDuration: "last_duration_zone_",
  lastIrrigated: "last_irrigated_zone_",
  lastSource: "last_source_zone_",
  irrigationMode: "irrigation_mode_zone_",
  irrigationTime: "irrigation_time_zone_",
  kc: "kc_zone_",
  area: "area_zone_",
  efficiency: "efficiency_zone_",
  btnIrrigate: "irrigate_",
  btnMark: "mark_irrigated_",
  btnStop: "stop_",
  btnReset: "reset_valve_",
};

// A NeverDry zone is a device created by the integration with this model.
const ZONE_MODEL = "Irrigation Zone";

class NeverDryZoneCard extends HTMLElement {
  setConfig(config) {
    if (!config) throw new Error("Invalid configuration");
    this._config = config;
    this._built = false;
    if (this._hass) this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  getCardSize() {
    return 7;
  }

  static getConfigElement() {
    return document.createElement("never-dry-zone-card-editor");
  }

  static getStubConfig(hass) {
    const device = pickFirstZoneDevice(hass);
    return { type: "custom:never-dry-zone-card", device_id: device || "" };
  }

  // ---- entity resolution ------------------------------------------------

  _zoneEntities() {
    // Returns { role: stateObj } for the configured device. Prefers the stable
    // unique_id prefix (from the entity registry); falls back to entity_id
    // suffix when the registry isn't loaded yet or the unique_id is unknown.
    const hass = this._hass;
    const deviceId = this._config && this._config.device_id;
    const out = {};
    if (!hass || !deviceId || !hass.entities) return out;

    const uidMap = this._uidMap;
    const suffixRoles = Object.entries(ROLE_SUFFIX).sort((a, b) => b[1].length - a[1].length);
    const uidRoles = Object.entries(UID_PREFIX);

    for (const ent of Object.values(hass.entities)) {
      if (ent.device_id !== deviceId) continue;
      const st = hass.states[ent.entity_id];
      if (!st) continue;

      // 1) Preferred: stable unique_id prefix.
      const uid = uidMap && uidMap[ent.entity_id];
      if (uid) {
        // unique_ids are entry-scoped since GH #116
        // ("<entry_id>_irrigate_<zone>"): strip the entry prefix before
        // matching, but keep trying the raw uid for unmigrated installs.
        const bare = uid.slice(uid.indexOf("_") + 1);
        let matched = false;
        for (const [role, prefix] of uidRoles) {
          if (!out[role] && (uid.startsWith(prefix) || bare.startsWith(prefix))) {
            out[role] = st;
            matched = true;
            break;
          }
        }
        if (matched) continue;
      }

      // 2) Fallback: entity_id suffix (longest-match).
      const objectId = (ent.entity_id.split(".")[1] || "").toLowerCase();
      for (const [role, suffix] of suffixRoles) {
        if (!out[role] && objectId.endsWith(suffix)) {
          out[role] = st;
          break;
        }
      }
    }
    return out;
  }

  _ensureRegistry() {
    // Lazily load entity_id -> unique_id for never_dry entities (admin WS call).
    if (this._uidMap || this._uidLoading || !this._hass) return;
    this._uidLoading = true;
    this._hass
      .callWS({ type: "config/entity_registry/list" })
      .then((list) => {
        const map = {};
        for (const e of list) {
          if (e.platform === "never_dry" && e.unique_id) map[e.entity_id] = e.unique_id;
        }
        this._uidMap = map;
      })
      .catch(() => {
        this._uidMap = {}; // give up -> suffix fallback stays in effect
      })
      .finally(() => {
        this._uidLoading = false;
        this._built = false; // rebuild with corrected mapping
        this._render();
      });
  }

  _deviceName() {
    const hass = this._hass;
    const deviceId = this._config && this._config.device_id;
    if (hass && hass.devices && hass.devices[deviceId]) {
      const d = hass.devices[deviceId];
      return d.name_by_user || d.name || "NeverDry zone";
    }
    return "NeverDry zone";
  }

  /** Localized short label for an entity = its friendly_name minus device prefix. */
  _label(st, fallback) {
    const fn = st && st.attributes && st.attributes.friendly_name;
    if (fn) {
      const dn = this._deviceName();
      return fn.startsWith(dn + " ") ? fn.slice(dn.length + 1) : fn;
    }
    return fallback;
  }

  // ---- rendering --------------------------------------------------------

  _render() {
    if (!this._hass || !this._config) return;

    if (!this._config.device_id) {
      this._renderEmpty(t(this._hass, "selectZone"));
      return;
    }
    this._ensureRegistry();
    const ents = this._zoneEntities();
    if (Object.keys(ents).length === 0) {
      this._renderEmpty(t(this._hass, "noEntities"));
      return;
    }

    if (!this._built) this._buildStructure();
    this._update(ents);
  }

  _renderEmpty(msg) {
    this._built = false;
    this.innerHTML = `
      <ha-card header="NeverDry">
        <div style="padding:16px;color:var(--secondary-text-color)">${msg}</div>
      </ha-card>`;
  }

  _buildStructure() {
    this.innerHTML = `
      <ha-card>
        <style>${CARD_CSS}</style>
        <div class="nd-head">
          <ha-icon icon="mdi:sprinkler-variant"></ha-icon>
          <span class="nd-title"></span>
        </div>

        <div class="nd-status">
          <div class="nd-status-chips"></div>
          <div class="nd-status-src"></div>
        </div>

        <div class="nd-bar-wrap">
          <div class="nd-bar-labels">
            <span class="nd-bar-lbl"></span><span class="nd-bar-val"></span>
          </div>
          <div class="nd-bar"><div class="nd-bar-fill"></div></div>
          <div class="nd-bar-sub"></div>
        </div>

        <div class="nd-section" data-key="next">
          <div class="nd-sec-title"></div><div class="nd-grid"></div>
        </div>
        <div class="nd-section" data-key="last">
          <div class="nd-sec-title"></div><div class="nd-grid"></div>
        </div>
        <div class="nd-section" data-key="totals">
          <div class="nd-sec-title"></div><div class="nd-grid"></div>
        </div>
        <div class="nd-section" data-key="params">
          <div class="nd-sec-title"></div><div class="nd-grid"></div>
        </div>

        <div class="nd-actions"></div>
      </ha-card>`;

    this._el = {
      title: this.querySelector(".nd-title"),
      statusChips: this.querySelector(".nd-status-chips"),
      statusSrc: this.querySelector(".nd-status-src"),
      barLbl: this.querySelector(".nd-bar-lbl"),
      barVal: this.querySelector(".nd-bar-val"),
      barFill: this.querySelector(".nd-bar-fill"),
      barSub: this.querySelector(".nd-bar-sub"),
      actions: this.querySelector(".nd-actions"),
    };
    this._buildActions();
    this._built = true;
  }

  _buildActions() {
    // Labels are filled in _update() from each button entity's localized
    // friendly_name; "irrigateNow" has a dedicated static string for emphasis.
    this._actionDefs = [
      { role: "btnIrrigate", icon: "mdi:sprinkler", i18n: "irrigateNow", cls: "primary" },
      { role: "btnStop", icon: "mdi:stop", cls: "warn" },
      { role: "btnMark", icon: "mdi:water-check", cls: "" },
      { role: "btnReset", icon: "mdi:lock-reset", cls: "warn" },
    ];
    this._el.actions.innerHTML = "";
    this._actionBtns = {};
    for (const d of this._actionDefs) {
      const btn = document.createElement("button");
      btn.className = `nd-btn ${d.cls}`.trim();
      btn.innerHTML = `<ha-icon icon="${d.icon}"></ha-icon><span class="nd-btn-lbl"></span>`;
      btn.addEventListener("click", () => this._press(d.role));
      this._el.actions.appendChild(btn);
      this._actionBtns[d.role] = btn;
    }
  }

  _press(role) {
    const ents = this._zoneEntities();
    const st = ents[role];
    if (!st) return;
    this._hass.callService("button", "press", { entity_id: st.entity_id });
  }

  _update(ents) {
    const hass = this._hass;
    this._el.title.textContent = this._deviceName();

    // --- at-a-glance status chips (valve state / irrigating / maintenance) ---
    this._el.statusChips.innerHTML = this._statusChips(ents);

    // Last source, top-right aligned with the valve state.
    const srcVal = fmtState(hass, ents.lastSource);
    this._el.statusSrc.innerHTML = srcVal
      ? `<ha-icon icon="mdi:source-branch"></ha-icon><span>${escapeHtml(srcVal)}</span>`
      : "";

    // --- deficit vs threshold bar ---
    // The percentage is a ratio of two same-unit values (mm), so it is
    // independent of the user's measurement system. Displayed values go
    // through formatEntityState → unit-system + locale aware.
    const deficit = numState(ents.deficit);
    const threshold = numState(ents.threshold);
    this._el.barLbl.textContent = this._label(ents.deficit, "Deficit");
    if (deficit != null && threshold != null && threshold > 0) {
      const pct = Math.max(0, Math.min(100, (deficit / threshold) * 100));
      this._el.barFill.style.width = `${pct}%`;
      this._el.barFill.style.background = barColor(pct);
      this._el.barVal.textContent = `${pct.toFixed(0)}%`;
      const dStr = fmtState(hass, ents.deficit);
      const tStr = fmtState(hass, ents.threshold);
      this._el.barSub.textContent =
        `${dStr} / ${tStr}` + (deficit >= threshold ? ` — ${t(hass, "due")}` : "");
    } else {
      this._el.barFill.style.width = "0%";
      this._el.barVal.textContent = "—";
      this._el.barSub.textContent = t(hass, "barUnavailable");
    }

    // --- temporal sections (label = localized friendly_name, value = formatEntityState) ---
    // Current state (deficit) lives in the bar above; here we group by horizon.
    this._fillSection("next", t(hass, "secNext"), [
      ["mdi:cup-water", ents.volume, "Volume"],
      ["mdi:timer-sand", ents.duration, "Duration", "duration"],
    ]);
    const _carrier = ents.deficit || ents.volume;
    const _irrigating = !!(_carrier && _carrier.attributes && _carrier.attributes.irrigating === true);
    const lastRows = [
      ["mdi:clock-outline", ents.lastIrrigated, "Last irrigated"],
      ["mdi:history", ents.lastDuration, "Last duration", "duration"],
      ["mdi:water-outline", ents.lastVolume, "Last volume"],
    ];
    // Session water is a LIVE progress indicator: when idle it is 0 (not
    // persisted across restarts) or just duplicates Last volume, so show it
    // only while the zone is actively irrigating. It becomes meaningful in
    // every delivery mode once the driver abstraction reports a real-time
    // delivered volume — measured or calculated (AI-128 / AI-186).
    if (_irrigating) {
      lastRows.push(["mdi:water", ents.sessionWater, "Session water"]);
    }
    this._fillSection("last", t(hass, "secLast"), lastRows);
    this._fillSection("totals", t(hass, "secTotals"), [
      ["mdi:water-plus", ents.yearlyWater, "Yearly water"],
      ["mdi:weather-rainy", ents.rain, "Rain"],
    ]);
    // Static / config parameters last.
    this._fillSection("params", t(hass, "secParams"), [
      ["mdi:target", ents.threshold, "Threshold"],
      ["mdi:speedometer", ents.flowRate, "Flow rate"],
      ["mdi:texture-box", ents.area, "Area"],
      ["mdi:leaf", ents.kc, "Kc"],
      ["mdi:percent", ents.efficiency, "Efficiency"],
      ["mdi:cog", ents.irrigationMode, "Mode"],
      ["mdi:clock-time-six", ents.irrigationTime, "Irrigation time"],
    ]);

    // --- action buttons (localized label from button entity friendly_name) ---
    for (const d of this._actionDefs) {
      const btn = this._actionBtns[d.role];
      const st = ents[d.role];
      btn.disabled = !st;
      const lbl = d.i18n ? t(hass, d.i18n) : this._label(st, d.role);
      btn.querySelector(".nd-btn-lbl").textContent = lbl;
    }
  }

  _fillSection(key, title, items) {
    const box = this.querySelector(`.nd-section[data-key="${key}"]`);
    if (!box) return;
    const html = this._rows(items);
    box.querySelector(".nd-sec-title").textContent = title;
    box.querySelector(".nd-grid").innerHTML = html;
    box.style.display = html ? "" : "none"; // hide a section with no available data
  }

  _statusChips(ents) {
    const hass = this._hass;
    // valve_fsm_state / valve_in_maintenance / irrigating live as attributes on
    // the Deficit (or Volume) sensor.
    const carrier = ents.deficit || ents.volume;
    const a = (carrier && carrier.attributes) || {};
    const chips = [];

    // Valve state — always shown.
    const vState = a.valve_fsm_state;
    const vm = valveMeta(vState);
    chips.push(this._chip(vm.icon, `${t(hass, "valve")}: ${valveStateLabel(hass, vState)}`, vm.color));

    // Irrigating — only when active.
    if (a.irrigating === true) {
      chips.push(this._chip("mdi:sprinkler-variant", t(hass, "irrigating"), "var(--info-color, #2196f3)"));
    }

    // Not responding — amber warning triangle. Deliberately its own chip and
    // not a shade of the valve-state one: "did not answer" is a radio problem
    // the user can act on, and it stays true while the valve keeps reporting a
    // perfectly ordinary "off". Without this the only symptom of a flaky valve
    // is that pressing Irrigate appears to do nothing for the better part of a
    // minute, and then the zone is blocked (field, 'Giardino Pino').
    if (a.valve_reachable === false) {
      chips.push(
        this._chip("mdi:alert", `${t(hass, "unreachable")} — ${t(hass, "unreachableHint")}`, "var(--warning-color, #ffa600)"),
      );
    }

    // Maintenance — only when in maintenance (red, the at-a-glance alarm).
    // Shown alongside the amber one when both apply: they say different things,
    // and a zone blocked *because* the valve stopped answering is precisely the
    // case where the user needs to read both.
    if (a.valve_in_maintenance === true) {
      chips.push(this._chip("mdi:wrench", t(hass, "maintenance"), "var(--error-color, #db4437)"));
    }

    return chips.join("");
  }

  _chip(icon, label, color) {
    return `<span class="nd-chip" style="--c:${color}">
      <ha-icon icon="${icon}"></ha-icon>${escapeHtml(label)}</span>`;
  }

  _rows(items) {
    return items
      .map(([icon, st, fallback, fmt]) => {
        const v = fmt === "duration" ? fmtDuration(this._hass, st) : fmtState(this._hass, st);
        if (v === null) return "";
        const label = this._label(st, fallback);
        return `
        <div class="nd-cell">
          <ha-icon icon="${icon}"></ha-icon>
          <div class="nd-cell-txt">
            <span class="nd-cell-lbl">${escapeHtml(label)}</span>
            <span class="nd-cell-val">${escapeHtml(v)}</span>
          </div>
        </div>`;
      })
      .join("");
  }
}

// ---- helpers ------------------------------------------------------------

function numState(st) {
  if (!st) return null;
  const n = Number(st.state);
  return Number.isFinite(n) ? n : null;
}

/**
 * Display a state value the way Home Assistant would: applies the user's
 * measurement system (unit conversion), locale number formatting and enum
 * state translation. Falls back to raw state + unit if the helper is absent.
 */
function fmtState(hass, st) {
  if (!st || st.state === "unknown" || st.state === "unavailable") return null;
  try {
    if (typeof hass.formatEntityState === "function") {
      return hass.formatEntityState(st);
    }
  } catch (e) {
    /* fall through to raw rendering */
  }
  const unit = st.attributes && st.attributes.unit_of_measurement;
  return unit ? `${st.state} ${unit}` : st.state;
}

// Format a DURATION sensor (native unit: seconds) as mm:ss, or h:mm:ss past
// one hour — so a duration reads "17:53" instead of "1.073 s" with no mental
// conversion. Falls back to fmtState for non-numeric / unavailable states.
function fmtDuration(hass, st) {
  const n = numState(st);
  if (n === null) return fmtState(hass, st);
  const total = Math.max(0, Math.round(n));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const pad = (x) => String(x).padStart(2, "0");
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function barColor(pct) {
  if (pct >= 90) return "var(--error-color, #db4437)";
  if (pct >= 60) return "var(--warning-color, #ffa600)";
  return "var(--success-color, #43a047)";
}

function pickFirstZoneDevice(hass) {
  const zones = zoneDevices(hass);
  return zones.length ? zones[0].id : "";
}

function zoneDevices(hass) {
  // Returns [{id, name}] for every NeverDry zone device, identified by the
  // never_dry identifier + the "Irrigation Zone" model (so the hub is excluded).
  const out = [];
  if (!hass || !hass.devices) return out;
  for (const d of Object.values(hass.devices)) {
    const isNeverDry = (d.identifiers || []).some((t) => t[0] === "never_dry");
    if (isNeverDry && d.model === ZONE_MODEL) {
      out.push({ id: d.id, name: d.name_by_user || d.name || d.id });
    }
  }
  out.sort((a, b) => a.name.localeCompare(b.name));
  return out;
}

const CARD_CSS = `
  ha-card { padding: 12px 12px 16px; }
  .nd-head { display:flex; align-items:center; gap:8px; margin-bottom:12px; }
  .nd-head ha-icon { color: var(--primary-color); }
  .nd-title { font-size:1.15rem; font-weight:600; }
  .nd-status { display:flex; align-items:center; justify-content:space-between;
    gap:8px; margin:2px 0 12px; }
  .nd-status-chips { display:flex; flex-wrap:wrap; gap:6px; min-width:0; }
  .nd-status-src { display:inline-flex; align-items:center; gap:4px; flex:0 0 auto;
    font-size:.8rem; color:var(--secondary-text-color); white-space:nowrap; }
  .nd-status-src ha-icon { --mdc-icon-size:16px; }
  .nd-chip { display:inline-flex; align-items:center; gap:4px;
    padding:3px 9px; border-radius:12px; font-size:.78rem; font-weight:600;
    color: var(--c, var(--secondary-text-color));
    background: color-mix(in srgb, var(--c, #888) 14%, transparent);
    border: 1px solid color-mix(in srgb, var(--c, #888) 35%, transparent); }
  .nd-chip ha-icon { --mdc-icon-size:16px; }
  .nd-bar-wrap { margin: 4px 0 14px; }
  .nd-bar-labels { display:flex; justify-content:space-between;
    font-size:.8rem; color:var(--secondary-text-color); margin-bottom:4px; }
  .nd-bar-val { font-weight:600; color:var(--primary-text-color); }
  .nd-bar { height:12px; border-radius:6px; overflow:hidden;
    background: var(--divider-color, #e0e0e0); }
  .nd-bar-fill { height:100%; width:0%; border-radius:6px;
    transition: width .4s ease, background .4s ease; }
  .nd-bar-sub { font-size:.75rem; color:var(--secondary-text-color); margin-top:4px; }
  .nd-grid { display:grid; grid-template-columns:1fr 1fr; gap:8px 14px; }
  .nd-cell { display:flex; align-items:center; gap:8px; min-width:0; }
  .nd-cell ha-icon { color:var(--state-icon-color, var(--paper-item-icon-color));
    flex:0 0 auto; }
  .nd-cell-txt { display:flex; flex-direction:column; min-width:0; }
  .nd-cell-lbl { font-size:.72rem; color:var(--secondary-text-color); }
  .nd-cell-val { font-size:.95rem; font-weight:500;
    overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .nd-section { margin-top:6px; }
  .nd-section + .nd-section { border-top:1px solid var(--divider-color);
    margin-top:12px; padding-top:12px; }
  .nd-sec-title { font-size:.7rem; font-weight:700; letter-spacing:.05em;
    text-transform:uppercase; color:var(--secondary-text-color); margin-bottom:8px; }
  .nd-actions { display:flex; flex-wrap:wrap; gap:8px; margin-top:16px; }
  .nd-btn { display:inline-flex; align-items:center; gap:6px; cursor:pointer;
    border:none; border-radius:18px; padding:8px 14px; font-size:.85rem;
    font-weight:500; color:var(--primary-text-color);
    background: var(--secondary-background-color, #eee); transition:filter .15s; }
  .nd-btn ha-icon { --mdc-icon-size:18px; }
  .nd-btn:hover:not(:disabled) { filter:brightness(.95); }
  .nd-btn:disabled { opacity:.4; cursor:not-allowed; }
  .nd-btn.primary { background: var(--primary-color); color: var(--text-primary-color,#fff); }
  .nd-btn.warn { background: var(--error-color, #db4437); color:#fff; }
`;

// ---- visual config editor ----------------------------------------------

class NeverDryZoneCardEditor extends HTMLElement {
  setConfig(config) {
    this._config = { ...config };
    if (this._hass) this._render();
  }

  set hass(hass) {
    this._hass = hass;
    if (this._config && !this._built) this._render();
  }

  _render() {
    const devices = zoneDevices(this._hass);
    const current = this._config.device_id || "";
    const options = devices
      .map(
        (d) =>
          `<option value="${escapeHtml(d.id)}" ${d.id === current ? "selected" : ""}>${escapeHtml(d.name)}</option>`
      )
      .join("");
    this.innerHTML = `
      <div style="padding:8px 4px;display:flex;flex-direction:column;gap:6px">
        <label style="font-size:.85rem;color:var(--secondary-text-color)">${t(this._hass, "zone")}</label>
        <select id="nd-zone"
          style="padding:8px;border-radius:6px;border:1px solid var(--divider-color);
                 background:var(--card-background-color);color:var(--primary-text-color);font-size:.95rem">
          <option value="" ${current ? "" : "selected"} disabled>${t(this._hass, "selectPlaceholder")}</option>
          ${options}
        </select>
        ${
          devices.length === 0
            ? `<span style="font-size:.8rem;color:var(--error-color)">${t(this._hass, "noZones")}</span>`
            : ""
        }
      </div>`;
    this.querySelector("#nd-zone").addEventListener("change", (e) => {
      this._config = { ...this._config, device_id: e.target.value };
      this.dispatchEvent(
        new CustomEvent("config-changed", {
          detail: { config: this._config },
          bubbles: true,
          composed: true,
        })
      );
    });
    this._built = true;
  }
}

// ---- registration -------------------------------------------------------

if (!customElements.get("never-dry-zone-card")) {
  customElements.define("never-dry-zone-card", NeverDryZoneCard);
}
if (!customElements.get("never-dry-zone-card-editor")) {
  customElements.define("never-dry-zone-card-editor", NeverDryZoneCardEditor);
}

window.customCards = window.customCards || [];
if (!window.customCards.some((c) => c.type === "never-dry-zone-card")) {
  window.customCards.push({
    type: "never-dry-zone-card",
    name: "NeverDry Zone Card",
    description: "All entities of one NeverDry irrigation zone in a clean layout.",
    preview: false,
    documentationURL: "https://github.com/never-dry/NeverDry",
  });
}

console.info(
  `%c NeverDry Zone Card %c v${CARD_VERSION} `,
  "color:#fff;background:#43a047;border-radius:3px 0 0 3px;padding:2px 4px",
  "color:#43a047;background:#fff;border-radius:0 3px 3px 0;padding:2px 4px"
);
