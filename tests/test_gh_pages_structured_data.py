"""The landing's machine-readable layer: sitemap dates and structured data.

None of it shows on a rendered page, so none of it can be caught by looking at
the site. The FAQ's structured data is generated from the page at deploy time;
these tests pin that the generator reads every question and only the answer,
and that the deploy check refuses the broken states it exists for.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load(name: str):
    """Import one deploy script as a module, since scripts/ is not a package.

    Args:
        name: the file name in scripts/ without the .py suffix.
    """
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


stamp = _load("stamp_gh_pages")
check = _load("check_gh_pages")

FAQ_PAGE = """<html><head><script type="application/ld+json">{{FAQ_STRUCTURED_DATA}}</script></head><body>
<main class="doc">
    <p>Intro that is not an answer.</p>
    <h2 id="a">Section</h2>
    <h3 id="q1">First question?</h3>
    <p class="asked">Asked 2026-06.</p>
    <p>First answer with a <a href="#">link</a>.</p>
    <ul><li>one point</li><li>another point</li></ul>
    <h3 id="q2">Second question?</h3>
    <p class="asked">Asked 2026-07.</p>
    <p>Second answer &amp; more.</p>
    <h2 id="b">Next section</h2>
    <h3 id="q3">Third question?</h3>
    <p>Third answer.</p>
</main>
<footer><p>Not part of any answer.</p></footer>
</body></html>"""


def _entries(page: str) -> list[tuple[str, str]]:
    document = json.loads(stamp.faq_structured_data(page))
    return [(entry["name"], entry["acceptedAnswer"]["text"]) for entry in document["mainEntity"]]


def test_every_question_is_read_with_its_whole_answer():
    assert _entries(FAQ_PAGE) == [
        ("First question?", "First answer with a link. one point another point"),
        ("Second question?", "Second answer & more."),
        ("Third question?", "Third answer."),
    ]


def test_the_asked_line_and_the_footer_stay_out_of_the_answers():
    answers = " ".join(answer for _, answer in _entries(FAQ_PAGE))
    assert "Asked" not in answers
    assert "Not part" not in answers
    assert "Intro" not in answers


def test_an_answer_cannot_close_the_script_element():
    page = FAQ_PAGE.replace("Third answer.", "Use &lt;/script&gt; carefully.")
    assert "</script" not in stamp.faq_structured_data(page)


def test_the_real_faq_yields_one_entry_per_question():
    page = (check.PAGES / "faq.html").read_text(encoding="utf-8")
    main = page[page.find("<main") :]
    assert len(_entries(page)) == main.count("<h3") > 0


def test_every_landing_page_carries_parseable_structured_data():
    pages = [check.PAGES / "index.html", *sorted(check.PAGES.glob("*/index.html"))]
    for page in pages:
        blocks = check.JSON_LD.findall(page.read_text(encoding="utf-8"))
        assert blocks, page
        types = {node["@type"] for node in json.loads(blocks[0])["@graph"]}
        assert types == {"WebSite", "Organization", "SoftwareApplication"}, page


def test_a_sitemap_address_maps_to_the_file_that_answers_it():
    assert stamp.page_for(f"{stamp.SITE}/") == stamp.PAGES / "index.html"
    assert stamp.page_for(f"{stamp.SITE}/it/") == stamp.PAGES / "it" / "index.html"
    assert stamp.page_for(f"{stamp.SITE}/faq.html") == stamp.PAGES / "faq.html"
    assert stamp.page_for("https://example.org/") is None


def test_a_wrong_lastmod_is_refused(tmp_path, monkeypatch):
    (tmp_path / "sitemap.xml").write_text(
        "<urlset><url><loc>x</loc><lastmod>2026-13-01</lastmod></url><url><loc>y</loc><lastmod>2999-01-01</lastmod></url></urlset>",
        encoding="utf-8",
    )
    monkeypatch.setattr(check, "PAGES", tmp_path)
    report = check.Report()
    check.check_lastmod(report)
    assert len(report.problems) == 2


def test_a_faq_that_lost_a_question_is_refused(tmp_path, monkeypatch):
    document = json.loads(stamp.faq_structured_data(FAQ_PAGE))
    document["mainEntity"].pop()
    page = FAQ_PAGE.replace("{{FAQ_STRUCTURED_DATA}}", json.dumps(document))
    (tmp_path / "faq.html").write_text(page, encoding="utf-8")
    monkeypatch.setattr(check, "PAGES", tmp_path)
    report = check.Report()
    check.check_structured_data(report)
    assert report.problems == ["faq.html: FAQPage lists 2 questions, the page has 3"]
