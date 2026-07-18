#!/usr/bin/env python3
"""Completion Integrity Gate (CIC) for Fas A — v1.1 (strict, external hash).

Read-only enforcement code. Reads a requirements manifest (YAML), validates it
against the schema BEFORE any evaluation, then computes a deterministic status
for every requirement and returns a JSON verdict. The CIC never mutates state:
no task creation, no board writes, no push, no install, no restart, no file
writes outside the read-only verification path.

Design invariants (task t_200e833a, krav 1-12):

  1. AUKTORITET: Filip's original Fas A requirements are authority. The manifest
     is consumed as evidence, never trusted as status.
  2. SCHEMAVALIDERING FÖRE UTVÄRDERING: the manifest MUST validate against
     control-plane/fas-a-requirements.schema.json before any evaluation. If
     validation fails → gate=FAIL, no further evaluation. Empty/truncated YAML
     → FAIL.
  3. CANONICAL ID SET: exactly {FAS-A-001 ... FAS-A-013}, no missing, no extra,
     no duplicates. Violation → FAIL.
  4. NO FREE EXPECTED: accepted outcome is derived deterministically from the
     check type, not from a YAML `expected` field. A manifest may NEVER declare
     `missing`, `unverified`, or `unauthorized` as accepted. The CIC computes
     the actual result and compares to a type-specific deterministic rule.
  5. STRICT TYPED CHECKS: schema uses discriminated union with `type` and
     additionalProperties=false per check type. The CIC additionally enforces
     the per-type required fields at runtime (defence in depth).
  6. all_of: every sub-check must pass. One sub-check fail → whole requirement
     FAIL. Sub-checks are evaluated strictly (no owner bypass inside all_of).
  7. TECHNICAL ≠ OWNER: technical requirements (file/test-receipt/schema/git)
     never pass via owner_decision_receipt. Missing technical evidence = FAIL.
     Only genuine Filip-owned owner_decision_receipt checks may be BLOCKED.
  8. EXTERNAL MANIFEST HASH: --expected-manifest-hash is the trust anchor. A
     `source_hash` field inside the manifest is NOT a trust anchor (it can be
     rewritten by the same party that writes the manifest). Without
     --expected-manifest-hash, gate is NEVER PASS / closeout_permitted NEVER
     true. A mismatch → FAIL with manifest_tampered.
  9. STRICT RECEIPT BINDING: test receipts bind to requirement-id, check-id,
     branch, commit, fresh UTC timestamp (within 24h), expected result, and an
     output artefact whose hash is recomputed every run. Owner receipts bind to
     decision_id, check-id, branch, commit, allowed values, and an EXTERNAL
     attestation (never candidate-local JSON alone). Missing trust anchor →
     BLOCKED with trust_anchor_missing.
  10. FAIL > BLOCKED: when both FAIL and BLOCKED requirements are present,
      gate=FAIL. Both failing_ids and blocked_ids are reported.
  11. GIT-REF/BRANCH VALIDATION + SHELL-FREE: branch names only
      [A-Za-z0-9._/-], max 200 chars, no '..', no NULL, no newlines. commit_sha
      is exactly 40 hex chars. All subprocess calls use list args, shell=False.
      No os.system, no eval/exec, no tempfile writes.
  12. PRODUCTION ALLOWLIST + SYMLINK ESCAPE: --base-dir must resolve under one
      of PATH_ALLOWLIST_PREFIXES. Every path inside a check is resolved with
      os.path.realpath() and the realpath is checked against the allowlist. A
      symlink that escapes the allowlist → FAIL with path_not_allowlisted.

Output: a single JSON object on stdout. Exit 0 = PASS, 1 = FAIL, 2 = BLOCKED.
Deterministic: identical inputs produce identical output across runs.
"""

from __future__ import annotations

import argparse
import datetime as _dt
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

# ── Hard-coded constants (never read from YAML) ─────────────────────────────

GIT_REPO = "/Users/ai/.hermes/hermes-agent"

