# Translations

NeverDry ships its interface text in **two** places, and a language is not
done until both are.

1. **The integration catalogue**, under `custom_components/never_dry/translations/`.
   Home Assistant picks the file matching the user's language and falls back to
   English when there is none. This covers the configuration forms, the errors,
   the notifications, the entity names and the repairs.
2. **The zone card's own dictionary**, inside
   `custom_components/never_dry/www/never-dry-zone-card.js`. A Lovelace card is
   frontend code and cannot read the integration's translation files, so it
   carries its own. This covers everything written on the card.

They are separate files with separate keys, and translating one leaves half the
product in English. Both are listed per language in the table below for that
reason: "Spanish: done" is a sentence that could mean either half, so the table
does not let anyone write it.

## What ships, and who checked it

| Language | Catalogue | Card | Read back |
|---|---|---|---|
| English | source | source | n/a, every string originates here |
| Italian | complete | complete | **against the running product**, 2026-09-13 |
| German | complete | complete | **against the running product**, 2026-09-17, by @MetheLittle |
| Spanish | complete | complete | **against the running product** for the configuration and options forms and the entity names, 2026-09-21, and the zone card, 2026-09-26, by @laurash96 (native speaker, Colombia); notifications, repairs, services and the model card read in the file only (the model card finds no entity on a Spanish install, #279). Five notification strings added 2026-09-26 were read in the file that day, with two points still open in #215 |

**Read back** is the column that matters, and it is deliberately not a yes or a
no. There are two different verifications behind that word and they do not catch
the same things:

- **File only** means somebody who speaks the language read the strings against
  the English. That catches wrong words. It does not catch two labels that are
  each correct and, side by side on a screen, say the same thing.
- **Against the running product** means somebody installed it, used it, and read
  the labels where they appear. That is the one that catches the rest.

The distinction is not theoretical. The Italian was "human checked" for months
while the card called a valve that had never spoken and a valve that had stopped
answering by the same name, and showed `open (verified)` as a tick. Both were
found in seconds from a screenshot, by people who had already read the file
carefully. The same two defects had been copied into Spanish before anyone
noticed.

A machine translation nobody has read is indistinguishable from a correct one
until it is in front of a user, so it ships **labelled**, in its own row, rather
than leaving the reader to assume. An unchecked translation is better than no
translation, but only when it says so.

## Contributing a language

The shortest path, and the one that credits you automatically:

1. Copy `custom_components/never_dry/translations/en.json` to `<code>.json`,
   using the Home Assistant language code (`de`, `fr`, `nl`, and so on).
2. Translate the **values**. Leave every key untouched, and leave the
   `{placeholders}` in braces exactly as they are: they are filled in at
   runtime with names, numbers and units.
3. **Add your language to the card's dictionary too**, in
   `custom_components/never_dry/www/never-dry-zone-card.js`. Copy the `en`
   block, keep the keys, translate the values. Skipping this leaves the card
   in English and the language only half done.
4. Open a pull request. Your commits carry your authorship, so GitHub records
   the contribution without anyone having to remember to.

Either half on its own is a welcome contribution and will be merged. It just
does not make the language complete, and the table above will say which half is
missing until the other arrives.

If a pull request is inconvenient, open an issue with the file attached and it
will be added for you, but say so, because the commit will then be authored by
the maintainer and your name has to be entered by hand in the contributors list.

### What to watch for while translating

- **Labels are names, not explanations.** The label names the field; the
  explanation belongs in `data_description` beside it. A label long enough to be
  a sentence is a mistake: there is a test that fails on it.
- **Never write an identifier.** Values like `estimated_flow` are internal keys
  that have their own translated labels; naming one in a message shows the user
  the machinery. There is a test for this too.
- **Units belong to the reader.** Depths are millimetres and flows litres per
  hour in metric, inches and gallons per hour in imperial. The form does the
  conversion; the text only has to name the right one.

Both tests live in `tests/test_translation_consistency.py`, and they run against
every language file, so a new one is held to the same rules as the ones already
here.
