#!/usr/bin/env python3
"""stamp_gh_pages.py - fill in the version and date the published pages show.

Every page carries a line saying which version of NeverDry it describes and
when the site was last published. The checklist for a published page asks for
it, and the reason is not bookkeeping: a landing page with no date is one a
visitor cannot date, and a reader who cannot date a page cannot tell a current
project from an abandoned one.

Writing those two values by hand would guarantee they go stale, because they
change on a cadence nobody edits the site on. So the pages carry placeholders
and this script fills them in at deploy time, from the two places that already
know the answer: the integration manifest for the version, and the commit being
deployed for the date.

The date is the commit's, not today's. The badge refresh runs on a weekly
schedule and redeploys the same content; stamping "today" then would announce an
update that did not happen, which is the same lie as no date at all.

Two more things are filled in here for the same reason, because each would go
stale if typed by hand. The sitemap gets a <lastmod> per page, the date of the
last commit that touched that page: search engines use it to decide when to come
back, and ignore it for good once it proves unreliable. And the FAQ gets its
FAQPage structured data, built from the questions on the page itself, so the
copy that machines read cannot drift from the one people read.

Run from the repository root, before the site is uploaded:

    python3 scripts/stamp_gh_pages.py
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from datetime import date
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGES = ROOT / "docs" / "gh-pages"
MANIFEST = ROOT / "custom_components" / "never_dry" / "manifest.json"

SITEMAP = PAGES / "sitemap.xml"
FAQ = PAGES / "faq.html"
SITE = "https://never-dry.github.io/NeverDry"

PLACEHOLDERS = ("{{SITE_VERSION}}", "{{SITE_UPDATED}}")
FAQ_PLACEHOLDER = "{{FAQ_STRUCTURED_DATA}}"
BLOCK_TAGS = ("p", "li", "td", "th", "tr", "ul", "ol", "table", "pre", "blockquote")
LOC = re.compile(r"(\s*)<loc>([^<]+)</loc>")


def integration_version() -> str:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))["version"]


def git_output(*arguments: str) -> str | None:
    """Run one read-only git command in the repository and return its trimmed output.

    Exists so that every git question this script asks fails the same way: any
    reason git cannot answer (absent, no repository, too slow) becomes None, and
    each caller decides what a missing answer is worth.

    Args:
        arguments: the git subcommand and its options, passed as argv, never a shell string.

    Returns:
        The standard output without surrounding whitespace, or None when git is
        unavailable, fails, or prints nothing.
    """
    git = shutil.which("git")
    if not git:
        return None
    try:
        out = subprocess.run(  # noqa: S603 - resolved path, fixed argv, no input from anywhere
            [git, *arguments],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        # Swallowed on purpose, and this is the whole of the handling: every way
        # git can fail here has the same answer, which is the caller's fallback.
        # Failing the deploy instead would take the site down to protect a date.
        return None
    return out.stdout.strip() or None


def published_on() -> str:
    """The date of the commit being deployed, falling back to today.

    A shallow checkout still has HEAD, which is all this needs. If git is not
    available at all the fallback is today's date: less accurate, but a date
    that is a day or two off is worth more to a reader than no date, and the
    check that follows would rather see a plausible value than a placeholder.
    """
    return git_output("log", "-1", "--format=%cs") or date.today().isoformat()


def page_for(url: str) -> Path | None:
    """The file in the published tree that answers at a site address.

    Args:
        url: an absolute address under SITE, as the sitemap lists it.

    Returns:
        The index.html of a directory address or the named file, or None for an
        address outside this site.
    """
    if not url.startswith(f"{SITE}/"):
        return None
    relative = url[len(SITE) + 1 :]
    return PAGES / relative / "index.html" if relative == "" or relative.endswith("/") else PAGES / relative


def stamp_sitemap_lastmod() -> int:
    """Give every sitemap entry the date its page last changed in git.

    A shallow checkout is refused rather than trusted: there the one commit
    present appears to have created every file, so every page would carry the
    deploy date, and a lastmod that always says "today" is the kind search
    engines learn to ignore. Without history the entries ship with no lastmod,
    which is allowed and costs nothing; a page whose date git cannot tell is
    left without one for the same reason.

    Returns:
        The number of entries that received a date.
    """
    if not SITEMAP.exists():
        return 0
    if git_output("rev-parse", "--is-shallow-repository") != "false":
        print("shallow or missing git history: sitemap left without <lastmod>", file=sys.stderr)
        return 0
    text = SITEMAP.read_text(encoding="utf-8")
    if "<lastmod>" in text:
        return 0  # already stamped: a second run must not add a second date

    stamped = 0

    def add_lastmod(match: re.Match[str]) -> str:
        nonlocal stamped
        indent, url = match.group(1), match.group(2)
        page = page_for(url)
        changed = (
            git_output("log", "-1", "--format=%cs", "--", str(page.relative_to(ROOT)))
            if page and page.exists()
            else None
        )
        if not changed:
            return match.group(0)
        stamped += 1
        return f"{match.group(0)}{indent}<lastmod>{changed}</lastmod>"

    SITEMAP.write_text(LOC.sub(add_lastmod, text), encoding="utf-8")
    return stamped


class FaqReader(HTMLParser):
    """Collect each question of the FAQ page with the text of its answer.

    A question is an h3 inside <main>; its answer is everything after it up to the
    next h2 or h3, minus the "Asked YYYY-MM" line, which dates the question and
    is not part of what it says.

    Attributes:
        entries: (question, answer) pairs in page order, whitespace collapsed.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.entries: list[tuple[str, str]] = []
        self._in_main = False
        self._in_question = False
        self._skip_depth = 0
        self._question: list[str] = []
        self._answer: list[str] | None = None

    def _close_entry(self) -> None:
        if self._answer is not None:
            question = " ".join("".join(self._question).split())
            answer = " ".join("".join(self._answer).split())
            self.entries.append((question, answer))
        self._answer = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "main":
            self._in_main = True
        if not self._in_main:
            return
        if tag in ("h2", "h3"):
            self._close_entry()
            if tag == "h3":
                self._in_question = True
                self._question = []
        elif self._skip_depth or (tag == "p" and ("class", "asked") in attrs):
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "main" and self._in_main:
            self._close_entry()
            self._in_main = False
        elif tag == "h3" and self._in_question:
            self._in_question = False
            self._answer = []
        elif self._skip_depth and tag not in ("h2", "h3"):
            self._skip_depth -= 1
        elif self._answer is not None and tag in BLOCK_TAGS:
            # Text inside one paragraph arrives in pieces around inline tags and is
            # joined as is; only the end of a block is a word boundary.
            self._answer.append(" ")

    def handle_data(self, data: str) -> None:
        if self._in_question:
            self._question.append(data)
        elif self._answer is not None and not self._skip_depth:
            self._answer.append(data)


