"""Tests for scripts/spam_guard.py - the rule that hides link spam from new accounts.

Both directions of failure are silent: a guard that misses the spam leaves the thread
as it was, and a guard that hides a real user's question leaves them talking to a
collapsed comment nobody expands. The fixtures include the two spam comments that
actually arrived, because a rule tuned on invented examples proves only that it
matches what its author imagined.
"""

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location("spam_guard", REPO_ROOT / "scripts" / "spam_guard.py")
spam_guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(spam_guard)

MAINTAINERS = frozenset({"drake69"})

# Received on discussion 239, 2026-11-13.
REAL_SPAM = (
    "Early-morning watering, running irrigation zones one at a time, and adjusting schedules based on weather "
    "conditions can help reduce water waste and maintain healthier [landscapes](https://opallandscaping.com/).\r\n\r\n"
    "One useful feature would be automatically prioritising irrigation zones "
    "when the available watering window is limited."
)


@pytest.mark.parametrize(
    "post_text",
    [
        "See https://github.com/Koenkk/zigbee2mqtt/issues/1 and https://www.home-assistant.io/integrations/template/",
        "Logs at https://community.home-assistant.io/t/x/123 and https://raw.githubusercontent.com/a/b/main/c.yaml",
        "Docs: https://www.zigbee2mqtt.io/devices/SWV.html, planner https://never-dry.github.io/neverdry-planner/",
        "No links at all, just a question about the deficit.",
        "",
    ],
)
def test_links_genuine_users_post_are_allowed(post_text):
    assert spam_guard.foreign_hosts(post_text) == []


def test_real_spam_link_is_found():
    assert spam_guard.foreign_hosts(REAL_SPAM) == ["opallandscaping.com"]


@pytest.mark.parametrize(
    ("post_text", "expected_hosts"),
    [
        ("visit www.example.com today", ["www.example.com"]),
        ("<a href='http://Example.org:8080/x'>x</a>", ["example.org:8080"]),
        # A lookalike that ends in an allowed name without being a subdomain of it.
        ("https://evilgithub.com/x", ["evilgithub.com"]),
        ("https://github.com.evil.io/x", ["github.com.evil.io"]),
        ("[a](https://spam.example/) and again [b](https://spam.example/p)", ["spam.example"]),
    ],
)
def test_foreign_hosts_are_reported_once(post_text, expected_hosts):
    assert spam_guard.foreign_hosts(post_text) == expected_hosts


@pytest.mark.parametrize(
    ("association", "login", "expected"),
    [
        ("NONE", "abhilashtakemail2-spec", True),
        ("FIRST_TIMER", "someone", True),
        # Private org membership reads back as NONE through the workflow token.
        ("NONE", "Drake69", False),
        ("CONTRIBUTOR", "philipgiuliani", False),
        ("FIRST_TIME_CONTRIBUTOR", "someone", False),
        ("MEMBER", "someone", False),
    ],
)
def test_only_authors_without_history_are_judged(association, login, expected):
    assert spam_guard.is_outsider(association, login, MAINTAINERS) is expected


def test_discussion_comment_is_described_as_comment():
    payload = {
        "comment": {
            "node_id": "DC_x",
            "body": REAL_SPAM,
            "author_association": "NONE",
            "user": {"login": "abhilashtakemail2-spec"},
            "html_url": "https://github.com/never-dry/NeverDry/discussions/239#discussioncomment-19583391",
        }
    }
    post = spam_guard.describe_post("discussion_comment", payload)
    assert post["kind"] == "comment"
    assert post["node_id"] == "DC_x"
    assert post["login"] == "abhilashtakemail2-spec"


def test_new_issue_includes_title_in_text():
    payload = {
        "issue": {"node_id": "I_x", "title": "Buy at https://spam.example", "body": None, "user": {"login": "x"}}
    }
    post = spam_guard.describe_post("issues", payload)
    assert post["kind"] == "post"
    assert spam_guard.foreign_hosts(post["text"]) == ["spam.example"]
    assert post["association"] == "NONE"


def test_pull_request_comment_is_out_of_scope():
    payload = {"issue": {"pull_request": {"url": "u"}}, "comment": {"node_id": "IC_x", "body": "https://x.example"}}
    assert spam_guard.describe_post("issue_comment", payload) is None
