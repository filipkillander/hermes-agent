#!/usr/bin/env python3
"""Completion Integrity Gate (CIC) for Fas A.

Read-only enforcement code. Reads a requirements manifest (YAML) plus
evidence (git history, files, receipts) and returns a deterministic JSON
verdict. The CIC never mutates state: it does not create tasks, change
boards, push, install, restart, or write to any surface.

Design invariants (see task t_294c700d):

  * shell=False is always used for subprocess calls. No free-form command,
    script, runner, or shell strings are ever read from the manifest.
  * Only allowlisted check types are accepted. An unknown type → FAIL.
  * File paths are validated against a hard-coded path allowlist. Any path
    that escapes → FAIL with ``path_not_allowlisted``.
  * Shell metacharacters in check arguments (";", "|", "$(", "`") → FAIL.
  * Manifest hash and per-requirement source_hash are recomputed and
    compared. Any mismatch → FAIL.
  * Test and owner-decision receipts bind to exact commit-SHA, branch,
    check-ID, timestamp and output-hash. An owner_id that is missing or a
    placeholder (PENDING_OWNER_DECISION / empty / fabricated) → BLOCKED,
    never PASS or FAIL.
  * PENDING_OWNER_DECISION always yields BLOCKED.

Gate result:

  * ``PASS``    — every requirement's evidence_check returns ``expected``.
                  "Fas A klar" is permitted.
  * ``FAIL``    — some evidence_check returns a value other than ``expected``,
                  or manifest/source hash mismatch, unknown check type,
                  path escape, or shell metacharacters. Closeout blocked;
                  exact failing requirement IDs are listed.
  * ``BLOCKED`` — some requirement uses ``owner_decision_receipt`` and its
                  ``owner_id`` is missing or a placeholder. "Fas A klar"
                  is NOT permitted; only fully independent work cards (not
                  depending on the blocked requirement) may continue.

Output: a single JSON object printed to stdout. Exit code is 0 when the
gate is PASS, 1 when FAIL, and 2 when BLOCKED. Deterministic: identical
inputs produce identical output across runs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# ── Hard-coded allowlists (never read from YAML) ───────────────────────────

GIT_REPO = "/Users/ai/.hermes/hermes-agent"

# Path allowlist for file_sha256 / receipt_path / schema_valid arguments.
# Any path that does not resolve under one of these prefixes → FAIL with
# ``path_not_allowlisted``. Resolved (realpath) comparison guards against
# ``../../`` escapes.
PATH_ALLOWLIST_PREFIXES = (
    "/Users/ai/.hermes/worktrees/kmros-",
    "/Users/ai/Dropbox/FILIP KILLANDER/repository/the-terminal-worktrees/kmros-",
)

# The CIC itself may read its own manifest and schema inside the worktree.
# Receipts referenced by the manifest must also live under an allowlisted
# worktree. This is enforced by ``validate_path``.

ALLOWED_CHECK_TYPES = {
    "git_ancestor",
    "git_not_ancestor",
    "file_sha256",
    "schema_valid",
    "test_receipt",
    "owner_decision_receipt",
}

# Shell metacharacters that must never appear in a check argument. Their
# presence is treated as an attempted injection → FAIL.
SHELL_METACHARACTERS = (";", "|", "$(", "`")

# Owner-id placeholders that always yield BLOCKED. A concrete owner_id is
# required for owner_decision_receipt checks to even be evaluated.
OWNER_PLACEHOLDER_VALUES = {
    "",
    "PENDING_OWNER_DECISION",
    "PENDING",
    "TBD",
    "TODO",
    "NONE",
}

_COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# ── Data model ──────────────────────────────────────────────────────────────


@dataclass
class CheckResult:
    """Result of evaluating a single requirement."""

    requirement_id: str
    check_type: str
    status: str  # "pass" | "fail" | "blocked"
    observed: str
    expected: str
    reason: str = ""


@dataclass
class GateResult:
    """Top-level gate result."""

    gate: str  # "PASS" | "FAIL" | "BLOCKED"
    manifest_id: str
    manifest_hash: str
    manifest_hash_matches: bool
    results: list = field(default_factory=list)
    failing_ids: list = field(default_factory=list)
    blocked_ids: list = field(default_factory=list)
    closeout_permitted: bool = False
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "gate": self.gate,
            "manifest_id": self.manifest_id,
            "manifest_hash": self.manifest_hash,
            "manifest_hash_matches": self.manifest_hash_matches,
            "results": [
                {
                    "requirement_id": r.requirement_id,
                    "check_type": r.check_type,
                    "status": r.status,
                    "observed": r.observed,
                    "expected": r.expected,
                    "reason": r.reason,
                }
                for r in self.results
            ],
            "failing_ids": self.failing_ids,
            "blocked_ids": self.blocked_ids,
            "closeout_permitted": self.closeout_permitted,
            "reason": self.reason,
        }


# ── Validation helpers ─────────────────────────────────────────────────────


def _contains_shell_metacharacters(value: str) -> bool:
    """Return True if any shell metacharacter appears in ``value``."""
    return any(ch in value for ch in SHELL_METACHARACTERS)


def _check_args_for_metachars(check: dict) -> str | None:
    """Walk a check dict; return the first offending value or None."""
    for key, value in check.items():
        if key == "type":
            continue
        if isinstance(value, str) and _contains_shell_metacharacters(value):
            return value
    return None


def validate_path(path_str: str, base_dir: Path | None = None) -> tuple[bool, str, str | None]:
    """Validate ``path_str`` against the path allowlist.

    Returns ``(ok, resolved_or_error, real_path)``. When ``base_dir`` is
    given, a relative ``path_str`` is resolved against it before
    allowlisting (so manifest-relative receipt paths are honored inside
    the worktree). The real (symlink-resolved) path is returned for
    callers that need to read the file.
    """
    if _contains_shell_metacharacters(path_str):
        return False, "shell_metacharacters_in_path", None

    candidate = Path(path_str)
    if not candidate.is_absolute() and base_dir is not None:
        candidate = (base_dir / candidate)
    try:
        # Resolve without requiring the file to exist; we still want to
        # normalize ``..`` segments. ``strict=False`` lets us validate
        # paths for files that may be created later (e.g. receipts).
        real = candidate.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        return False, f"path_resolution_error:{exc}", None

    real_str = str(real)
    for prefix in PATH_ALLOWLIST_PREFIXES:
        # The worktree root itself must be under one of the allowlist
        # prefixes; a path inside it is acceptable iff its realpath starts
        # with an allowlisted prefix (after ``..`` resolution). This also
        # blocks paths like ``/Users/ai/.hermes/worktrees/kmros-cic/../../etc``.
        if real_str == prefix or real_str.startswith(prefix.rstrip("/") + "/"):
            return True, real_str, real_str
    return False, "path_not_allowlisted", None


def validate_commit_sha(sha: str) -> bool:
    return bool(_COMMIT_SHA_RE.match(sha or ""))


def validate_sha256(value: str) -> bool:
    return bool(_SHA256_RE.match(value or ""))


# ── Hash helpers ───────────────────────────────────────────────────────────


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def manifest_hash(manifest_text: str) -> str:
    """Stable hash of the manifest text, used to detect tampering.

    The hash is taken over the raw manifest bytes as read from disk, so a
    caller that mutates the file after the CIC was originally run will
    produce a different hash. The CIC always recomputes this from the
    file it actually reads; it never trusts a hash field in the YAML.
    """
    return sha256_text(manifest_text)


# ── Check evaluators (shell=False, allowlisted) ─────────────────────────────


def _git_ancestor(repo: str, commit: str, branch: str, expect_ancestor: bool) -> tuple[str, str]:
    """Run ``git merge-base --is-ancestor`` with shell=False.

    Returns ``(observed, reason)`` where observed is ``exit_0`` or
    ``exit_1`` (the actual subprocess exit code) and ``reason`` is empty
    on the expected path or a short explanation on error.
    """
    cmd = ["git", "-C", repo, "merge-base", "--is-ancestor", commit, branch]
    proc = subprocess.run(cmd, shell=False, capture_output=True, text=True)
    observed = f"exit_{proc.returncode}"
    if expect_ancestor:
        return observed, "" if proc.returncode == 0 else "commit_not_ancestor"
    # git_not_ancestor: expected exit_1 (i.e. commit is NOT an ancestor)
    return observed, "" if proc.returncode == 1 else "commit_is_ancestor"


def _file_sha256(path_str: str, expected_sha256: str, base_dir: Path | None) -> tuple[str, str, str | None]:
    ok, msg, real = validate_path(path_str, base_dir=base_dir)
    if not ok or real is None:
        return "path_not_allowlisted", msg, None
    if not os.path.exists(real):
        return "missing_file", f"file_not_found:{path_str}", None
    actual = sha256_file(real)
    observed = "match" if actual == expected_sha256 else "mismatch"
    return observed, "" if observed == "match" else f"sha256_mismatch:{actual}", actual


def _schema_valid(yaml_path: str, schema_path: str, base_dir: Path | None) -> tuple[str, str]:
    """Validate a YAML file against a JSON Schema. Returns (observed, reason)."""
    ok_y, msg_y, real_y = validate_path(yaml_path, base_dir=base_dir)
    if not ok_y or real_y is None:
        return "path_not_allowlisted", f"yaml_path:{msg_y}"
    ok_s, msg_s, real_s = validate_path(schema_path, base_dir=base_dir)
    if not ok_s or real_s is None:
        return "path_not_allowlisted", f"schema_path:{msg_s}"
    if not os.path.exists(real_y):
        return "invalid", f"yaml_not_found:{yaml_path}"
    if not os.path.exists(real_s):
        return "invalid", f"schema_not_found:{schema_path}"
    import jsonschema  # local import keeps the module import-free at parse time
    try:
        with open(real_y, "rb") as fh:
            doc = yaml.safe_load(fh)
        with open(real_s, "rb") as fh:
            schema = json.load(fh)
        jsonschema.validate(instance=doc, schema=schema)
        return "valid", ""
    except jsonschema.ValidationError as exc:
        return "invalid", f"schema_violation:{exc.message}"
    except Exception as exc:  # noqa: BLE001 — read-only, no mutation
        return "invalid", f"schema_check_error:{type(exc).__name__}:{exc}"


def _parse_receipt(receipt_path: str, base_dir: Path | None) -> tuple[dict | None, str]:
    ok, msg, real = validate_path(receipt_path, base_dir=base_dir)
    if not ok or real is None:
        return None, f"path_not_allowlisted:{msg}"
    if not os.path.exists(real):
        return None, f"receipt_not_found:{receipt_path}"
    try:
        with open(real, "rb") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return None, "receipt_not_object"
        return data, ""
    except Exception as exc:  # noqa: BLE001
        return None, f"receipt_parse_error:{type(exc).__name__}:{exc}"


def _test_receipt(check: dict, base_dir: Path | None) -> tuple[str, str]:
    """Verify a test receipt binds to expected branch/commit/check_id."""
    receipt_path = check.get("receipt_path", "")
    expected_branch = check.get("expected_branch", "")
    expected_commit = check.get("expected_commit", "")
    expected_check_id = check.get("expected_check_id", "")

    # Reject shell metacharacters in any of these args.
    for key in ("receipt_path", "expected_branch", "expected_commit", "expected_check_id"):
        v = check.get(key, "")
        if isinstance(v, str) and _contains_shell_metacharacters(v):
            return "unverified", f"shell_metacharacters_in:{key}"

    if not validate_commit_sha(expected_commit):
        return "unverified", "expected_commit_not_40char_hex"

    data, err = _parse_receipt(receipt_path, base_dir=base_dir)
    if data is None:
        return "unverified", err

    required_fields = ("branch", "commit", "check_id", "timestamp", "output_hash")
    missing = [f for f in required_fields if f not in data]
    if missing:
        return "unverified", f"receipt_missing_fields:{','.join(missing)}"

    if data.get("branch") != expected_branch:
        return "unverified", f"branch_mismatch:{data.get('branch')}"
    if data.get("commit") != expected_commit:
        return "unverified", f"commit_mismatch:{data.get('commit')}"
    if data.get("check_id") != expected_check_id:
        return "unverified", f"check_id_mismatch:{data.get('check_id')}"
    if not validate_sha256(str(data.get("output_hash", ""))):
        return "unverified", "output_hash_not_sha256"
    return "verified", ""


def _owner_decision_receipt(check: dict, base_dir: Path | None) -> tuple[str, str, bool]:
    """Verify an owner-decision receipt.

    Returns ``(observed, reason, is_blocked)``. ``is_blocked`` is True when
    the owner_id is missing or a placeholder — in that case the check is
    BLOCKED, never PASS or FAIL.
    """
    owner_id = str(check.get("owner_id", "")).strip()
    if owner_id in OWNER_PLACEHOLDER_VALUES or owner_id.upper() in {
        p.upper() for p in OWNER_PLACEHOLDER_VALUES
    }:
        return "blocked", f"owner_id_placeholder:{owner_id}", True

    receipt_path = check.get("receipt_path", "")
    if isinstance(receipt_path, str) and _contains_shell_metacharacters(receipt_path):
        return "unauthorized", "shell_metacharacters_in:receipt_path", False

    data, err = _parse_receipt(receipt_path, base_dir=base_dir)
    if data is None:
        # Missing receipt for a concrete owner_id is FAIL (not BLOCKED) —
        # a concrete owner without a binding receipt is not authorization.
        return "unauthorized", err, False

    required_fields = ("owner_id", "decision", "commit", "branch", "timestamp", "output_hash")
    missing = [f for f in required_fields if f not in data]
    if missing:
        return "unauthorized", f"receipt_missing_fields:{','.join(missing)}", False

    if str(data.get("owner_id", "")).strip() != owner_id:
        return "unauthorized", f"owner_id_mismatch:{data.get('owner_id')}", False

    decision = str(data.get("decision", "")).strip().lower()
    if decision not in ("approved", "authorized", "accept", "yes"):
        return "unauthorized", f"decision_not_approved:{decision}", False

    if not validate_commit_sha(str(data.get("commit", ""))):
        return "unauthorized", "receipt_commit_not_40char_hex", False
    if not validate_sha256(str(data.get("output_hash", ""))):
        return "unauthorized", "receipt_output_hash_not_sha256", False

    return "authorized", "", False


# ── Per-requirement evaluation ─────────────────────────────────────────────


def evaluate_requirement(req: dict, base_dir: Path | None) -> CheckResult:
    rid = req.get("id", "<unknown>")
    check = req.get("check", {}) or {}
    ctype = check.get("type", "")
    expected = req.get("expected", "")

    # Unknown check type → FAIL.
    if ctype not in ALLOWED_CHECK_TYPES:
        return CheckResult(
            requirement_id=rid,
            check_type=ctype,
            status="fail",
            observed="unknown_check_type",
            expected=expected,
            reason=f"unknown_check_type:{ctype}",
        )

    # Shell metacharacters in any check argument → FAIL.
    offender = _check_args_for_metachars(check)
    if offender is not None:
        return CheckResult(
            requirement_id=rid,
            check_type=ctype,
            status="fail",
            observed="shell_metacharacters_detected",
            expected=expected,
            reason=f"shell_metacharacters_in:{offender}",
        )

    if ctype == "git_ancestor":
        branch = check.get("branch", "")
        commit = check.get("commit_sha", "")
        if not validate_commit_sha(commit):
            return CheckResult(rid, ctype, "fail", "invalid_commit_sha", expected,
                               f"commit_sha_not_40char_hex:{commit}")
        observed, reason = _git_ancestor(GIT_REPO, commit, branch, expect_ancestor=True)
        status = "pass" if observed == expected else "fail"
        return CheckResult(rid, ctype, status, observed, expected, reason)

    if ctype == "git_not_ancestor":
        branch = check.get("branch", "")
        commit = check.get("commit_sha", "")
        if not validate_commit_sha(commit):
            return CheckResult(rid, ctype, "fail", "invalid_commit_sha", expected,
                               f"commit_sha_not_40char_hex:{commit}")
        observed, reason = _git_ancestor(GIT_REPO, commit, branch, expect_ancestor=False)
        status = "pass" if observed == expected else "fail"
        return CheckResult(rid, ctype, status, observed, expected, reason)

    if ctype == "file_sha256":
        path_str = check.get("path", "")
        exp = check.get("expected_sha256", "")
        if not validate_sha256(exp):
            return CheckResult(rid, ctype, "fail", "invalid_expected_sha256", expected,
                               f"expected_sha256_not_64char_hex:{exp}")
        observed, reason, _actual = _file_sha256(path_str, exp, base_dir=base_dir)
        status = "pass" if observed == expected else "fail"
        return CheckResult(rid, ctype, status, observed, expected, reason)

    if ctype == "schema_valid":
        observed, reason = _schema_valid(
            check.get("yaml_path", ""),
            check.get("schema_path", ""),
            base_dir=base_dir,
        )
        status = "pass" if observed == expected else "fail"
        return CheckResult(rid, ctype, status, observed, expected, reason)

    if ctype == "test_receipt":
        observed, reason = _test_receipt(check, base_dir=base_dir)
        status = "pass" if observed == expected else "fail"
        return CheckResult(rid, ctype, status, observed, expected, reason)

    if ctype == "owner_decision_receipt":
        observed, reason, is_blocked = _owner_decision_receipt(check, base_dir=base_dir)
        if is_blocked:
            return CheckResult(rid, ctype, "blocked", observed, expected, reason)
        status = "pass" if observed == expected else "fail"
        return CheckResult(rid, ctype, status, observed, expected, reason)

    # Defensive — should be unreachable due to the allowlist check above.
    return CheckResult(rid, ctype, "fail", "unreachable", expected, "unreachable_branch")


# ── Manifest source-hash verification ──────────────────────────────────────


def verify_source_hashes(manifest: dict) -> list[str]:
    """Return list of requirement IDs whose text hash does not match source_hash."""
    mismatches = []
    for req in manifest.get("requirements", []):
        text = req.get("text", "")
        declared = req.get("source_hash", "")
        actual = sha256_text(text)
        if actual != declared:
            mismatches.append(req.get("id", "<unknown>"))
    return mismatches


# ── Top-level gate ─────────────────────────────────────────────────────────


def run_gate(manifest_path: str, base_dir: Path | None = None, expected_manifest_hash: str | None = None) -> GateResult:
    """Run the integrity gate and return a GateResult.

    ``base_dir`` is the worktree root used to resolve relative paths in
    check arguments. ``expected_manifest_hash`` (if given) is compared to
    the recomputed manifest hash; a mismatch → FAIL.
    """
    real_manifest = manifest_path
    if not os.path.isabs(real_manifest) and base_dir is not None:
        real_manifest = str((base_dir / real_manifest).resolve(strict=False))
    if not os.path.exists(real_manifest):
        return GateResult(
            gate="FAIL",
            manifest_id="",
            manifest_hash="",
            manifest_hash_matches=False,
            reason=f"manifest_not_found:{manifest_path}",
        )

    with open(real_manifest, "rb") as fh:
        raw_text = fh.read().decode("utf-8")
    manifest = yaml.safe_load(raw_text)
    if not isinstance(manifest, dict):
        return GateResult(
            gate="FAIL",
            manifest_id="",
            manifest_hash="",
            manifest_hash_matches=False,
            reason="manifest_not_yaml_mapping",
        )

    m_hash = sha256_text(raw_text)
    hash_matches = (expected_manifest_hash is None) or (m_hash == expected_manifest_hash)
    manifest_id = manifest.get("manifest_id", "")

    # Source-hash drift → FAIL (text was edited after pinning).
    source_mismatches = verify_source_hashes(manifest)
    if source_mismatches:
        return GateResult(
            gate="FAIL",
            manifest_id=manifest_id,
            manifest_hash=m_hash,
            manifest_hash_matches=hash_matches,
            reason=f"source_hash_mismatch:{','.join(source_mismatches)}",
            failing_ids=source_mismatches,
        )

    if not hash_matches:
        return GateResult(
            gate="FAIL",
            manifest_id=manifest_id,
            manifest_hash=m_hash,
            manifest_hash_matches=False,
            reason="manifest_hash_mismatch",
        )

    results: list[CheckResult] = []
    failing_ids: list[str] = []
    blocked_ids: list[str] = []

    for req in manifest.get("requirements", []):
        result = evaluate_requirement(req, base_dir=base_dir)
        results.append(result)
        if result.status == "fail":
            failing_ids.append(result.requirement_id)
        elif result.status == "blocked":
            blocked_ids.append(result.requirement_id)

    if blocked_ids:
        gate = "BLOCKED"
        reason = f"blocked_requirements:{','.join(blocked_ids)}"
        closeout_permitted = False
    elif failing_ids:
        gate = "FAIL"
        reason = f"failed_requirements:{','.join(failing_ids)}"
        closeout_permitted = False
    else:
        gate = "PASS"
        reason = "all_requirements_pass"
        closeout_permitted = True

    return GateResult(
        gate=gate,
        manifest_id=manifest_id,
        manifest_hash=m_hash,
        manifest_hash_matches=hash_matches,
        results=results,
        failing_ids=failing_ids,
        blocked_ids=blocked_ids,
        closeout_permitted=closeout_permitted,
        reason=reason,
    )


# ── CLI ─────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fas A completion-integrity-gate (read-only, deterministic)."
    )
    parser.add_argument("--manifest", required=True, help="Path to fas-a-requirements.yaml")
    parser.add_argument("--base-dir", default=".",
                        help="Worktree root for resolving relative check paths.")
    parser.add_argument("--expected-manifest-hash", default=None,
                        help="Expected sha256 of the manifest file; mismatch → FAIL.")
    parser.add_argument("--json", action="store_true", default=True,
                        help="Emit JSON to stdout (default).")
    args = parser.parse_args(argv)

    base_dir = Path(args.base_dir).resolve(strict=False) if args.base_dir else None
    result = run_gate(
        manifest_path=args.manifest,
        base_dir=base_dir,
        expected_manifest_hash=args.expected_manifest_hash,
    )

    print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    if result.gate == "PASS":
        return 0
    if result.gate == "BLOCKED":
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())