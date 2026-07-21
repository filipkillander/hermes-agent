"""Completion Gate — reusable, fail-closed, goal-ID-bound integrity gate.

This package provides a reusable completion-integrity gate (CIG) that can be
used with ANY requirements manifest, not just Fas A.  The gate enforces:

1.  Ledger is version-bound to goal-ID and source message.
2.  Cards reference ledger requirements, not the other way around.
3.  Every requirement requires evidence.
4.  Review compares original requirements against actual state.
5.  The agent cannot self-certify a simplified task list.
6.  COMPLETE is denied if any requirement lacks evidence, has a blocker, or
    only has the agent's own declaration.

The gate is fail-closed: if evidence is missing, the result is FAIL or
BLOCKED, never PASS.  Production CLI mode has no trust context and therefore
NEVER sets closeout_permitted = True.
"""

from __future__ import annotations

from .gate import (
    CheckResult,
    GateResult,
    TrustContext,
    run_gate,
    validate_path,
    validate_branch_name,
    validate_commit_sha,
    validate_sha256,
    validate_iso_timestamp,
)

__all__ = [
    "CheckResult",
    "GateResult",
    "TrustContext",
    "run_gate",
    "validate_path",
    "validate_branch_name",
    "validate_commit_sha",
    "validate_sha256",
    "validate_iso_timestamp",
]
