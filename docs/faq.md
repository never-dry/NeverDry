# Frequently asked questions

**Status:** open register, started 2026-10-25. English only.

This page collects the questions people actually ask, on the forums, in issues and
in comments, with the best answer the project can give today. Each entry says
when the question arrived. When an answer depends on hardware, it points to the
[compatibility register](valve-compatibility.md), which records what has really
been tested rather than what should work in theory.

Asked something that is not here? Open an
[issue](https://github.com/never-dry/NeverDry/issues/new/choose).

## Contents

- [Hardware and controllers](#hardware-and-controllers)
  - [Can I use the ESPHome Sprinkler controller?](#can-i-use-the-esphome-sprinkler-controller)
  - [Can I use a Rachio controller?](#can-i-use-a-rachio-controller)

## Hardware and controllers

NeverDry never talks to your hardware: it commands Home Assistant **entities**.
Any controller whose valves appear in Home Assistant as `switch.*` or `valve.*`
can be driven. What changes from one controller to another is whether it reports
the water actually delivered, and whether it insists on timing the run itself.

### Can I use the ESPHome Sprinkler controller?

*Asked 2026-10.*

Yes. One user runs NeverDry on an ESPHome "Sprinkler" board with 24 V valves
(see the [compatibility register](valve-compatibility.md)). Pick each valve's
`switch.*` entity directly in the zone form.

You don't need a script or an ESPHome queue, and you shouldn't build one.
NeverDry decides when a zone opens and when it closes. If the controller also
sequences or times the valves, the two schedulers fight, and a run ends when
the board decides rather than when NeverDry does. Turn the controller's own
sequencing off, or leave the valves out of it. The zone field accepts only
`switch` and `valve` entities, not scripts.

Out of the box nothing reports the water actually delivered, so NeverDry runs
these valves on a clock, from the flow rate you declare. If you add a flow
meter to the board, the zone form can read it: either a cumulative volume
counter or an instantaneous flow rate works.

### Can I use a Rachio controller?

*Asked 2026-10.*

Probably, but nobody has verified it yet. The official Home Assistant
[Rachio integration](https://www.home-assistant.io/integrations/rachio/) creates
a `switch.*` for every enabled zone, and NeverDry can command any switch. Three
things to know before relying on it:

- **Rachio holds its own run time.** Turning a zone switch on runs it for the
  duration set in the Rachio integration options, and then Rachio closes it.
  Set that duration longer than NeverDry's longest run, or the zone will stop
  early.
- **Disable Rachio's own schedules** for zones NeverDry manages, or both will
  water.
- **It is cloud based.** Home Assistant must be reachable from the internet for
  state updates, so confirmations can lag. No volume sensor is exposed, so
  NeverDry runs it on a clock.

If you try it, please add a row to the
[compatibility register](valve-compatibility.md).