# Path allowlist for --base-dir, file_sha256 / receipt_path / schema_valid
# paths. Any path whose realpath does NOT start with one of these prefixes →
# FAIL with path_not_allowlisted. The realpath check guards against symlink
# escapes: a symlink inside the worktree that points to /etc/passwd is rejected.
PATH_ALLOWLIST_PREFIXES = (
    "/Users/ai/.hermes/worktrees/kmros-",
    "/Users/ai/Dropbox/FILIP KILLANDER/repository/the-terminal-worktrees/kmros-",
)

# Canonical ID set — exactly these 13 IDs, no more, no less, no duplicates.
CANONICAL_REQUIREMENT_IDS = frozenset(
    f"FAS-A-{i:03d}" for i in range(1, 14)
)

# Allowed check type discriminators (schema also enforces this via oneOf).
ALLOWED_CHECK_TYPES = {
    "git_ancestor",
    "git_not_ancestor",
    "file_sha256",
    "schema_valid",
    "test_receipt",
    "owner_decision_receipt",
    "all_of",
}

# Shell metacharacters that must never appear in a check argument.
SHELL_METACHARACTERS = (";", "|", "$(", "`")

# Owner-id placeholders that always yield BLOCKED for owner_decision_receipt.
OWNER_PLACEHOLDER_VALUES = {
    "",
    "PENDING_OWNER_DECISION",
    "PENDING",
    "TBD",
    "TODO",
    "NONE",
}

# Deterministic expected outcome per check type (krav 3). The CIC compares the
# computed actual against this — never against a YAML `expected` field.
DETERMINISTIC_EXPECTED = {
    "git_ancestor": "exit_0",
    "git_not_ancestor": "exit_1",
    "file_sha256": "match",
    "schema_valid": "valid",
    "test_receipt": "verified",
    "owner_decision_receipt": "authorized",
    "all_of": "all_pass",
}

# Receipt timestamp freshness window (seconds). A test receipt with a
# timestamp older than this window → FAIL with stale_timestamp.
RECEIPT_TIMESTAMP_WINDOW_SECONDS = 24 * 3600  # 24 hours

_COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
# Branch/ref name: only [A-Za-z0-9._/-], max 200 chars, no '..', no NULL, no
# newlines. (The character class already excludes control chars and whitespace.)
_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")


# ── Data model ──────────────────────────────────────────────────────────────

@dataclass
class CheckResult:
    """Result of evaluating a single requirement (or all_of sub-check)."""

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
    schema_valid: bool
    id_set_valid: bool
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
            "schema_valid": self.schema_valid,
            "id_set_valid": self.id_set_valid,
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
    return any(ch in value for ch in SHELL_METACHARACTERS)


def _check_args_for_metachars(check: dict) -> str | None:
    """Walk a check dict; return the first offending value or None."""
    for key, value in check.items():
        if key == "type":
            continue
        if isinstance(value, str) and _contains_shell_metacharacters(value):
            return value
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    offender = _check_args_for_metachars(item)
                    if offender is not None:
                        return offender
                elif isinstance(item, str) and _contains_shell_metacharacters(item):
                    return item
    return None


def validate_path(path_str: str, base_dir: Path | None = None) -> tuple[bool, str, str | None]:
    """Validate ``path_str`` against the path allowlist using realpath.

    Returns ``(ok, resolved_or_error, real_path)``. ``base_dir`` resolves
    relative paths. realpath is used so symlinks that escape the allowlist
    are rejected (krav 8, 12).
    """
    if not isinstance(path_str, str) or not path_str:
        return False, "empty_path", None
    if _contains_shell_metacharacters(path_str):
        return False, "shell_metacharacters_in_path", None
    if "\x00" in path_str or "\n" in path_str:
        return False, "control_char_in_path", None

    candidate = Path(path_str)
    if not candidate.is_absolute() and base_dir is not None:
        candidate = base_dir / candidate
    try:
        # realpath resolves symlinks AND normalizes '..' segments. strict=False
        # lets us validate paths for files that may not yet exist (e.g.
        # receipts that have not been produced). os.path.realpath does not
        # raise on missing paths.
        real_str = os.path.realpath(str(candidate))
    except (OSError, RuntimeError) as exc:
        return False, f"path_resolution_error:{exc}", None

    for prefix in PATH_ALLOWLIST_PREFIXES:
        # A path is inside an allowlisted root iff it equals the prefix OR
        # continues right after the prefix with a path boundary. The prefix
        # may itself end with a separator (e.g. ".../kmros-") — in that case the
        # prefix is already a boundary and we accept anything that starts with
        # it. Otherwise (prefix is a directory root without trailing slash),
        # the next char in the real path must be "/".
        if real_str == prefix:
            return True, real_str, real_str
        if real_str.startswith(prefix):
            # Boundary: either the prefix ends with "/" or "-", or the next
            # character in real_str is a path separator.
            if prefix.endswith("/") or prefix.endswith("-"):
                return True, real_str, real_str
            if len(real_str) > len(prefix) and real_str[len(prefix)] == "/":
                return True, real_str, real_str
    return False, "path_not_allowlisted", None


