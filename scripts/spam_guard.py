#!/usr/bin/env python3
"""spam_guard.py - hide link spam from first-time accounts the moment it lands.

The spam this repository receives has one shape: a fresh account posts a plausible
feature suggestion on a discussion, and one ordinary word in it links to a company
site. GitHub has no setting that refuses a post for its content, so the post cannot
be stopped before it is published. It can be hidden seconds after, which is what
this script does when a workflow hands it the event.

The rule is deliberately narrow, because a false positive hides a real user:

- only authors with no relationship to the repository (NONE, FIRST_TIMER) and not in
  the maintainers' login list are looked at;
- only links to a domain outside the allowlist count - GitHub, Home Assistant,
  Zigbee2MQTT and our own site are where genuine users point;
- a comment is minimized as SPAM, which anyone with triage access undoes with a click.
  The body of a new issue or discussion cannot be minimized on GitHub, so those get
  the spam-suspect label and stay visible for a human to judge.

Accounts are never blocked from here: the workflow token has no org-level rights, and
blocking is a decision the maintainer takes after reading the post.

Run by .github/workflows/spam-guard.yml, which sets GITHUB_EVENT_NAME,
GITHUB_EVENT_PATH, GH_TOKEN and GH_REPO.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

# Domains a genuine user links to. A subdomain of any of these is allowed too.
ALLOWED_DOMAINS = frozenset(
    {
        "github.com",
        "githubusercontent.com",
        "github.io",
        "home-assistant.io",
        "zigbee2mqtt.io",
    }
)

# Associations that mean "no history with this repository". FIRST_TIME_CONTRIBUTOR is
# left out: that author has a pull request here, which no drive-by spammer has.
OUTSIDER_ASSOCIATIONS = frozenset({"NONE", "FIRST_TIMER"})

SUSPECT_LABEL = "spam-suspect"

# A scheme link, or the bare www. form GitHub autolinks. The host stops at the first
# character that cannot belong to it, which covers the closing parenthesis of [x](url).
LINK_HOST = re.compile(r"(?:https?://|(?<![\w.])www\.)([^\s/?#)\]>\"'`<]+)", re.IGNORECASE)


def is_allowed_host(host_name: str) -> bool:
    """Tell whether a link host belongs to a domain genuine users point to.

    Args:
        host_name: the host part of a link as found in the text, possibly with a port,
            userinfo or a trailing dot; case does not matter.

    Returns:
        True when the host is an allowlisted domain or one of its subdomains.
    """
    bare_host = host_name.rsplit("@", 1)[-1].split(":", 1)[0].rstrip(".").lower()
    return any(bare_host == domain or bare_host.endswith("." + domain) for domain in ALLOWED_DOMAINS)


def foreign_hosts(post_text: str) -> list[str]:
    """List the hosts in a post that fall outside the allowlist.

    Args:
        post_text: the markdown body, plus the title for a top-level post. An empty
            string is allowed and yields no hosts.

    Returns:
        The offending hosts in order of appearance, without duplicates.
    """
    found_hosts: list[str] = []
    for match in LINK_HOST.finditer(post_text):
        link_host = match.group(1)
        if match.group(0).lower().startswith("www."):
            link_host = "www." + link_host
        if not is_allowed_host(link_host) and link_host.lower() not in found_hosts:
            found_hosts.append(link_host.lower())
    return found_hosts


def is_outsider(author_association: str, author_login: str, maintainer_logins: frozenset[str]) -> bool:
    """Tell whether the author of a post is someone the guard should look at.

    The login list is not redundant with the association: the workflow token is not an
    org member, so a maintainer whose membership is private reads back as NONE.

    Args:
        author_association: the author_association field of the event payload.
        author_login: the author's GitHub login; empty when the account is gone.
        maintainer_logins: lowercase logins that are never treated as outsiders.

    Returns:
        True for an author with no history here who is not a listed maintainer.
    """
    if author_login.lower() in maintainer_logins:
        return False
    return author_association in OUTSIDER_ASSOCIATIONS


def describe_post(event_name: str, event_payload: dict) -> dict | None:
    """Pull out of a webhook payload the one post the guard has to judge.

    Args:
        event_name: GITHUB_EVENT_NAME, one of discussion, discussion_comment, issues,
            issue_comment.
        event_payload: the parsed JSON of GITHUB_EVENT_PATH.

    Returns:
        A dict with node_id, kind ("comment" or "post"), text, association, login and
        url, or None when the event carries nothing to judge (an unrelated event, or
        a comment on a pull request, which is code review and out of scope).
    """
    if event_name in ("discussion_comment", "issue_comment"):
        if event_name == "issue_comment" and event_payload.get("issue", {}).get("pull_request"):
            return None
        subject, kind, text = event_payload["comment"], "comment", event_payload["comment"].get("body") or ""
    elif event_name in ("discussion", "issues"):
        subject = event_payload["discussion" if event_name == "discussion" else "issue"]
        kind, text = "post", (subject.get("title") or "") + "\n" + (subject.get("body") or "")
    else:
        return None
    return {
        "node_id": subject["node_id"],
        "kind": kind,
        "text": text,
        "association": subject.get("author_association") or "NONE",
        "login": (subject.get("user") or {}).get("login") or "",
        "url": subject.get("html_url") or "",
    }


def run_gh(*gh_arguments: str) -> str:
    """Call the gh CLI and fail loudly, so a workflow that could not act shows red.

    Args:
        gh_arguments: arguments after "gh", passed as a list and never through a shell.

    Returns:
        The command's standard output.
    """
    return subprocess.run(["gh", *gh_arguments], check=True, capture_output=True, text=True).stdout  # noqa: S603, S607


def minimize_as_spam(node_id: str) -> None:
    """Collapse a comment as SPAM; the reader can still expand it, and triage can undo it.

    Args:
        node_id: GraphQL node id of the issue or discussion comment.
    """
    mutation = (
        "mutation($id:ID!){minimizeComment(input:{subjectId:$id,classifier:SPAM}){minimizedComment{isMinimized}}}"
    )
    run_gh("api", "graphql", "-f", f"query={mutation}", "-f", f"id={node_id}")


def label_as_suspect(node_id: str, repository_slug: str) -> None:
    """Put the suspect label on an issue or discussion whose body cannot be minimized.

    Args:
        node_id: GraphQL node id of the issue or discussion.
        repository_slug: "owner/name" of the repository that holds the label.
    """
    run_gh(
        "label",
        "create",
        SUSPECT_LABEL,
        "--repo",
        repository_slug,
        "--force",
        "--color",
        "B60205",
        "--description",
        "Link from a first-time account outside the allowlist; review and block or remove",
    )
    owner, name = repository_slug.split("/", 1)
    label_query = (
        "query($owner:String!,$name:String!,$label:String!)"
        "{repository(owner:$owner,name:$name){label(name:$label){id}}}"
    )
    label_id = json.loads(
        run_gh(
            "api",
            "graphql",
            "-f",
            f"query={label_query}",
            "-f",
            f"owner={owner}",
            "-f",
            f"name={name}",
            "-f",
            f"label={SUSPECT_LABEL}",
        )
    )["data"]["repository"]["label"]["id"]
    mutation = (
        "mutation($id:ID!,$label:ID!)"
        "{addLabelsToLabelable(input:{labelableId:$id,labelIds:[$label]}){clientMutationId}}"
    )
    run_gh("api", "graphql", "-f", f"query={mutation}", "-f", f"id={node_id}", "-f", f"label={label_id}")


def main() -> int:
    """Judge the post in the current workflow event and act on it.

    Returns:
        The process exit status: 0 whether or not the post was acted on.
    """
    event_name = os.environ["GITHUB_EVENT_NAME"]
    event_payload = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    maintainer_logins = frozenset(
        login.strip().lower() for login in os.environ.get("MAINTAINER_LOGINS", "").split(",") if login.strip()
    )

    post = describe_post(event_name, event_payload)
    if post is None:
        print(f"{event_name}: nothing to judge")
        return 0
    # Printed every time, acted on or not: a guard that reads authorship has to say
    # what it read, or the day it misreads it nobody can tell why.
    print(f"{event_name}: {post['url']} by {post['login'] or 'unknown'} ({post['association']})")
    if not is_outsider(post["association"], post["login"], maintainer_logins):
        print("author has history here or is a maintainer; left alone")
        return 0
    offending_hosts = foreign_hosts(post["text"])
    if not offending_hosts:
        print("no link outside the allowlist; left alone")
        return 0
    if post["kind"] == "comment":
        minimize_as_spam(post["node_id"])
        print(f"minimized as spam, links to: {', '.join(offending_hosts)}")
    else:
        label_as_suspect(post["node_id"], os.environ["GH_REPO"])
        print(f"labelled {SUSPECT_LABEL}, links to: {', '.join(offending_hosts)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
