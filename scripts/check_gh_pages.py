#!/usr/bin/env python3
"""check_gh_pages.py - refuse to publish a site that contradicts itself.

The published pages make a set of promises to machines that no test covers: that
every language a page advertises is a page that exists, that each translation
claims itself as canonical rather than handing its ranking to English, that the
sitemap lists only addresses which answer with their own content, and that the
one declaration of which languages the site serves matches what is on disk.

None of that fails loudly. A dead hreflang, a canonical pointing at the wrong
variant or a sitemap entry aimed at a redirect all render a page that looks
perfect and quietly costs it the index. The failure surfaces weeks later as
traffic that never arrived, which is not a signal anyone can act on.

So it is checked here, and this script exits non-zero when it finds a problem:
the deploy stops and the broken state does not reach anybody. A check that
prints and lets the build continue is a check nobody reads.

Run from the repository root:

    python3 scripts/check_gh_pages.py
"""

from __future__ import annotations

import json
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGES = ROOT / "docs" / "gh-pages"
SITE = "https://never-dry.github.io/NeverDry"
BASE = "/NeverDry"

# Anything the browser is told to fetch, minus the query and fragment.
REF = re.compile(r'(?:href|src)="([^"]+)"')
CANONICAL = re.compile(r'<link rel="canonical" href="([^"]+)">')
ALTERNATE = re.compile(r'<link rel="alternate" hreflang="([^"]+)" href="([^"]+)">')
HTML_LANG = re.compile(r'<html lang="([^"]+)"')
ROBOTS = re.compile(r'<meta name="robots" content="([^"]*)"')
REFRESH = re.compile(r'<meta http-equiv="refresh"', re.IGNORECASE)
LOC = re.compile(r"<loc>([^<]+)</loc>")
LASTMOD = re.compile(r"<lastmod>([^<]*)</lastmod>")
JSON_LD = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)
LANG_ENTRY = re.compile(r"\{\s*code:\s*'([^']+)',\s*name:\s*'([^']*)'\s*\}")
PLACEHOLDER = re.compile(r"\{\{[A-Z_]+\}\}")
TEXT_ELEMENT = re.compile(r"<(h1|h2|h3|p)\b[^>]*>(.*?)</\1>", re.S)
TAGS = re.compile(r"<[^>]+>")
SECTION = re.compile(r"<section\b([^>]*)>(.*?)</section>", re.S)
HEADING = re.compile(r"<h[23]\b[^>]*>(.*?)</h[23]>", re.S)
# Shorter than this, an identical string is more often a name than an omission.
MIN_TRANSLATABLE = 25


class Report:
    """Collected complaints, so one run names every problem it can see."""

    def __init__(self) -> None:
        self.problems: list[str] = []

    def fail(self, where: Path | str, what: str) -> None:
        name = where.relative_to(PAGES) if isinstance(where, Path) else where
        self.problems.append(f"{name}: {what}")


def local_path(url: str) -> Path | None:
    """Where an address lands on disk, or None when it leaves this site."""
    path = url
    if path == SITE or path.startswith(SITE + "/"):
        # Same site. The host alone is not enough to conclude that: the project
        # publishes a second site (the planner) under the same github.io host,
        # and treating its addresses as local files would report every link to
        # it as dead.
        path = path[len(SITE) :]
    elif path.startswith(("http://", "https://", "mailto:", "#", "data:")):
        return None
    path = path.split("#")[0].split("?")[0]
    if path.startswith(BASE):
        path = path[len(BASE) :]
    if not path.startswith("/"):
        return None  # relative link: resolved against its own page, checked there
    path = path.lstrip("/")
    target = PAGES / path if path else PAGES
    return target / "index.html" if target.is_dir() or path.endswith("/") or not path else target


def declared_languages() -> dict[str, str]:
    source = (PAGES / "languages.js").read_text(encoding="utf-8")
    return dict(LANG_ENTRY.findall(source))