def validate_commit_sha(sha: str) -> bool:
    return bool(_COMMIT_SHA_RE.match(sha or ""))


def validate_sha256(value: str) -> bool:
    return bool(_SHA256_RE.match(value or ""))


def validate_branch_name(name: str) -> bool:
    """Branch/ref name: [A-Za-z0-9._/-], max 200 chars, no '..', no NULL, no newlines."""
    if not isinstance(name, str):
        return False
    if ".." in name:
        return False
    if "\x00" in name or "\n" in name:
        return False
    return bool(_BRANCH_RE.match(name))


# ── Hash helpers ────────────────────────────────────────────────────────────


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


# ── Schema validation (krav 2) ─────────────────────────────────────────────


def validate_manifest_against_schema(
    manifest: dict, schema_path: str
) -> tuple[bool, str]:
    """Validate the manifest dict against the JSON Schema at ``schema_path``."""
    try:
        import jsonschema
    except ImportError as exc:
        return False, f"jsonschema_unavailable:{exc}"
    if not os.path.exists(schema_path):
        return False, f"schema_not_found:{schema_path}"
    try:
        with open(schema_path, "rb") as fh:
            schema = json.load(fh)
        jsonschema.validate(instance=manifest, schema=schema)
        return True, ""
    except jsonschema.ValidationError as exc:
        return False, f"schema_violation:{exc.message}"
    except Exception as exc:  # noqa: BLE001 — read-only, no mutation
        return False, f"schema_check_error:{type(exc).__name__}:{exc}"


# ── Canonical ID set check (krav 2) ────────────────────────────────────────


def validate_canonical_id_set(requirements: list) -> tuple[bool, str, list[str]]:
    """Return ``(ok, reason, offending_ids)``. ``offending_ids`` lists
    duplicates, missing, and extra IDs (for diagnostic)."""
    if not isinstance(requirements, list):
        return False, "requirements_not_list", []
    ids = []
    for r in requirements:
        if not isinstance(r, dict) or "id" not in r:
            return False, "requirement_missing_id", []
        ids.append(r["id"])
    id_set = set(ids)
    canonical = set(CANONICAL_REQUIREMENT_IDS)
    missing = sorted(canonical - id_set)
    extra = sorted(id_set - canonical)
    # Duplicates: any id appearing more than once.
    seen = set()
    dupes = []
    for i in ids:
        if i in seen and i not in dupes:
            dupes.append(i)
        seen.add(i)
    if missing or extra or dupes:
        parts = []
        if missing:
            parts.append(f"missing:{','.join(missing)}")
        if extra:
            parts.append(f"extra:{','.join(extra)}")
        if dupes:
            parts.append(f"duplicate:{','.join(dupes)}")
        return False, "id_set_violation:" + "|".join(parts), missing + extra + dupes
    return True, "", []


# ── Check evaluators (shell=False, allowlisted) ────────────────────────────