def faq_structured_data(page_html: str) -> str:
    """The FAQPage JSON-LD for a FAQ page, ready to sit inside a script element.

    Args:
        page_html: the full markup of faq.html, already stamped.

    Returns:
        Indented JSON with "</" escaped, so an answer can never close the script element early.
    """
    reader = FaqReader()
    reader.feed(page_html)
    document = {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "url": f"{SITE}/faq.html",
        "inLanguage": "en",
        "mainEntity": [
            {"@type": "Question", "name": question, "acceptedAnswer": {"@type": "Answer", "text": answer}}
            for question, answer in reader.entries
        ],
    }
    body = json.dumps(document, ensure_ascii=False, indent=2).replace("</", "<\\/")
    return "\n".join("    " + line if index else line for index, line in enumerate(body.splitlines()))


def stamp_faq() -> int:
    """Replace the FAQ placeholder with structured data built from the page.

    Returns:
        The number of questions written, 0 when the page or its placeholder is absent.
    """
    if not FAQ.exists():
        return 0
    text = FAQ.read_text(encoding="utf-8")
    if FAQ_PLACEHOLDER not in text:
        return 0
    data = faq_structured_data(text)
    FAQ.write_text(text.replace(FAQ_PLACEHOLDER, data), encoding="utf-8")
    return data.count('"@type": "Question"')


def main() -> int:
    version = integration_version()
    updated = published_on()
    values = {"{{SITE_VERSION}}": version, "{{SITE_UPDATED}}": updated}

    touched = 0
    filled = 0
    for page in sorted(PAGES.rglob("*.html")):
        text = page.read_text(encoding="utf-8")
        if not any(p in text for p in PLACEHOLDERS):
            continue
        for placeholder, value in values.items():
            filled += text.count(placeholder)
            text = text.replace(placeholder, value)
        page.write_text(text, encoding="utf-8")
        touched += 1

    print(f"stamped v{version} / {updated} into {touched} pages ({filled} placeholders)")
    print(f"sitemap: {stamp_sitemap_lastmod()} entries dated; FAQ: {stamp_faq()} questions as structured data")
    if not touched:
        print("no page carried a placeholder: nothing to stamp", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