def is_redirect(text: str) -> bool:
    return bool(REFRESH.search(text))


def check_page(page: Path, text: str, languages: dict[str, str], report: Report) -> None:
    leftovers = PLACEHOLDER.findall(text)
    if leftovers:
        report.fail(page, f"unfilled placeholder {sorted(set(leftovers))} - was the site stamped?")

    for ref in REF.findall(text):
        target = local_path(ref)
        if target is None:
            if ref.startswith(("http", "mailto:", "#", "data:")):
                continue
            target = (page.parent / ref).resolve()
        if not target.exists():
            report.fail(page, f"dead link: {ref}")

    canonical = CANONICAL.search(text)
    alternates = ALTERNATE.findall(text)

    if is_redirect(text):
        robots = ROBOTS.search(text)
        if not robots or "noindex" not in robots.group(1):
            report.fail(page, "redirect page is not noindex: it competes with the page it points at")
        if alternates:
            report.fail(page, "redirect page declares hreflang alternates")
        return

    if not canonical:
        return  # the 404 has none on purpose

    target = local_path(canonical.group(1))
    if target != page:
        report.fail(page, f"canonical points at {canonical.group(1)}, which is not this page")

    if alternates:
        declared = {tag.lower() for tag, _ in alternates if tag.lower() != "x-default"}
        if declared != set(languages):
            missing = sorted(set(languages) - declared)
            extra = sorted(declared - set(languages))
            report.fail(page, f"hreflang set does not match languages.js (missing {missing}, extra {extra})")
        for tag, href in alternates:
            alt_target = local_path(href)
            if alt_target is None or not alt_target.exists():
                report.fail(page, f"hreflang {tag} points at {href}, which does not exist")
            elif alt_target.exists() and is_redirect(alt_target.read_text(encoding="utf-8")):
                report.fail(page, f"hreflang {tag} points at a redirect: {href}")
        if not any(tag.lower() == "x-default" for tag, _ in alternates):
            report.fail(page, "no hreflang=x-default: nothing declares which language is the default")

    lang = HTML_LANG.search(text)
    if lang:
        expected = page.parent.name.lower() if page.parent != PAGES else "en"
        if lang.group(1).lower() != expected:
            report.fail(page, f'<html lang="{lang.group(1)}"> does not match its location (/{expected}/)')


def check_sitemap(languages: dict[str, str], report: Report) -> None:
    sitemap = PAGES / "sitemap.xml"
    if not sitemap.exists():
        report.fail("sitemap.xml", "missing")
        return
    locs = LOC.findall(sitemap.read_text(encoding="utf-8"))
    for loc in locs:
        target = local_path(loc)
        if target is None or not target.exists():
            report.fail("sitemap.xml", f"lists {loc}, which does not exist")
            continue
        text = target.read_text(encoding="utf-8")
        if is_redirect(text):
            report.fail("sitemap.xml", f"lists {loc}, which is a redirect")
        robots = ROBOTS.search(text)
        if robots and "noindex" in robots.group(1):
            report.fail("sitemap.xml", f"lists {loc}, which is noindex")
    for code in languages:
        expected = f"{SITE}/" if code == "en" else f"{SITE}/{code}/"
        if expected not in locs:
            report.fail("sitemap.xml", f"does not list the {code} page ({expected})")


def check_lastmod(report: Report) -> None:
    """Refuse a sitemap date that is not a real calendar date, or one in the future.

    The dates are stamped from git at deploy time; a malformed or future one means
    the stamping went wrong, and a search engine that meets a wrong lastmod stops
    trusting every other one on the site.
    """
    sitemap = PAGES / "sitemap.xml"
    if not sitemap.exists():
        return
    for value in LASTMOD.findall(sitemap.read_text(encoding="utf-8")):
        try:
            day = date.fromisoformat(value)
        except ValueError:
            report.fail("sitemap.xml", f"lastmod {value!r} is not a YYYY-MM-DD date")
            continue
        if day > date.today():
            report.fail("sitemap.xml", f"lastmod {value} is in the future")