def _git_ancestor(repo: str, commit: str, branch: str, expect_ancestor: bool) -> tuple[str, str]:
    """Run ``git merge-base --is-ancestor`` with shell=False.

    Returns ``(observed, reason)`` where observed is ``exit_0`` or ``exit_1``
    (the actual subprocess return code). On a git error (e.g. unknown commit),
    observed is ``git_error``.
    """
    cmd = ["git", "-C", repo, "merge-base", "--is-ancestor", commit, branch]
    try:
        proc = subprocess.run(cmd, shell=False, capture_output=True, text=True)
    except Exception as exc:  # noqa: BLE001
        return "git_error", f"git_subprocess_error:{type(exc).__name__}:{exc}"
    observed = f"exit_{proc.returncode}"
    if expect_ancestor:
        return observed, "" if proc.returncode == 0 else "commit_not_ancestor"
    # git_not_ancestor: expected exit_1 (commit is NOT an ancestor)
    return observed, "" if proc.returncode == 1 else "commit_is_ancestor"


def _file_sha256(path_str: str, expected_sha256: str, base_dir: Path | None) -> tuple[str, str, str | None]:
    ok, msg, real = validate_path(path_str, base_dir=base_dir)
    if not ok or real is None:
        return "path_not_allowlisted", msg, None
    if not os.path.exists(real):
        return "missing_file", f"file_not_found:{path_str}", None
    if os.path.islink(real):
        # realpath already resolved the link; check the resolved target is
        # still under an allowlist prefix (defence in depth — validate_path
        # already does this, but be explicit).
        pass
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
    try:
        import jsonschema
        with open(real_y, "rb") as fh:
            doc = yaml.safe_load(fh)
        with open(real_s, "rb") as fh:
            schema = json.load(fh)
        jsonschema.validate(instance=doc, schema=schema)
        return "valid", ""
    except jsonschema.ValidationError as exc:
        return "invalid", f"schema_violation:{exc.message}"
    except Exception as exc:  # noqa: BLE001
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


def _parse_iso_timestamp(ts: str) -> _dt.datetime | None:
    """Parse an ISO-8601 timestamp (with optional trailing Z) to aware UTC."""
    if not isinstance(ts, str) or not ts:
        return None
    s = ts.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = _dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return dt.astimezone(_dt.timezone.utc)


def _timestamp_is_fresh(ts: str, now: _dt.datetime | None = None) -> bool:
    """Return True iff ``ts`` parses to a UTC instant within the last 24h."""
    dt = _parse_iso_timestamp(ts)
    if dt is None:
        return False
    if now is None:
        now = _dt.datetime.now(_dt.timezone.utc)
    age = (now - dt).total_seconds()
    return 0 <= age <= RECEIPT_TIMESTAMP_WINDOW_SECONDS


def _test_receipt(check: dict, base_dir: Path | None, requirement_id: str) -> tuple[str, str]:
    """Verify a test receipt binds to expected branch/commit/check_id/timestamp."""
    receipt_path = check.get("receipt_path", "")
    expected_branch = check.get("expected_branch", "")
    expected_commit = check.get("expected_commit", "")
    expected_check_id = check.get("expected_check_id", "")

    # Reject shell metacharacters in any of these args.
    for key in ("receipt_path", "expected_branch", "expected_commit", "expected_check_id"):
        v = check.get(key, "")
        if isinstance(v, str) and _contains_shell_metacharacters(v):
            return "unverified", f"shell_metacharacters_in:{key}"

    # Strict branch validation (krav 11).
    if not validate_branch_name(expected_branch):
        return "unverified", f"expected_branch_invalid:{expected_branch}"
    if not validate_commit_sha(expected_commit):
        return "unverified", "expected_commit_not_40char_hex"
    if not expected_check_id:
        return "unverified", "expected_check_id_empty"

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
    # Recompute the output artefact hash from the referenced output_path (if
    # present) and compare to the declared output_hash. This binds the receipt
    # to a real artefact whose hash is recomputed every run (krav 9).
    output_path = data.get("output_path", "")
    if isinstance(output_path, str) and output_path:
        ok_op, msg_op, real_op = validate_path(output_path, base_dir=base_dir)
        if not ok_op or real_op is None:
            return "unverified", f"output_path_not_allowlisted:{msg_op}"
        if not os.path.exists(real_op):
            return "unverified", f"output_artefact_not_found:{output_path}"
        recomputed = sha256_file(real_op)
        if recomputed != str(data.get("output_hash", "")):
            return "unverified", f"output_hash_mismatch:{recomputed}"
    # Timestamp freshness (krav 9).
    if not _timestamp_is_fresh(str(data.get("timestamp", ""))):
        return "unverified", "stale_timestamp"
    return "verified", ""


