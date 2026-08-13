# Changelog

All notable changes to NeverDry are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- System-wide reset buttons on the NeverDry hub device ([#142]):
  - **Reset yearly rain** — clears the shared year-to-date rain total (behind every zone's *Rain Yearly [L]*) without waiting for 1 January. The total is a saved value that survives a restart and a plain reinstall, so this button is the way to clear a wrong figure — e.g. after switching rain sensor type.
  - **Reset yearly water** — clears *Irrigated Yearly [L]* for every zone at once; each zone's lifetime total is preserved.
  - Both are state-only: recorder long-term statistics (Energy dashboard) are left untouched.
- Per-zone **site exposure**: a microclimate factor (`k_mc`) that multiplies the crop coefficient, so a shaded, windy or paving-adjacent zone keeps its seasonal Kc curve instead of being frozen at one value by a constant Kc override ([#146]). Presets from the landscape coefficient method (0.60 deep shade … 1.20 reflected heat), plus an *Advanced (custom factor)* entry (0.1–1.5). Default *Full sun, open* (×1.00) leaves existing zones unchanged.
- Zone Kc sensor attributes `kc_base`, `exposure` and `microclimate_factor`, so an effective Kc can be traced back to the curve and the factor it came from.

### Fixed
- Soil-moisture probes reporting a **percentage** no longer stop a zone from watering ([#170]). A reading of 45 rather than 0.45 made `(field_capacity − vwc)` negative for every possible value — including a bone-dry 15 % — and the clamp that keeps a deficit from going negative pinned it at exactly zero, silently and forever. Readings are now normalised at the input boundary: above 1 is read as a percentage, exactly 1.0 as a saturated fraction. Consumer probes (Ecowitt, most Zigbee models) work without a template-sensor helper.
- A moisture reading that is not a water content on either scale — a raw ADC count, a negative, a NaN — is now refused instead of being clamped to "saturated": the last good deficit is held and one warning is logged, naming the sensor.
- Imperial units in the config flow and displays ([#139]):
  - Zone threshold help text no longer hardcodes "(mm)" — the field label already shows the user's unit (mm or in).
  - Deficit, threshold and ET sensors now declare a display precision, so imperial users see meaningful decimals instead of values rounded to whole inches.
  - Reconfiguring a zone in imperial is now stable: threshold and max-deficit round-trip through inches without drifting on every edit.

## [0.11.0] - 2026-07-26

First stable of the 0.11 line. Theme: **trust your water balance**.

### Fixed
- Rain over-counting on accumulator sensors: credit only positive increments, so excess rain no longer causes under-watering ([#123]).
- Phantom rain from rolling-24h sensors eliminated.
- Rain baseline survives restarts — no re-crediting of cumulative rain at boot.
- Config-entry-scoped `unique_id`s, avoiding clashes across multiple installations ([#116]).
- Manual valve close now accounts for delivered water instead of resetting the deficit.
- Hardware self-close mid-session is no longer mistaken for manual irrigation.
- Manual sessions settle on entry unload.
- Valve close verification: retry cap of 5, silent transient retries, CRITICAL only on definitive failure.
- Legacy rain entities auto-removed on setup (no orphans); new rain-sensor `unique_id` avoids the mm→L unit-changed repair.

### Added
- Per-zone authoritative deficit, scheduled top-up, and yearly rain.
- "Irrigated Yearly" sensor (irrigation-only; feeds the Home Assistant Energy dashboard).
- Full ET bypass when a soil-moisture (VWC) sensor is configured.
- Zone card: Duration and Last Duration as `mm:ss`, localized "Last irrigated", live "Session water" during flow-metered cycles.
- System sensors editable in the options flow.
- Redesigned landing page with star CTA and download/install badges.
- Public engineering docs, CONTRIBUTING, and Vision.

### Changed
- Documented the `main`/`develop` branching model.
- CI runs on `develop` and on pull requests; dependency bumps.

---

For releases prior to 0.11.0, see the [GitHub Releases](https://github.com/never-dry/NeverDry/releases) page.

[Unreleased]: https://github.com/never-dry/NeverDry/compare/v0.11.0...HEAD
[0.11.0]: https://github.com/never-dry/NeverDry/releases/tag/v0.11.0
[#170]: https://github.com/never-dry/NeverDry/issues/170
[#146]: https://github.com/never-dry/NeverDry/issues/146
[#142]: https://github.com/never-dry/NeverDry/pull/142
[#139]: https://github.com/never-dry/NeverDry/issues/139
[#123]: https://github.com/never-dry/NeverDry/issues/123
[#116]: https://github.com/never-dry/NeverDry/issues/116
