#!/usr/bin/env python3
"""Standalone CLI entry point for the completion-integrity-gate.

Usage:
    python3 scripts/completion_gate_cli.py \
        --manifest control-plane/requirements.yaml \
        --base-dir /path/to/worktree \
        --schema-path control-plane/requirements.schema.json \
        --git-repo /path/to/git/repo

Exit codes: 0 = PASS, 1 = FAIL, 2 = BLOCKED.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from completion_gate.gate import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