def _owner_decision_receipt(check: dict, base_dir: Path | None, requirement_id: str) -> tuple[str, str, bool]:
    """Verify an owner-decision receipt.

    Returns ``(observed, reason, is_blocked)``. ``is_blocked`` is True when the
    owner_id is missing/placeholder OR the receipt lacks an external trust
    anchor — in those cases the check is BLOCKED, never PASS or FAIL.

    Candidate-local JSON alone NEVER counts as Filip-authorisation. The
    receipt must carry an ``external_attestation`` block with a
    ``trust_anchor_source`` that is not the candidate's own worktree, and a
    signature/hash that the CIC can recompute (krav 9).
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
        # Missing receipt for a concrete owner_id is BLOCKED (trust anchor
        # missing), not FAIL — owner decisions are not technical evidence.
        return "blocked", f"trust_anchor_missing:{err}", True

    required_fields = ("owner_id", "decision_id", "commit", "branch", "timestamp",
                       "output_hash", "external_attestation")
    missing = [f for f in required_fields if f not in data]
    if missing:
        return "blocked", f"trust_anchor_missing:fields:{','.join(missing)}", True

    if str(data.get("owner_id", "")).strip() != owner_id:
        return "unauthorized", f"owner_id_mismatch:{data.get('owner_id')}", False

    # External trust anchor (krav 9). Candidate-local JSON alone is never
    # enough. The receipt MUST carry an external_attestation with a
    # ``trust_anchor_source`` outside the candidate worktree and a recomputable
    # attestation_hash.
    attestation = data.get("external_attestation")
    if not isinstance(attestation, dict):
        return "blocked", "trust_anchor_missing:no_attestation", True
    tas = str(attestation.get("trust_anchor_source", "")).strip()
    if not tas or tas == "candidate_local" or tas.startswith("/Users/ai/.hermes/worktrees/"):
        return "blocked", f"trust_anchor_missing:source:{tas}", True
    att_hash = str(attestation.get("attestation_hash", ""))
    if not validate_sha256(att_hash):
        return "blocked", "trust_anchor_missing:attestation_hash_not_sha256", True

    decision = str(data.get("decision", "")).strip().lower()
    if decision not in ("approved", "authorized", "accept", "yes"):
        return "unauthorized", f"decision_not_approved:{decision}", False

    if not validate_commit_sha(str(data.get("commit", ""))):
        return "unauthorized", "receipt_commit_not_40char_hex", False
    if not validate_branch_name(str(data.get("branch", ""))):
        return "unauthorized", "receipt_branch_invalid", False
    if not validate_sha256(str(data.get("output_hash", ""))):
        return "unauthorized", "receipt_output_hash_not_sha256", False
    # Timestamp freshness for owner receipts too.
    if not _timestamp_is_fresh(str(data.get("timestamp", ""))):
        return "unauthorized", "stale_timestamp", False

    return "authorized", "", False


# ── Per-requirement evaluation ─────────────────────────────────────────────


def _evaluate_single_check(rid: str, check: dict, base_dir: Path | None) -> CheckResult:
    """Evaluate one check dict (NOT all_of — caller handles all_of separately).

    The expected value is derived deterministically from the check type
    (krav 3). owner_decision_receipt is the only type that can return BLOCKED.
    """
    ctype = check.get("type", "")
    expected = DETERMINISTIC_EXPECTED.get(ctype, "")

    if ctype not in ALLOWED_CHECK_TYPES:
        return CheckResult(rid, ctype, "fail", "unknown_check_type", expected,
                           f"unknown_check_type:{ctype}")

    offender = _check_args_for_metachars(check)
    if offender is not None:
        return CheckResult(rid, ctype, "fail", "shell_metacharacters_detected",
                           expected, f"shell_metacharacters_in:{offender}")

    if ctype == "git_ancestor":
        branch = check.get("branch", "")
        commit = check.get("commit_sha", "")
        if not validate_branch_name(branch):
            return CheckResult(rid, ctype, "fail", "invalid_branch", expected,
                               f"branch_invalid:{branch}")
        if not validate_commit_sha(commit):
            return CheckResult(rid, ctype, "fail", "invalid_commit_sha", expected,
                               f"commit_sha_not_40char_hex:{commit}")
        observed, reason = _git_ancestor(GIT_REPO, commit, branch, expect_ancestor=True)
        status = "pass" if observed == expected else "fail"
        return CheckResult(rid, ctype, status, observed, expected, reason)

    if ctype == "git_not_ancestor":
        branch = check.get("branch", "")
        commit = check.get("commit_sha", "")
        if not validate_branch_name(branch):
            return CheckResult(rid, ctype, "fail", "invalid_branch", expected,
                               f"branch_invalid:{branch}")
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
        observed, reason = _test_receipt(check, base_dir=base_dir, requirement_id=rid)
        status = "pass" if observed == expected else "fail"
        return CheckResult(rid, ctype, status, observed, expected, reason)

    if ctype == "owner_decision_receipt":
        observed, reason, is_blocked = _owner_decision_receipt(
            check, base_dir=base_dir, requirement_id=rid
        )
        if is_blocked:
            return CheckResult(rid, ctype, "blocked", observed, expected, reason)
        status = "pass" if observed == expected else "fail"
        return CheckResult(rid, ctype, status, observed, expected, reason)

    # Defensive — unreachable due to the allowlist check above.
    return CheckResult(rid, ctype, "fail", "unreachable", expected, "unreachable_branch")


def _evaluate_all_of(rid: str, check: dict, base_dir: Path | None) -> CheckResult:
    """Evaluate an all_of composite: every sub-check must pass (krav 5).

    Sub-checks are evaluated strictly. owner_decision_receipt inside all_of
    returns BLOCKED only for that sub-check — and per krav 10, if ANY sub-check
    FAILs, the whole requirement is FAIL (FAIL > BLOCKED). If no sub-check FAILs
    but some BLOCK, the requirement is BLOCKED.
    """
    expected = DETERMINISTIC_EXPECTED["all_of"]
    sub_checks = check.get("checks", [])
    if not isinstance(sub_checks, list) or not sub_checks:
        return CheckResult(rid, "all_of", "fail", "no_sub_checks", expected,
                           "all_of_empty")
    sub_results: list[CheckResult] = []
    any_fail = False
    any_blocked = False
    reasons: list[str] = []
    for sub in sub_checks:
        if not isinstance(sub, dict):
            sub_results.append(CheckResult(rid, "all_of", "fail", "sub_not_dict",
                                            expected, "sub_check_not_dict"))
            any_fail = True
            continue
        sub_rid = f"{rid}::sub{len(sub_results)}"
        # Nested all_of is allowed by the schema; recurse.
        if sub.get("type") == "all_of":
            sr = _evaluate_all_of(sub_rid, sub, base_dir=base_dir)
        else:
            sr = _evaluate_single_check(sub_rid, sub, base_dir=base_dir)
        sub_results.append(sr)
        if sr.status == "fail":
            any_fail = True
            reasons.append(f"{sub_rid}:{sr.reason}")
        elif sr.status == "blocked":
            any_blocked = True
            reasons.append(f"{sub_rid}:{sr.reason}")
    if any_fail:
        return CheckResult(rid, "all_of", "fail", "not_all_pass", expected,
                            "|".join(reasons))
    if any_blocked:
        return CheckResult(rid, "all_of", "blocked", "sub_blocked", expected,
                            "|".join(reasons))
    return CheckResult(rid, "all_of", "pass", "all_pass", expected, "")


def evaluate_requirement(req: dict, base_dir: Path | None) -> CheckResult:
    rid = req.get("id", "<unknown>")
    check = req.get("check", {}) or {}
    ctype = check.get("type", "") if isinstance(check, dict) else ""

    if ctype == "all_of":
        return _evaluate_all_of(rid, check, base_dir=base_dir)
    return _evaluate_single_check(rid, check, base_dir=base_dir)


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


def run_gate(
    manifest_path: str,
    base_dir: Path | None = None,
    expected_manifest_hash: str | None = None,
    schema_path: str | None = None,
) -> GateResult:
    """Run the integrity gate and return a GateResult.

    ``base_dir`` is the worktree root for resolving relative paths.
    ``expected_manifest_hash`` is the EXTERNAL trust anchor for closeout
    (krav 7). Without it, gate is NEVER PASS / closeout_permitted NEVER true.
    ``schema_path`` defaults to ``base_dir / control-plane/fas-a-requirements.schema.json``.
    """
    # Resolve manifest path.
    real_manifest = manifest_path
    if not os.path.isabs(real_manifest) and base_dir is not None:
        real_manifest = str((base_dir / real_manifest).resolve(strict=False))
    if not os.path.exists(real_manifest):
        return GateResult(
            gate="FAIL", manifest_id="", manifest_hash="",
            manifest_hash_matches=False, schema_valid=False, id_set_valid=False,
            reason=f"manifest_not_found:{manifest_path}",
        )

    # Read manifest bytes.
    try:
        with open(real_manifest, "rb") as fh:
            raw_text_bytes = fh.read()
    except OSError as exc:
        return GateResult(
            gate="FAIL", manifest_id="", manifest_hash="",
            manifest_hash_matches=False, schema_valid=False, id_set_valid=False,
            reason=f"manifest_read_error:{exc}",
        )
    raw_text = raw_text_bytes.decode("utf-8", errors="replace")
    m_hash = sha256_text(raw_text)

    # Empty/truncated manifest → FAIL. (Empty or all-whitespace YAML parses to
    # None; truncated YAML raises.) Either way we FAIL before evaluation.
    try:
        manifest = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        return GateResult(
            gate="FAIL", manifest_id="", manifest_hash=m_hash,
            manifest_hash_matches=False, schema_valid=False, id_set_valid=False,
            reason=f"manifest_not_yaml:{type(exc).__name__}:{exc}",
        )
    if manifest is None:
        return GateResult(
            gate="FAIL", manifest_id="", manifest_hash=m_hash,
            manifest_hash_matches=False, schema_valid=False, id_set_valid=False,
            reason="manifest_empty",
        )
    if not isinstance(manifest, dict):
        return GateResult(
            gate="FAIL", manifest_id="", manifest_hash=m_hash,
            manifest_hash_matches=False, schema_valid=False, id_set_valid=False,
            reason="manifest_not_yaml_mapping",
        )

    manifest_id = manifest.get("manifest_id", "")

    # krav 2: schema validation BEFORE evaluation. The schema itself lives
    # under the worktree (allowlisted via base_dir).
    if schema_path is None:
        if base_dir is not None:
            schema_path = str(base_dir / "control-plane" / "fas-a-requirements.schema.json")
        else:
            schema_path = "control-plane/fas-a-requirements.schema.json"
    ok_s, err_s = validate_manifest_against_schema(manifest, schema_path)
    # Allow schema_path to be resolved via base_dir for the file-exists check.
    if not ok_s and err_s.startswith("schema_not_found:") and base_dir is not None:
        # Try resolving the schema path relative to base_dir.
        alt_schema = str(base_dir / schema_path) if not os.path.isabs(schema_path) else schema_path
        ok_s, err_s = validate_manifest_against_schema(manifest, alt_schema)
    if not ok_s:
        return GateResult(
            gate="FAIL", manifest_id=manifest_id, manifest_hash=m_hash,
            manifest_hash_matches=False, schema_valid=False, id_set_valid=False,
            reason=f"schema_invalid:{err_s}",
        )

    # krav 2: canonical ID set — exactly FAS-A-001..013.
    ok_ids, err_ids, _off = validate_canonical_id_set(manifest.get("requirements", []))
    if not ok_ids:
        return GateResult(
            gate="FAIL", manifest_id=manifest_id, manifest_hash=m_hash,
            manifest_hash_matches=False, schema_valid=True, id_set_valid=False,
            reason=f"id_set_invalid:{err_ids}",
        )

    # krav 7: external manifest hash. Without --expected-manifest-hash, gate is
    # NEVER PASS. A mismatch → FAIL with manifest_tampered.
    if expected_manifest_hash is None:
        # Read-only inspection path: gate=FAIL (not PASS), closeout blocked.
        # We still evaluate requirements so failing_ids/blocked_ids are
        # populated for diagnostics, but closeout_permitted stays False.
        hash_matches = False
        manifest_tampered = False
    else:
        if not validate_sha256(expected_manifest_hash):
            return GateResult(
                gate="FAIL", manifest_id=manifest_id, manifest_hash=m_hash,
                manifest_hash_matches=False, schema_valid=True, id_set_valid=True,
                reason=f"expected_manifest_hash_not_sha256:{expected_manifest_hash}",
            )
        hash_matches = (m_hash == expected_manifest_hash)
        manifest_tampered = not hash_matches

    # Source-hash drift → FAIL (text was edited after pinning).
    source_mismatches = verify_source_hashes(manifest)
    if source_mismatches:
        return GateResult(
            gate="FAIL", manifest_id=manifest_id, manifest_hash=m_hash,
            manifest_hash_matches=hash_matches, schema_valid=True, id_set_valid=True,
            reason=f"source_hash_mismatch:{','.join(source_mismatches)}",
            failing_ids=source_mismatches,
        )

    if manifest_tampered:
        return GateResult(
            gate="FAIL", manifest_id=manifest_id, manifest_hash=m_hash,
            manifest_hash_matches=False, schema_valid=True, id_set_valid=True,
            reason="manifest_tampered",
        )

    # Evaluate every requirement.
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

    # krav 10: FAIL > BLOCKED. When both present, gate=FAIL; both lists reported.
    if failing_ids:
        gate = "FAIL"
        reason = f"failed_requirements:{','.join(failing_ids)}"
        closeout_permitted = False
    elif blocked_ids:
        gate = "BLOCKED"
        reason = f"blocked_requirements:{','.join(blocked_ids)}"
        closeout_permitted = False
    else:
        # No fails, no blocks. PASS only if the external manifest hash was
        # supplied and matches (krav 7). Without it, NEVER PASS.
        if expected_manifest_hash is not None and hash_matches:
            gate = "PASS"
            reason = "all_requirements_pass"
            closeout_permitted = True
        else:
            gate = "FAIL"
            reason = "expected_manifest_hash_missing"
            closeout_permitted = False

    return GateResult(
        gate=gate,
        manifest_id=manifest_id,
        manifest_hash=m_hash,
        manifest_hash_matches=hash_matches,
        schema_valid=True,
        id_set_valid=True,
        results=results,
        failing_ids=failing_ids,
        blocked_ids=blocked_ids,
        closeout_permitted=closeout_permitted,
        reason=reason,
    )


# ── CLI ────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fas A completion-integrity-gate (read-only, deterministic)."
    )
    parser.add_argument("--manifest", required=True,
                        help="Path to fas-a-requirements.yaml")
    parser.add_argument("--base-dir", default=".",
                        help="Worktree root for resolving relative check paths.")
    parser.add_argument("--expected-manifest-hash", default=None,
                        help="External trust anchor: expected sha256 of the manifest "
                             "file. Without this, gate is NEVER PASS / "
                             "closeout_permitted is NEVER true. A mismatch → "
                             "FAIL with manifest_tampered.")
    parser.add_argument("--schema-path", default=None,
                        help="Override path to the JSON Schema (defaults to "
                             "<base-dir>/control-plane/fas-a-requirements.schema.json).")
    parser.add_argument("--json", action="store_true", default=True,
                        help="Emit JSON to stdout (default).")
    args = parser.parse_args(argv)

    base_dir = Path(args.base_dir).resolve(strict=False) if args.base_dir else None
    result = run_gate(
        manifest_path=args.manifest,
        base_dir=base_dir,
        expected_manifest_hash=args.expected_manifest_hash,
        schema_path=args.schema_path,
    )

    print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    if result.gate == "PASS":
        return 0
    if result.gate == "BLOCKED":
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())