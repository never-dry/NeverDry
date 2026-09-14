# Changelog

All notable changes to NeverDry are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

The domain model stops being scaffolding and starts running the integration. Each
entry below links the design note that explains it properly — this is the summary,
not the argument.

### Added
- **A zone can water by what its soil measures.** Give a zone's probe the depth
  its roots reach and that zone stops estimating its deficit and starts reading
  it. The weather model keeps running underneath the whole time, so a probe that
  goes quiet costs an afternoon on the estimate rather than a season: a reading
  older than that probe's own usual rhythm is set aside and the zone falls back,
  with a line in the log. The depth is the one number no table holds, because a
  lawn, a hedge and a pot differ by a factor of four on the same ground.
  ([#234](https://github.com/never-dry/NeverDry/issues/234))
- **Soil type as a choice rather than a number.** Sandy, loam, clay, or automatic
  for a middle soil, and it supplies the field capacity the probe's reading is
  measured against. Left on automatic the form tells you which soil it is
  assuming, because a default nobody is told about is a hardcoded constant with a
  dropdown in front of it. *Custom* is there for anyone who has measured their
  own.
- **Four ways to compute the water balance, and the site picks one.**
  Temperature-only, Hargreaves, Penman-Monteith, or a soil probe. *Automatic*
  takes the best your sensors support and says which one is running, because the
  number depends on it. Five optional sensor pickers — humidity, wind, net
  radiation and the two daily temperature extremes — unlock the richer tiers; a
  site that declares none behaves exactly as before.
  ([design](docs/design_water_balance_reference_model.md))
- **A supervised valve test that measures your real flow rate**, shown beside the
  configured one so the gap argues for itself. It publishes the meter's smallest
  observed step and its update count too: on a coarse counter those are the limit
  of detection, and without them a lone number looks like a measurement when it is
  an artifact. ([design](docs/design/flow-rate-provenance.md))
- **The quantities the model derives become entities**, not attributes at the
  bottom of a dialog. Judging a computed daily radiation means watching it follow
  the weather for a week, and Home Assistant keeps history for entities only. Only
  the ones the running method actually computes appear.

- **Spanish, both halves.** The configuration forms, errors, notifications and
  entity names, and the zone card's own dictionary, so no screen is left half in
  English. The card's Spanish came from @Mr-Neutr0n
  ([#226](https://github.com/never-dry/NeverDry/pull/226)) and was completed here
  for the labels that landed while it was open; the catalogue was written here
  too. Neither half has been read back by a Spanish speaker yet, and the
  [translations table](docs/translations.md) says so in its own column rather
  than leaving anyone to assume.
  ([#215](https://github.com/never-dry/NeverDry/issues/215))

### Changed
- **The zone card speaks the language of whoever is looking at it.** About half
  its labels used to be taken from the entity names, which Home Assistant
  resolves once in the *server's* language: a household whose frontend was in
  German saw German headings wrapped around Italian labels, and no translation
  could have fixed it. Four cells were labelled with the zone's own name, and two
  different quantities, water delivered and rain, were both called *Water*.
- **Italian, read back against the running product rather than the file.** Around
  seventy strings. A valve that is not answering had three different names; two
  valve states had collapsed into one, so a valve that had never spoken and a
  valve that had stopped answering read alike. Two of those defects were in the
  English source and are fixed there too.
- **The soil probe belongs to a zone, not to the installation.** A probe measures
  one patch of soil with one planting above it; declared once for everything it
  drove zones it knows nothing about. A zone that has one measures instead of
  estimating and stops listening to the site. This also removes, rather than
  fixes, the class of defect where a shared probe overwrote a zone's deficit right
  after that zone was watered. ([design](docs/design/soil-moisture-model.md))
- **Three flow rates, told apart.** *Design* — what the zone was built to deliver.
  *Water meter* — what the counter reads, renamed because it reports litres, not a
  rate. *Historical* — the median of what past sessions actually delivered, and
  what planning now uses when it exists. One label covering all three was the root
  of false leak reports, false actuation failures, and zones quietly watering a
  fraction of what they should. ([design](docs/design/flow-rate-provenance.md))
- **The zone card** shows site exposure, separates what was measured from what was
  derived, acknowledges a press before its outcome, and puts the zone's settings
  one click away.
- **The three ways into a zone now answer the same** ([#196]). First-run setup, *add
  zone* and *edit zone* each took a zone in, and each judged it differently: setup
  refused a delivery mode whose one indispensable input was missing, while the two
  options steps saved it without a word — or, for a flow meter or volume preset
  with no entity behind it, quietly swapped the mode for *estimated flow*. A mode
  you did not choose, needing a design flow rate nobody then checked. All three
  doors now apply the same rule and say the same thing, so a zone that cannot
  deliver is refused wherever you try to save it. If you already have such a zone,
  the next edit will ask you for the missing value before it will save. A zone with
  **no valve** is exempt from all of it: it delivers nothing by design — it watches
  the deficit and tells you when to water by hand — so asking it how fast it waters
  would be asking about something it will never do.
- **Declining "save anyway" no longer undoes your edit** ([#196]). A zone with
  unusual values asks for confirmation, and submitting without ticking the box
  sends you back to the form. That return was drawn from the *saved* zone — the
  configuration you were in the middle of changing — so a box you had just
  emptied came back full. The form now comes back holding what you typed,
  cleared boxes included.
- **The form stops reading identifiers out loud.** An error said *"required for
  estimated_flow delivery mode"* while the dropdown beside it said *"Simple
  on/off"*: the same choice, named twice, once in a language meant for the code.
  Twelve messages and labels did this. In Italian the design flow rate had its
  whole explanation pasted into the label slot, so during setup the field
  announced itself with a 493-character paragraph while every neighbour had a
  name.
- **A silent water meter no longer stops the watering.** A zone whose meter was
  already unavailable when its turn came did not water at all, and said so only in
  the log. Mid-session the code had always judged this the other way — a run that
  measures zero with the valve open is credited from the flow rate, because a
  silent sensor is far more likely than a dry pipe — and a meter that is silent
  from the start is the same fault. It now waters on the estimate, and you are
  told: *Flow meter not reporting* when the volume was estimated, *Water delivered
  could not be credited* when the zone had no design flow rate to estimate with
  and its deficit was therefore left standing. Both are new notifications; until
  now the only trace was a log line nobody opens while the garden looks fine.
  The estimate itself also got better: it follows the rate your sessions actually
  measured, where before it used the design rate regardless. Crediting the design
  rate on a zone that really delivers a fraction of it settles a debt that was
  never paid — and it did so most confidently on exactly the installations whose
  meter had stopped answering.

### Fixed
- **A zone with a probe could sit at zero deficit and never water again**
  ([#234](https://github.com/never-dry/NeverDry/issues/234)). Two installations
  reported it within a day: one probe reading 21 %, four others between 94 and
  100 %, every affected zone stuck at 0.0 mm, and changing the root depth
  changed nothing. The deficit subtracted the reading from the soil's field
  capacity, which requires the two to be the same quantity. A garden probe does
  not report volumetric water content, so once the reading sat above the field
  capacity the result went negative and the clamp turned it into zero. Zero on
  the card looks exactly like soil that has just been watered.

  The reading is now taken for what it can honestly be: where the ground sits
  between dry and wet on the probe's own 0-100 scale, and therefore the share of
  the soil's available water still present. That number is bounded, it cannot
  invert, and only a reading of 100 produces no deficit. The scale is stated
  under the probe field, in every language, along with the fact that it is used
  **uncalibrated**: a real probe is calibrated at the factory against dry air and
  open water, neither of which is a state soil is ever in, so the millimetres are
  out by a factor nobody has measured. Which way, and what would close it, is
  written up in the [design note](docs/design/soil-moisture-model.md).
- **Every restart overwrote the weather reserve with the probe's number**
  ([#234](https://github.com/never-dry/NeverDry/issues/234)). The deficit on
  display is the soil's while a probe drives the zone, and that was the figure
  restored at startup into the estimate underneath. So the reserve a zone falls
  back on when its probe stops being believed was replaced, every restart, by
  whatever the soil happened to say. In the field: one zone at 0.03 mm while its
  three siblings, through the same restart, held 0.31, 1.52 and 2.38. The
  estimate is now published and restored in its own right.
- **A probe reading off the scale is set aside instead of held.** The last good
  value used to stand, which kept it *fresh* for the staleness check and so kept
  a probe talking nonsense in charge indefinitely. The published reading goes
  with it, because a stale percentage on the card reads as the current state of
  the soil.
- **A zone on Custom soil no longer drives on its probe.** Custom supplies a
  field capacity and no wilting point, and half an interval is not a reservoir.
  The missing end is not derived, because the ratio between the two is not
  constant across soils.
- **The soil-probe method could not be chosen by anyone**
  ([#234](https://github.com/never-dry/NeverDry/issues/234)). Picking it answered
  that sensors were missing, and no sensor you could declare would have satisfied
  it: the check asked for a probe bound to the installation, and the probe moved
  onto the zone several releases ago. The option is gone from the site menu,
  which now describes the model your zones run on when they have no probe of
  their own, and the error names the zone setting instead.
- **Saving the model parameters no longer deletes an old site-wide probe.** On an
  installation still waiting to be asked which zone its probe belongs to, opening
  that form and pressing save lost the probe, and the question then disappeared
  unanswered.
- **A flow-metered valve no longer times out on a coarse counter** ([#173]). The
  verification window was a fixed 10 s, but a counter cannot move before its
  resolution divided by the flow rate — 50 s in the original report, around 60 s
  on a meter that steps in 28 L units. The window is now derived per zone. Merely
  widening it would have cost the safety it provides, so where the derivation
  outgrows what still works as a guard the check is declared **inapplicable**
  rather than stretched: the run proceeds and flow stays an observer.
  ([design](docs/design/evidence-and-methodology.md))
- **The valve picker accepts `valve.*` entities**, not only `switch.*` ([#94]).
  The engine could already drive them; the form would not let you choose one.
- **A rejected setup form no longer empties itself in silence** ([#196]). Filling in a
  zone, pressing submit, and watching every field go blank with nothing on screen to
  say why — the form would not close, and the installation could not be completed.
  Two faults met there. The zone form is built from collapsible sections, and an error
  filed against a field *inside* a section never reached the frontend, so no message
  appeared; the three delivery-mode requirements were filed exactly that way, and the
  one most people hit is the design flow rate — mandatory, but impossible to mark as
  such in the form, because it only applies to one delivery mode. On top of that the
  redraw carried none of the submitted values, so even a visible error cost the user
  the whole zone. Errors now always reach the top of the form, and a rejected zone
  comes back filled in. The first step keeps what was typed too: an ET method the
  declared sensors cannot support used to take the sensor pickers down with it.
  What is refused, and where, is now one rule — see *Changed*.

## [0.11.2] - 2026-08-29

A patch stable for the 0.11 line, and it exists for one reason: **a new user could not finish installing the integration.**

Filling in a zone and pressing submit emptied every field and said nothing. The form would not close, and there was no way to learn why. Reported by a new user who got that far and stopped; nobody knows how many got that far and did not write in.

Everything else here is the same fault found in its other hiding places while fixing that one. Three ways into a zone that answered differently, a cleared field that refilled itself, an error code printed raw instead of its message, and a form that named its own internal identifiers out loud.

**Upgrading.** Update through HACS and restart Home Assistant. No entity is renamed or removed and nothing needs reconfiguring.

One change is worth knowing before you next edit a zone. The rules that decide whether a zone can be saved now apply in all three places a zone can be created or changed — first-run setup, *add zone* and *edit zone* — where before only setup enforced them. If an existing zone was saved through the options dialogs without the design flow rate its delivery mode requires, the next edit will ask for that value before it will save. It is the number that makes the zone able to water at all. A zone with no valve is exempt: it delivers nothing by design.


### Changed
- **The three ways into a zone now answer the same** ([#196]). First-run setup, *add zone* and *edit zone* each took a zone in, and each judged it differently: setup refused a delivery mode whose one indispensable input was missing, while the two options steps saved it without a word — or, for a flow meter or volume preset with no entity behind it, quietly swapped the mode for *estimated flow*. A mode you did not choose, needing a design flow rate nobody then checked. All three doors now apply the same rule and say the same thing, so a zone that cannot deliver is refused wherever you try to save it. If you already have such a zone, the next edit will ask you for the missing value before it will save. A zone with **no valve** is exempt from all of it: it delivers nothing by design — it watches the deficit and tells you when to water by hand — so asking it how fast it waters would be asking about something it will never do.

### Fixed
- **Declining "save anyway" no longer undoes your edit** ([#196]). A zone with unusual values asks for confirmation, and submitting without ticking the box sends you back to the form. That return was drawn from the *saved* zone — the configuration you were in the middle of changing — so a box you had just emptied came back full. The form now comes back holding what you typed, cleared boxes included.
- **The form stops reading identifiers out loud.** An error said *"required for estimated_flow delivery mode"* while the dropdown beside it said *"Simple on/off"*: the same choice, named twice, once in a language meant for the code. Twelve messages and labels did this.
- **A rejected zone form no longer empties itself in silence** ([#196]). Filling in a zone, pressing submit, and watching every field go blank with nothing on screen to say why — the form simply would not close, and the installation could not be completed. Two faults met there. The zone form is built from collapsible sections, and an error filed against a field *inside* a section could not be reached by the frontend, so the message never appeared; the three delivery-mode requirements were filed exactly that way, and the one most people hit is the design flow rate, which is mandatory but cannot be marked as such in the form because it only applies to one delivery mode. On top of that the redrawn form carried none of the submitted values, so even a visible error cost the user the whole zone. Errors now always reach the top of the form, and a rejected zone comes back filled in with what was typed. What is refused, and where, is now one rule — see *Changed*.

[#196]: https://github.com/never-dry/NeverDry/issues/196

## [0.11.1] - 2026-08-25

A patch stable for the 0.11 line. Theme: **stop failing quietly**. Almost everything here was a fault that produced no error — a valve that looked inert, a probe pinned at zero, a reload leaving two copies running, an override that came back on its own, a yearly rain total that stayed at zero while it rained.

Nothing in the water-balance engine changes: the domain model classes and the Hargreaves and Penman-Monteith ET tiers are present in the code but inert, with no caller in production. Wiring them is the next minor and goes through a pre-release first.

**Upgrading.** Update through HACS and restart Home Assistant. There is nothing to configure by hand and no entity is renamed or removed — but two things are worth knowing.

Your zone settings are migrated for you: a zone holding a custom efficiency or a manual Kc gets that value marked as *Custom* in the matching dropdown, so it goes on watering exactly as it did. Without that step the number would be ignored at the next start and the zone would fall back to its preset — a drip zone set to 0.55 would jump to 0.92 and water less, with nobody having touched it. Site exposure is deliberately left alone: there the dropdown already decided, so switching a leftover factor on would *change* the watering rather than preserve it. The config flow points those leftovers out the next time you save the zone.

Going back to 0.11.0 is not supported once 0.11.1 has started: the migration marks your configuration as a newer schema, and 0.11.0 refuses to load a configuration it does not understand. If you want a way back, take a backup before updating.

### Changed
- **A delivery is now bounded by the job, not by a constant** ([#173]). The safety timeout used to be combined with the expected duration by taking the *greater* of the two, which made the configured value a floor: a zone with five minutes of work was guarded with the one-hour default, and a flow meter that stopped counting mid-run kept the valve open for the whole hour. The bound is now the expected duration (`volume / flow rate`) times a safety margin, capped by the configured timeout — the field goes back to meaning what the manual has always said it means, an upper bound you can tighten but not loosen. If a zone genuinely needs longer than you allow, it now says so once instead of quietly stopping short. Zones with no guard flow rate configured are unaffected: with no estimate there is nothing to bound with, and the configured timeout is still all there is.
- **One rule for the three preset/override pairs** ([#168]). System type, plant family and site exposure each pair a dropdown with a box for a custom value, and each behaved differently. The rule is now the same everywhere and stated once: **the dropdown decides, and the box is read only when the dropdown says *Custom***. A number typed in the box while a preset is selected no longer takes effect silently. The step on the three dimensionless factors goes from 0.05 to 0.01, because the preset values are not multiples of 0.05 — drip is 0.92 and pop-up sprinklers 0.68, so dialling one back by hand used to be impossible.

### Added
- **The zone card says when a valve is not answering.** An amber warning appears as soon as a command goes unanswered, instead of the press appearing to do nothing. A valve that drops off the radio mesh periodically keeps reporting a perfectly ordinary "off", so it never shows as unavailable: the only symptom used to be a button that seemed inert for the better part of a minute, followed by a blocked zone. The warning distinguishes *did not answer* — a radio problem: check the link, check the batteries — from *answered and delivered no water*, which is hydraulic and needs a different look. It clears itself as soon as the valve replies, and it stays quiet for the first few minutes after a restart, when Zigbee entities are not loaded yet and every zone would otherwise cry wolf. New `valve_reachable` and `valve_last_failure` attributes carry it. See the user manual, *When a valve stops answering*.
- System-wide reset buttons on the NeverDry hub device ([#142]):
  - **Reset yearly rain** — clears the shared year-to-date rain total (behind every zone's *Rain Yearly [L]*) without waiting for 1 January. The total is a saved value that survives a restart and a plain reinstall, so this button is the way to clear a wrong figure — e.g. after switching rain sensor type.
  - **Reset yearly water** — clears *Irrigated Yearly [L]* for every zone at once; each zone's lifetime total is preserved.
  - Both are state-only: recorder long-term statistics (Energy dashboard) are left untouched.
- Per-zone **site exposure**: a microclimate factor (`k_mc`) that multiplies the crop coefficient, so a shaded, windy or paving-adjacent zone keeps its seasonal Kc curve instead of being frozen at one value by a constant Kc override ([#146], contributed by @philipgiuliani — the first outside code change NeverDry has shipped). Presets from the landscape coefficient method (0.60 deep shade … 1.20 reflected heat), plus an *Advanced (custom factor)* entry (0.1–1.5). Default *Full sun, open* (×1.00) leaves existing zones unchanged.
- Zone Kc sensor attributes `kc_base`, `exposure` and `microclimate_factor`, so an effective Kc can be traced back to the curve and the factor it came from.

### Fixed
- **Yearly rain stayed at zero for anyone using a soil-moisture probe** ([#144]). Rain is a system-wide quantity — one sky over the whole garden — but the credit lived inside the ET branch of the calculation, and a VWC setup bypasses that branch entirely. Every zone's *Rain Yearly [L]* therefore read 0 forever, with the rain sensor tracked, firing and correct. The credit now runs in both modes. ET behaviour is unchanged, and rain is still not subtracted from a VWC deficit — the probe already reflects it, because the water landed on the soil the probe is in.
- **An override set by accident could not be cleared** ([#165]). Emptying a zone's efficiency, manual Kc, exposure, microclimate factor, delivery timeout or irrigation time silently restored the previous value: the form re-injected its own default whenever a field came back empty. Efficiency had a second, separate reason — it was a slider, and a slider always submits a number, so it had no empty state to send at all. It is a box now, like the Kc field beside it. Fields that must always hold a value keep their default.
- **A partial delivery on 1 January no longer adds to last year's total.** *Irrigated Yearly [L]* is reset when the calendar year turns, but only the full-delivery path did so: a session that stopped short of its target — a stop, a timeout, a valve that closed early — carried the previous year's figure forward instead. The counter feeds long-term statistics, so the jump reached the Energy dashboard too. Both settle paths now share one answer.
- **Reloading no longer leaves the previous setup running underneath the new one.** Saving anything in the options flow reloads the integration, and until now the reload left the old copy subscribed: the retired hub kept waking on every temperature reading and advancing a second water balance, and each valve gained another operator — each with its own watchdog, able to force a valve closed under the one legitimately driving it. The count grew with every edit. Nothing was visibly broken, which is why it lasted: the symptoms are drifting deficits and valves that close early for no reason the log explains. Every listener is now released when the entity or the controller goes away.
- Soil-moisture probes reporting a **percentage** no longer stop a zone from watering ([#170]). A reading of 45 rather than 0.45 made `(field_capacity − vwc)` negative for every possible value — including a bone-dry 15 % — and the clamp that keeps a deficit from going negative pinned it at exactly zero, silently and forever. Readings are now normalised at the input boundary: above 1 is read as a percentage, exactly 1.0 as a saturated fraction. Consumer probes (Ecowitt, most Zigbee models) work without a template-sensor helper.
- A moisture reading that is not a water content on either scale — a raw ADC count, a negative, a NaN — is now refused instead of being clamped to "saturated": the last good deficit is held and one warning is logged, naming the sensor.
- Zone form fields showed their raw key instead of a label — `area_m2`, `flow_rate_lpm`, `plant_family` and every other field inside the three collapsible sections, in all languages including translated ones. Grouping the form into sections moved where Home Assistant looks a label up (`sections.<section>.data.<field>`), and the labels stayed at the step level where nothing reads them. Fields whose key happens to read like a word (`valve`, `name`) hid how widespread it was. The strings themselves were always there and are unchanged.
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

[Unreleased]: https://github.com/never-dry/NeverDry/compare/v0.11.1...HEAD
[0.11.1]: https://github.com/never-dry/NeverDry/releases/tag/v0.11.1
[0.11.0]: https://github.com/never-dry/NeverDry/releases/tag/v0.11.0
[#173]: https://github.com/never-dry/NeverDry/issues/173
[#94]: https://github.com/never-dry/NeverDry/issues/94
[#196]: https://github.com/never-dry/NeverDry/issues/196
[#168]: https://github.com/never-dry/NeverDry/pull/168
[#165]: https://github.com/never-dry/NeverDry/issues/165
[#144]: https://github.com/never-dry/NeverDry/issues/144
[#170]: https://github.com/never-dry/NeverDry/issues/170
[#146]: https://github.com/never-dry/NeverDry/issues/146
[#142]: https://github.com/never-dry/NeverDry/pull/142
[#139]: https://github.com/never-dry/NeverDry/issues/139
[#123]: https://github.com/never-dry/NeverDry/issues/123
[#116]: https://github.com/never-dry/NeverDry/issues/116
