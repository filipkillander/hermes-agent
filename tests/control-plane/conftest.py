"""Conftest for control-plane tests.

These tests exercise the Fas A completion-integrity-gate, which depends on
``jsonschema``. The host machine has a *broken* shared release venv on
``PYTHONPATH`` (a 3.11 release whose ``rpds.rpds`` native module is
incompatible with the 3.12 interpreter) — if that path leaks into ``sys.path``,
``import jsonschema`` fails with ``ModuleNotFoundError: No module named
'rpds.rpds'``.

To make these tests environment-independent, we scrub any site-packages
directory that does not belong to this worktree's own ``.venv`` from
``sys.path`` before collection. This guarantees the worktree's clean
``jsonschema`` (installed via ``uv pip install`` into ``.venv``) wins.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Absolute path of this worktree's clean venv site-packages dir.
_WORKTREE_VENV_SITE = str(
    Path(__file__).resolve().parents[2] / ".venv" / "lib" / "python3.12" / "site-packages"
)


def _scrub_foreign_site_packages() -> None:
    """Remove non-worktree site-packages dirs from sys.path.

    Keeps stdlib, the worktree's own .venv site-packages, and the repo root
    (for the editable install / local imports). Drops anything else that
    looks like a foreign ``site-packages`` — specifically the broken shared
    release venv that the host injects via PYTHONPATH.
    """
    keep = []
    for entry in sys.path:
        if not entry:
            keep.append(entry)
            continue
        # Always keep stdlib-ish entries and the worktree venv.
        if "site-packages" in entry and entry != _WORKTREE_VENV_SITE:
            # Foreign site-packages — drop it.
            continue
        keep.append(entry)
    sys.path[:] = keep


_scrub_foreign_site_packages()

# Also clear PYTHONPATH so subprocesses spawned by tests (none currently,
# but defensive) don't re-inherit the broken path.
os.environ.pop("PYTHONPATH", None)