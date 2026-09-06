"""The repository must not instruct an agent that reads it.

A coding agent pointed at this checkout reads more than the code. Several of them
open a file at a known path, without being asked and without telling anyone, and
treat what they find there as standing instructions: `AGENTS.md`, `CLAUDE.md`,
`.cursorrules`, and their relatives. A contributor adding one in good faith, to help
whoever comes next, would be writing policy for every agent that touches this project
from then on, including the maintainer's.

The configuration files are the more serious half. An instruction file suggests; a
`.mcp.json` or a `.devcontainer` grants. One tells an agent what to think, the other
hands it tools and runs on checkout.

What these guards can and cannot do, stated plainly so nobody mistakes them for more
than they are:

* **They remove the automatic vector.** A file no human opens but every agent does.
* **They remove the invisible vector.** Zero-width characters, direction overrides and
  the Unicode tag block carry text that review cannot see, by construction. A person
  rereading a translation in a language they do not speak has no chance against it,
  and neither does an agent summarising the diff.
* **They constrain the field where unreadable content legitimately lives.** A
  translation value is prose shown to a person. Markup, code fences and role markers
  are out of spec there, whatever their intent.

They do **not** detect prompt injection. Instructions written as ordinary readable
prose inside a docstring will pass every check in this file. What is left after these
three is text that a person can see in a diff, which is where the guard stops being a
test and becomes a review.

Workflows under `.github/workflows/` are deliberately out of scope: a hostile workflow
is a graver problem than any of this, and it is answered by branch protection and
review rather than by a pattern.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent

# Not part of what ships or what a contributor sends: agent scratch space and the
# reading-debt manifests, both local to the maintainer's machine.
_SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".coding_agent", ".code_reader"}

# Files an agent opens on its own initiative. The value says who reads it, because a
# contributor who trips this deserves a reason rather than a refusal.
_INSTRUCTION_FILES = {
    "AGENTS.md": "read automatically by Codex and several other coding agents",
    "AGENT.md": "the singular spelling of the same convention",
    "CLAUDE.md": "read automatically by Claude Code",
    "GEMINI.md": "read automatically by the Gemini CLI",
    ".cursorrules": "read automatically by Cursor",
    ".windsurfrules": "read automatically by Windsurf",
    ".clinerules": "read automatically by Cline",
    ".aider.conf.yml": "configures Aider, including which model runs and what it may execute",
    ".mcp.json": "declares MCP servers, which hand an agent tools rather than advice",
    "copilot-instructions.md": "read automatically by GitHub Copilot",
}

# Directories with the same effect. `.devcontainer` is here because it runs on checkout
# in Codespaces, which makes it execution rather than instruction. If this project ever
# wants one, that is a decision to take deliberately and to record by removing the entry.
_INSTRUCTION_DIRS = {
    ".claude": "Claude Code settings, hooks and permissions",
    ".cursor": "Cursor rules",
    ".continue": "Continue configuration",
    ".roo": "Roo Code rules",
    ".kilocode": "Kilo Code rules",
    ".aider": "Aider state",
    ".devcontainer": "runs on checkout in Codespaces, so it executes rather than advises",
}

# Invisible or direction-changing characters. The tag block at the end is the one that
# matters most: it encodes plain ASCII in codepoints that render as nothing at all.
_INVISIBLE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff\U000e0000-\U000e007f]")

_TRANSLATIONS = [
    *sorted((_REPO / "custom_components" / "never_dry" / "translations").glob("*.json")),
    _REPO / "custom_components" / "never_dry" / "strings.json",
]


def _repo_files():
    """Every text file in the checkout, with the agent scratch space left out."""
    for path in _REPO.rglob("*"):
        if not path.is_file():
            continue
        if _SKIP_DIRS & set(path.relative_to(_REPO).parts):
            continue
        yield path


def test_no_agent_instruction_or_configuration_files():
    """No file in this repository speaks to an agent behind the reviewer's back."""
    offenders: list[str] = []
    for path in _repo_files():
        relative = path.relative_to(_REPO)
        reason = _INSTRUCTION_FILES.get(path.name)
        if reason:
            offenders.append(f"{relative}: {reason}")
        for part in relative.parts[:-1]:
            if part in _INSTRUCTION_DIRS:
                offenders.append(f"{relative}: inside {part}/, {_INSTRUCTION_DIRS[part]}")
                break

    assert not offenders, (
        "files that instruct or configure a coding agent:\n  "
        + "\n  ".join(sorted(set(offenders)))
        + "\n\nProject conventions belong in CONTRIBUTING.md, where a person reads them and "
        "an agent has to be told to look."
    )


def test_no_invisible_or_direction_changing_characters():
    """Text review cannot see is text review cannot judge.

    This is the guard that earns its place. Everything else here can also be caught by a
    careful reader; this cannot, because the characters render as nothing. It matters most
    in the files nobody on the project can read anyway: a translation arrives in a language
    the maintainer does not speak, gets checked for key parity, and is merged.
    """
    offenders: list[str] = []
    for path in _repo_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary asset, nothing to read
        for number, line in enumerate(text.splitlines(), start=1):
            found = _INVISIBLE.findall(line)
            if found:
                codepoints = ", ".join(sorted({f"U+{ord(c):04X}" for c in found}))
                offenders.append(f"{path.relative_to(_REPO)}:{number} contains {codepoints}")

    assert not offenders, "invisible or direction-changing characters:\n  " + "\n  ".join(offenders[:40])


def _leaf_strings(node, prefix: str = ""):
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _leaf_strings(value, f"{prefix}.{key}" if prefix else key)
    elif isinstance(node, str):
        yield prefix, node


# What a translation value is: prose put in front of a person. Everything below is
# something else wearing its clothes.
_NOT_PROSE = (
    ("<!--", "an HTML comment, which a reader of the interface never sees"),
    ("-->", "the end of an HTML comment"),
    ("```", "a code fence"),
    ("<|", "a model role marker"),
    ("|>", "a model role marker"),
    ("{{", "a template expression"),
    ("}}", "a template expression"),
)

_ROLE_LINE = re.compile(r"(?im)^\s*(system|assistant|user|developer)\s*:")
_URL = re.compile(r"https?://")


def test_translated_strings_are_prose_not_markup():
    """A translation value carries wording, and nothing that behaves.

    Not a detector for injected instructions, which would be a promise this cannot keep.
    A contract on the field instead: these files hold sentences for a user, so markup,
    fences, role markers and links are out of spec regardless of why they are there. The
    point is that the rule can be checked, and the reviewer of a language they cannot read
    is left judging wording rather than intent.
    """
    offenders: list[str] = []
    for path in _TRANSLATIONS:
        if not path.exists():
            continue
        document = json.loads(path.read_text(encoding="utf-8"))
        for where, value in _leaf_strings(document):
            for needle, why in _NOT_PROSE:
                if needle in value:
                    offenders.append(f"{path.name}: {where} contains {needle!r}, {why}")
            if _ROLE_LINE.search(value):
                offenders.append(f"{path.name}: {where} opens a line with a conversational role")
            if _URL.search(value):
                offenders.append(f"{path.name}: {where} contains a link; put it in the documentation")

    assert not offenders, "translation values that are not prose:\n  " + "\n  ".join(offenders)