def check_structured_data(report: Report) -> None:
    """Refuse JSON-LD that does not parse, and a FAQPage that has lost questions.

    Structured data is invisible on the page: a missing comma leaves the page
    looking perfect while every search engine and AI crawler silently discards
    the block. The FAQ's block is generated from its h3 headings, so the two
    counts must agree; a difference means the generator misread the page.
    """
    for page in sorted(PAGES.rglob("*.html")):
        text = page.read_text(encoding="utf-8")
        for block in JSON_LD.findall(text):
            try:
                document = json.loads(block)
            except json.JSONDecodeError as error:
                report.fail(page, f"JSON-LD does not parse: {error}")
                continue
            if isinstance(document, dict) and document.get("@type") == "FAQPage":
                main = text[text.find("<main") :]
                questions = main.count("<h3")
                listed = len(document.get("mainEntity", []))
                if listed != questions:
                    report.fail(page, f"FAQPage lists {listed} questions, the page has {questions}")
                empty = [
                    entry.get("name", "?")
                    for entry in document.get("mainEntity", [])
                    if not entry.get("acceptedAnswer", {}).get("text")
                ]
                if empty:
                    report.fail(page, f"FAQPage questions without an answer: {empty}")


# Everything the deploy uploads is world-readable the moment it lands. The
# upload takes a whole directory, so a file that ends up in it is published
# whether or not anyone meant it to be: a draft saved in the wrong place, a data
# export, an environment file. These are the shapes a site is allowed to have.
PUBLISHABLE = {
    ".html",
    ".css",
    ".js",
    ".json",
    ".txt",
    ".xml",
    ".md",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".svg",
    ".ico",
    ".woff2",
}
NEVER_PUBLISH = (
    ".env",
    ".py",
    ".sh",
    ".csv",
    ".db",
    ".sqlite",
    ".zip",
    ".pem",
    ".key",
    ".p8",
    ".bak",
    ".log",
    ".yml",
    ".yaml",
)


def check_shipped_files(report: Report) -> None:
    """Name every file the deploy would upload, and object to the odd ones.

    Not hypothetical: this site is uploaded by pointing an action at a directory,
    and nobody reviews a directory listing before a deploy. The check is cheap
    and the failure it prevents is unrecoverable, because a file that has been
    public for an hour has been public.
    """
    for item in sorted(PAGES.rglob("*")):
        if item.is_dir():
            continue
        suffix = item.suffix.lower()
        if item.name.startswith("."):
            report.fail(item, "hidden file in the published tree")
        elif suffix in NEVER_PUBLISH:
            report.fail(item, f"a {suffix} file would be published as-is")
        elif suffix not in PUBLISHABLE:
            report.fail(item, f"unexpected file type {suffix or '(none)'} in the published tree")


def check_languages(languages: dict[str, str], report: Report) -> None:
    if not languages:
        report.fail("languages.js", "declares no languages")
        return
    if "en" not in languages:
        report.fail("languages.js", "English is missing: it is the one variant that cannot be absent")
    for code in languages:
        page = PAGES / "index.html" if code == "en" else PAGES / code / "index.html"
        if not page.exists():
            report.fail("languages.js", f"declares {code}, but {page.relative_to(PAGES)} does not exist")
    for directory in sorted(p for p in PAGES.iterdir() if p.is_dir()):
        if (directory / "index.html").exists() and directory.name not in languages:
            report.fail("languages.js", f"{directory.name}/ is published but not declared: nothing links to it")


def visible_texts(html: str) -> list[str]:
    """The headings and paragraphs a reader actually sees, stripped of markup."""
    found = []
    for match in TEXT_ELEMENT.finditer(html):
        text = re.sub(r"\s+", " ", TAGS.sub("", match.group(2))).strip()
        if len(text) >= MIN_TRANSLATABLE:
            found.append(text)
    return found


