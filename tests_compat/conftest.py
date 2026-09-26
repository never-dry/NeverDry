"""Deliberately minimal, and deliberately not under ``tests/``.

``tests/conftest.py`` stubs ``homeassistant`` wholesale, which is what makes the
suite fast and independent of any release. These tests exist for the opposite
reason: they run against a **real** Home Assistant, installed at the version
this project claims to support, and a stub would answer every question with
whatever the stub was written to say.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "custom_components"))