def check_untranslated_text(languages: dict[str, str], report: Report) -> None:
    """Refuse a translated page that still carries English prose.

    A section added to the English page after the last translation pass does not
    break anything: it renders, it validates, every other check here stays green,
    and it reads as English in the middle of a German page. That is exactly how
    the Zone Card section sat untranslated in eight languages at once, found by a
    reader rather than by a check.

    Identity with the English page is the signal. A translator can legitimately
    leave a product name or a short label alone, so only prose of some length
    counts: below that threshold the match is more likely a coincidence than an
    omission.
    """
    english = set(visible_texts((PAGES / "index.html").read_text(encoding="utf-8")))
    for code in sorted(languages):
        if code == "en":
            continue
        page = PAGES / code / "index.html"
        if not page.exists():
            continue
        text = page.read_text(encoding="utf-8")
        if is_redirect(text):
            continue
        for shared in visible_texts(text):
            if shared in english:
                report.fail(page, f'still in English: "{shared[:60]}..."')


def skeleton(html: str) -> list[tuple[str, int, int, int]]:
    """Each section as (first heading, h2 count, h3 count, feature cards), in page order.

    The counts are what a translation cannot change without changing the content:
    a card added in English, or a section that exists only there, shows up as a
    row that differs. Sections marked ``data-en-only`` (English on purpose) and
    ``data-translation-notice`` (only a translation has one) are left out.
    """
    rows = []
    for match in SECTION.finditer(html):
        if "data-en-only" in match.group(1) or "data-translation-notice" in match.group(1):
            continue
        body = match.group(2)
        heading = HEADING.search(body)
        title = re.sub(r"\s+", " ", TAGS.sub("", heading.group(1))).strip() if heading else ""
        rows.append((title, body.count("<h2"), body.count("<h3"), body.count('class="feature-card"')))
    return rows


def check_structure(languages: dict[str, str], report: Report) -> None:
    """Refuse a translated page whose sections no longer match the English ones.

    check_untranslated_text sees English prose left inside a translation. It is
    blind to the opposite case, content the English page gained that the
    translation never received: there is nothing English on the translated page
    to match. That is how the features section was rewritten in English in June
    2026 and stayed in its old form in eight languages for four months, until a
    translator noticed the two pages did not say the same thing.
    """
    english = skeleton((PAGES / "index.html").read_text(encoding="utf-8"))
    for code in sorted(languages):
        if code == "en":
            continue
        page = PAGES / code / "index.html"
        if not page.exists():
            continue
        text = page.read_text(encoding="utf-8")
        if is_redirect(text):
            continue
        translated = skeleton(text)
        if len(translated) != len(english):
            report.fail(page, f"has {len(translated)} sections, the English page has {len(english)}")
            continue
        for position, (ours, theirs) in enumerate(zip(english, translated, strict=True), start=1):
            if ours[1:] != theirs[1:]:
                report.fail(
                    page,
                    f'section {position} ("{ours[0]}" in English) has h2/h3/cards {theirs[1:]}, English has {ours[1:]}',
                )


def main() -> int:
    report = Report()
    languages = declared_languages()
    check_languages(languages, report)

    for page in sorted(PAGES.rglob("*.html")):
        check_page(page, page.read_text(encoding="utf-8"), languages, report)

    check_untranslated_text(languages, report)
    check_structure(languages, report)
    check_sitemap(languages, report)
    check_lastmod(report)
    check_structured_data(report)
    check_shipped_files(report)

    if report.problems:
        print(f"{len(report.problems)} problem(s) found in the published pages:\n", file=sys.stderr)
        for problem in report.problems:
            print(f"  - {problem}", file=sys.stderr)
        print("\nNot deploying. Every one of these is invisible on the rendered page.", file=sys.stderr)
        return 1

    pages = sum(1 for _ in PAGES.rglob("*.html"))
    print(f"published pages consistent: {pages} pages, {len(languages)} languages declared")
    return 0


if __name__ == "__main__":
    sys.exit(main())
