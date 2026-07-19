#!/usr/bin/env python3
"""Completion Integrity Gate (CIC) for Fas A — v3 (schema-realpath, dual status, FAS-A-012 regression).

Read-only enforcement code. Reads a requirements manifest (YAML), validates it
against the schema BEFORE any evaluation, then computes a deterministic status
for every requirement and returns a JSON verdict. The CIC never mutates state:
no task creation, no board writes, no push, no install, no restart, no file
writes outside the read-only verification path.

v3 corrections (task t_9f3f941e, on top of v2 commit 4c579a1b0):

  A. SCHEMA-REALPATH: run_gate resolves the top-level schema path via
     validate_path and opens/validates EXACTLY the returned realpath. The raw
     relative schema_path is never opened after containment. The cwd-
     dependent fallback (base_dir / schema_path) was removed. A different
     cwd with a permissive schema on the same relative path no longer
     influences the result — only the schema under base_dir is used.
  B. DUAL STATUS in all_of: a composite all_of with BOTH a FAIL sub-check
     and a BLOCKED sub-check sets also_blocked=True on the CheckResult and
     the top-level gate lists the requirement ID in BOTH failing_ids and
     blocked_ids (deduplicated, stable order). FAIL still drives the gate;
     both lists are always preserved (krav 6/10).
  C. FAS-A-012-REGRESSION: an all_of pinned to an approved digest that is
     then rewritten to an owner-only check (technical part removed) changes
     the manifest digest and yields manifest_tampered (FAIL), not a
     trust_context_missing PASS-by-skip.
  D. SCOPE: no owner provider, Keychain, GPG, ledger, central worker-guard,
     or runtime integration. CLI/default stays fail-closed.

Design invariants (task t_e5b2a010, krav 1-10):

  1. EN AUKTORITET: The requirements manifest is the single semantic authority.
     No hardcoded REQUIREMENT_SEMANTICS table in Python. CIC reads requirement
     texts, source_hash, check type, and all_of structure from the manifest.
  2. TRUST-GRÄNS: CLI/default mode has no trusted trust context and therefore
     NEVER sets closeout_permitted=true. An injectable TrustContext protocol
     allows future harness integration. Without verified trust context: fail-
     closed, closeout_permitted=false. Test-only fake verifier via DI, never
     exposed via CLI.
  3. MANIFESTBINDNING: A verified trust context binds the exact manifest
     bytes/digest. Any change to ID, text, source_hash, check type, all_of
     structure, or check arguments changes the digest and gives
     manifest_tampered. Without trust context: fail-closed, no PASS.
  4. STRICT TEST RECEIPTS: Exact strict schema (additionalProperties=false
     equivalent) with required fields: requirement_id, check_id, branch, commit,
     result (exactly "pass"), timestamp (valid ISO-8601 UTC, NO freshness
     window), output_path (required, must exist within base_dir), output_hash
     (sha256, recomputed from artefact), receipt_hash (sha256, recomputed from
     receipt bytes). Mismatch, missing field, or untrusted receipt binding → FAIL.
  5. OWNER RECEIPTS: Without trust context, owner decision receipts ALWAYS give
     BLOCKED in production/CLI mode, never authorized from self-declared JSON.
     Test-only trust context can authorize for test purposes only.
  6. FAS-A-012: all_of of technical current-truth evidence (file_sha256) and
     separate owner decision (owner_decision_receipt). Missing technical = FAIL;
     missing owner trust = BLOCKED; FAIL has priority; both ID lists reported.
  7. INGA PÅHITTADE IMPLEMENTATION PATHS: Neutral evidence-receipt-paths under
     control-plane/evidence/ in the manifest. No hardcoded paths to
     gateway/channel_route_contract.py, scripts/fleet_collector.py, etc.
  8. PATH CONTAINMENT: Manifest, schema, receipts, and output artefacts bound to
     exact realpath(base_dir). Sibling worktrees, absolute paths outside base_dir,
     traversal, and symlink-escape are rejected. PATH_ALLOWLIST_PREFIXES replaced
     with base_dir-containment.
  9. GIT REF: Branch/ref validated with git check-ref-format semantics, shell=False,
     list-args. Rejects leading dash, leading slash, --help, -x, .., control chars.
  10. STATUS: Technical errors → FAIL. Genuine missing owner/harness trust →
      BLOCKED. FAIL has priority over BLOCKED and both lists are always preserved.
      Without trusted trust, closeout_permitted is never true.

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
from typing import Any, Protocol, runtime_checkable

import yaml

# ── Hard-coded constants (never read from YAML) ─────────────────────────────

GIT_REPO = "/Users/ai/.hermes/hermes-agent"

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

# Required fields in a test receipt (strict, additionalProperties=false equivalent).
# No freshness window — timestamp must be valid ISO-8601 UTC but age is not checked.
REQUIRED_RECEIPT_FIELDS = frozenset({
    "requirement_id",
    "check_id",
    "branch",
    "commit",
    "result",
    "timestamp",
    "output_path",
    "output_hash",
    "receipt_hash",
})

_COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


# ── TrustContext protocol (krav 2) ──────────────────────────────────────────


@runtime_checkable
class TrustContext(Protocol):
    """Injectable trust context for the CIC.

    Production CLI has no trust context (None default). Without trust context,
    the CIC is fail-closed: closeout_permitted is never true.

    A real trust provider (future harness) will implement this interface to:
    - verify_manifest_digest: verify the manifest digest matches an approved digest.
    - verify_receipt_binding: verify a receipt's binding is trusted.
    - verify_owner_decision: verify an owner decision is authorized.

    The actual trust verifier and owner provisioning are NOT implemented in this
    candidate. Test-only fake verifiers implement this protocol via DI in tests.
    """

    def verify_manifest_digest(self, digest: str) -> bool:
        """Return True iff the manifest digest matches an approved digest."""
        ...

    def verify_receipt_binding(
        self, receipt_hash: str, requirement_id: str, check_id: str
    ) -> bool:
        """Return True iff the receipt binding is trusted."""
        ...

    def verify_owner_decision(self, owner_id: str, receipt: dict) -> bool:
        """Return True iff the owner decision is authorized."""
        ...


# ── Data model ──────────────────────────────────────────────────────────────


@dataclass
class CheckResult:
    """Result of evaluating a single requirement (or all_of sub-check).

    ``also_blocked`` (v3 dual status) is set ONLY for an all_of composite
    whose gate-driving status is ``fail`` but which also contains at least
    one ``blocked`` sub-check. It lets the top-level gate add the requirement
    ID to BOTH ``failing_ids`` and ``blocked_ids`` (deduplicated, stable
    order), preserving the dual-status signal described in krav 6 / 10.
    A bare FAIL or BLOCKED result never sets this flag.
    """

    requirement_id: str
    check_type: str
    status: str  # "pass" | "fail" | "blocked"
    observed: str
    expected: str
    reason: str = ""
    also_blocked: bool = False


@dataclass
class GateResult:
    """Top-level gate result."""

    gate: str  # "PASS" | "FAIL" | "BLOCKED"
    manifest_id: str
    manifest_hash: str
    manifest_verified: bool
    schema_valid: bool
    id_set_valid: bool
    trust_context_present: bool
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
            "manifest_verified": self.manifest_verified,
            "schema_valid": self.schema_valid,
            "id_set_valid": self.id_set_valid,
            "trust_context_present": self.trust_context_present,
            "results": [
                {
                    "requirement_id": r.requirement_id,
                    "check_type": r.check_type,
                    "status": r.status,
                    "observed": r.observed,
                    "expected": r.expected,
                    "reason": r.reason,
                    "also_blocked": r.also_blocked,
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


def validate_path(path_str: str, base_dir: Path) -> tuple[bool, str, str | None]:
    """Validate ``path_str`` is within ``base_dir`` using realpath (krav 8).

    Returns ``(ok, resolved_or_error, real_path)``. ``base_dir`` is REQUIRED.
    realpath resolves symlinks AND normalizes '..' segments. A path is inside
    base_dir iff realpath(path) equals realpath(base_dir) or starts with
    realpath(base_dir) + "/". Sibling worktrees, absolute paths outside base_dir,
    traversal, and symlink-escape are rejected with path_not_in_base_dir.
    """
    if not isinstance(path_str, str) or not path_str:
        return False, "empty_path", None
    if _contains_shell_metacharacters(path_str):
        return False, "shell_metacharacters_in_path", None
    if "\x00" in path_str or "\n" in path_str:
        return False, "control_char_in_path", None

    candidate = Path(path_str)
    if not candidate.is_absolute():
        candidate = base_dir / candidate

    try:
        real_str = os.path.realpath(str(candidate))
        base_real = os.path.realpath(str(base_dir))
    except (OSError, RuntimeError) as exc:
        return False, f"path_resolution_error:{exc}", None

    # A path is inside base_dir iff it equals base_real OR continues right
    # after base_real with a path separator.
    if real_str == base_real:
        return True, real_str, real_str
    if real_str.startswith(base_real + "/"):
        return True, real_str, real_str
    return False, "path_not_in_base_dir", None


def validate_commit_sha(sha: str) -> bool:
    return bool(_COMMIT_SHA_RE.match(sha or ""))


def validate_sha256(value: str) -> bool:
    return bool(_SHA256_RE.match(value or ""))


def validate_branch_name(name: str) -> bool:
    """Validate branch/ref name using git check-ref-format (krav 9).

    Uses subprocess.run with shell=False and list args. Returns True iff
    git check-ref-format --branch <name> exits 0. Falls back to regex
    if git is not available. Rejects leading dash, leading slash, --help,
    -x, .., control chars, and other invalid refs.
    """
    if not isinstance(name, str) or not name:
        return False
    if ".." in name:
        return False
    if "\x00" in name or "\n" in name:
        return False
    # Leading dash or slash rejected (git check-ref-format rejects these).
    if name.startswith("-") or name.startswith("/"):
        return False
    try:
        proc = subprocess.run(
            ["git", "check-ref-format", "--branch", name],
            shell=False,
            capture_output=True,
        )
        return proc.returncode == 0
    except (OSError, FileNotFoundError):
        # Fallback: regex that rejects leading [-/], .., control chars.
        return bool(re.match(r"^[A-Za-z0-9._][A-Za-z0-9._/-]{0,199}$", name))


def validate_iso_timestamp(ts: str) -> bool:
    """Validate that ``ts`` is a valid ISO-8601 UTC timestamp (krav 4).

    No freshness window — the timestamp must be valid ISO-8601 UTC but age
    is NOT checked. Commit-bound evidence must NOT be invalidated merely
    because 24 hours have passed; live-freshness belongs to later harness gates.
    """
    if not isinstance(ts, str) or not ts:
        return False
    s = ts.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = _dt.datetime.fromisoformat(s)
    except ValueError:
        return False
    if dt.tzinfo is None:
        return False  # Not explicitly UTC
    return dt.utcoffset() == _dt.timedelta(0)


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


def _compute_receipt_hash(data: dict) -> str:
    """Compute the receipt hash from receipt data, excluding the receipt_hash field.

    The receipt_hash is the sha256 of the canonical JSON (sorted keys, compact
    separators) of the receipt data WITHOUT the receipt_hash field itself. This
    avoids the self-referential problem and is deterministic.
    """
    data_without_hash = {k: v for k, v in data.items() if k != "receipt_hash"}
    canonical = json.dumps(data_without_hash, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ── Schema validation (krav 1) ──────────────────────────────────────────────


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


# ── Canonical ID set check ─────────────────────────────────────────────────


def validate_canonical_id_set(requirements: list) -> tuple[bool, str, list[str]]:
    """Return ``(ok, reason, offending_ids)``."""
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


# ── Check evaluators (shell=False, base_dir containment) ────────────────────


def _git_ancestor(repo: str, commit: str, branch: str, expect_ancestor: bool) -> tuple[str, str]:
    """Run ``git merge-base --is-ancestor`` with shell=False."""
    cmd = ["git", "-C", repo, "merge-base", "--is-ancestor", commit, branch]
    try:
        proc = subprocess.run(cmd, shell=False, capture_output=True, text=True)
    except Exception as exc:  # noqa: BLE001
        return "git_error", f"git_subprocess_error:{type(exc).__name__}:{exc}"
    observed = f"exit_{proc.returncode}"
    if expect_ancestor:
        return observed, "" if proc.returncode == 0 else "commit_not_ancestor"
    return observed, "" if proc.returncode == 1 else "commit_is_ancestor"


def _file_sha256(
    path_str: str, expected_sha256: str, base_dir: Path
) -> tuple[str, str, str | None]:
    ok, msg, real = validate_path(path_str, base_dir=base_dir)
    if not ok or real is None:
        return "path_not_in_base_dir", msg, None
    if not os.path.exists(real):
        return "missing_file", f"file_not_found:{path_str}", None
    actual = sha256_file(real)
    observed = "match" if actual == expected_sha256 else "mismatch"
    return observed, "" if observed == "match" else f"sha256_mismatch:{actual}", actual


def _schema_valid(
    yaml_path: str, schema_path: str, base_dir: Path
) -> tuple[str, str]:
    """Validate a YAML file against a JSON Schema. Returns (observed, reason)."""
    ok_y, msg_y, real_y = validate_path(yaml_path, base_dir=base_dir)
    if not ok_y or real_y is None:
        return "path_not_in_base_dir", f"yaml_path:{msg_y}"
    ok_s, msg_s, real_s = validate_path(schema_path, base_dir=base_dir)
    if not ok_s or real_s is None:
        return "path_not_in_base_dir", f"schema_path:{msg_s}"
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


def _parse_receipt(receipt_path: str, base_dir: Path) -> tuple[dict | None, str]:
    ok, msg, real = validate_path(receipt_path, base_dir=base_dir)
    if not ok or real is None:
        return None, f"path_not_in_base_dir:{msg}"
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


def _test_receipt(
    check: dict, base_dir: Path, requirement_id: str, trust_context: TrustContext | None
) -> tuple[str, str]:
    """Verify a test receipt with strict schema and trust binding (krav 4).

    Strict receipt schema (additionalProperties=false equivalent):
    - requirement_id: required, must match the check's requirement ID
    - check_id: required, must match expected_check_id
    - branch: required, git check-ref-format validated
    - commit: required, 40 hex
    - result: required, exactly "pass"
    - timestamp: required, valid ISO-8601 UTC (NO freshness window)
    - output_path: required, must exist within base_dir
    - output_hash: required, sha256, recomputed from artefact
    - receipt_hash: required, sha256, recomputed from receipt bytes

    Without trust context: untrusted_binding → FAIL (fail-closed).
    """
    receipt_path = check.get("receipt_path", "")
    expected_check_id = check.get("expected_check_id", "")

    # Reject shell metacharacters in check arguments.
    for key in ("receipt_path", "expected_check_id"):
        v = check.get(key, "")
        if isinstance(v, str) and _contains_shell_metacharacters(v):
            return "unverified", f"shell_metacharacters_in:{key}"

    if not expected_check_id:
        return "unverified", "expected_check_id_empty"

    # Parse receipt (path must be within base_dir).
    data, err = _parse_receipt(receipt_path, base_dir=base_dir)
    if data is None:
        return "unverified", err

    # Strict schema: no extra fields, no missing fields.
    extra = set(data.keys()) - REQUIRED_RECEIPT_FIELDS
    if extra:
        return "unverified", f"receipt_extra_fields:{','.join(sorted(extra))}"
    missing = REQUIRED_RECEIPT_FIELDS - set(data.keys())
    if missing:
        return "unverified", f"receipt_missing_fields:{','.join(sorted(missing))}"

    # requirement_id must match the check's requirement ID.
    if str(data.get("requirement_id", "")) != requirement_id:
        return "unverified", f"requirement_id_mismatch:{data.get('requirement_id')}"

    # check_id must match expected_check_id from the manifest.
    if str(data.get("check_id", "")) != expected_check_id:
        return "unverified", f"check_id_mismatch:{data.get('check_id')}"

    # branch must be valid (git check-ref-format).
    branch = str(data.get("branch", ""))
    if not validate_branch_name(branch):
        return "unverified", f"branch_invalid:{branch}"

    # commit must be 40 hex.
    commit = str(data.get("commit", ""))
    if not validate_commit_sha(commit):
        return "unverified", "commit_not_40char_hex"

    # result must be exactly "pass".
    if str(data.get("result", "")) != "pass":
        return "unverified", f"result_not_pass:{data.get('result')}"

    # timestamp must be valid ISO-8601 UTC (NO freshness window).
    timestamp = str(data.get("timestamp", ""))
    if not validate_iso_timestamp(timestamp):
        return "unverified", "timestamp_invalid"

    # output_path must be within base_dir and exist.
    output_path = str(data.get("output_path", ""))
    ok_op, msg_op, real_op = validate_path(output_path, base_dir=base_dir)
    if not ok_op or real_op is None:
        return "unverified", f"output_path_not_in_base_dir:{msg_op}"
    if not os.path.exists(real_op):
        return "unverified", f"output_artefact_not_found:{output_path}"

    # output_hash must be sha256 and match recomputed hash of the artefact.
    output_hash = str(data.get("output_hash", ""))
    if not validate_sha256(output_hash):
        return "unverified", "output_hash_not_sha256"
    recomputed_output_hash = sha256_file(real_op)
    if recomputed_output_hash != output_hash:
        return "unverified", f"output_hash_mismatch:{recomputed_output_hash}"

    # receipt_hash must be sha256 and match recomputed hash of receipt data.
    receipt_hash = str(data.get("receipt_hash", ""))
    if not validate_sha256(receipt_hash):
        return "unverified", "receipt_hash_not_sha256"
    recomputed_receipt_hash = _compute_receipt_hash(data)
    if recomputed_receipt_hash != receipt_hash:
        return "unverified", f"receipt_hash_mismatch:{recomputed_receipt_hash}"

    # Trust binding: receipt binding must be verified by trust context.
    # Without trust context: untrusted_binding → FAIL (fail-closed).
    if trust_context is None:
        return "unverified", "untrusted_binding"
    if not trust_context.verify_receipt_binding(
        receipt_hash, requirement_id, str(data.get("check_id", ""))
    ):
        return "unverified", "untrusted_binding"

    return "verified", ""


def _owner_decision_receipt(
    check: dict, base_dir: Path, requirement_id: str, trust_context: TrustContext | None
) -> tuple[str, str, bool]:
    """Verify an owner-decision receipt (krav 5).

    Returns ``(observed, reason, is_blocked)``.

    Without trust context: ALWAYS returns ("blocked", "trust_context_missing", True).
    Candidate-local JSON alone NEVER counts as authorization.

    With trust context: the trust context decides whether to authorize.
    No trust_anchor_source allowlist validation (no real trust provider exists).
    """
    # Without trust context: always BLOCKED (fail-closed).
    if trust_context is None:
        return "blocked", "trust_context_missing", True

    # With trust context: parse receipt and ask trust context to verify.
    receipt_path = check.get("receipt_path", "")
    if isinstance(receipt_path, str) and _contains_shell_metacharacters(receipt_path):
        return "unauthorized", "shell_metacharacters_in:receipt_path", False

    data, err = _parse_receipt(receipt_path, base_dir=base_dir)
    if data is None:
        return "blocked", f"receipt_not_found:{err}", True

    owner_id = str(check.get("owner_id", "")).strip()
    if trust_context.verify_owner_decision(owner_id, data):
        return "authorized", "", False
    return "blocked", "owner_decision_not_authorized", True


# ── Per-requirement evaluation ─────────────────────────────────────────────


def _evaluate_single_check(
    rid: str, check: dict, base_dir: Path, trust_context: TrustContext | None
) -> CheckResult:
    """Evaluate one check dict (NOT all_of — caller handles all_of separately).

    The expected value is derived deterministically from the check type.
    owner_decision_receipt is the only type that can return BLOCKED.
    """
    ctype = check.get("type", "")
    expected = DETERMINISTIC_EXPECTED.get(ctype, "")

    if ctype not in ALLOWED_CHECK_TYPES:
        return CheckResult(
            rid, ctype, "fail", "unknown_check_type", expected,
            f"unknown_check_type:{ctype}",
        )

    offender = _check_args_for_metachars(check)
    if offender is not None:
        return CheckResult(
            rid, ctype, "fail", "shell_metacharacters_detected",
            expected, f"shell_metacharacters_in:{offender}",
        )

    if ctype == "git_ancestor":
        branch = check.get("branch", "")
        commit = check.get("commit_sha", "")
        if not validate_branch_name(branch):
            return CheckResult(
                rid, ctype, "fail", "invalid_branch", expected,
                f"branch_invalid:{branch}",
            )
        if not validate_commit_sha(commit):
            return CheckResult(
                rid, ctype, "fail", "invalid_commit_sha", expected,
                f"commit_sha_not_40char_hex:{commit}",
            )
        observed, reason = _git_ancestor(GIT_REPO, commit, branch, expect_ancestor=True)
        status = "pass" if observed == expected else "fail"
        return CheckResult(rid, ctype, status, observed, expected, reason)

    if ctype == "git_not_ancestor":
        branch = check.get("branch", "")
        commit = check.get("commit_sha", "")
        if not validate_branch_name(branch):
            return CheckResult(
                rid, ctype, "fail", "invalid_branch", expected,
                f"branch_invalid:{branch}",
            )
        if not validate_commit_sha(commit):
            return CheckResult(
                rid, ctype, "fail", "invalid_commit_sha", expected,
                f"commit_sha_not_40char_hex:{commit}",
            )
        observed, reason = _git_ancestor(GIT_REPO, commit, branch, expect_ancestor=False)
        status = "pass" if observed == expected else "fail"
        return CheckResult(rid, ctype, status, observed, expected, reason)

    if ctype == "file_sha256":
        path_str = check.get("path", "")
        exp = check.get("expected_sha256", "")
        # PENDING_EVIDENCE is a valid placeholder (schema allows it). It is
        # never a real sha256, so any existing file will mismatch and a missing
        # file gives missing_file — either way FAIL (krav 6, 7).
        if exp != "PENDING_EVIDENCE" and not validate_sha256(exp):
            return CheckResult(
                rid, ctype, "fail", "invalid_expected_sha256", expected,
                f"expected_sha256_not_64char_hex:{exp}",
            )
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
        observed, reason = _test_receipt(
            check, base_dir=base_dir, requirement_id=rid, trust_context=trust_context
        )
        status = "pass" if observed == expected else "fail"
        return CheckResult(rid, ctype, status, observed, expected, reason)

    if ctype == "owner_decision_receipt":
        observed, reason, is_blocked = _owner_decision_receipt(
            check, base_dir=base_dir, requirement_id=rid, trust_context=trust_context
        )
        if is_blocked:
            return CheckResult(rid, ctype, "blocked", observed, expected, reason)
        status = "pass" if observed == expected else "fail"
        return CheckResult(rid, ctype, status, observed, expected, reason)

    # Defensive — unreachable due to the allowlist check above.
    return CheckResult(rid, ctype, "fail", "unreachable", expected, "unreachable_branch")


def _evaluate_all_of(
    rid: str, check: dict, base_dir: Path, trust_context: TrustContext | None
) -> CheckResult:
    """Evaluate an all_of composite: every sub-check must pass (krav 6).

    Sub-checks are evaluated strictly. owner_decision_receipt inside all_of
    returns BLOCKED only for that sub-check. If ANY sub-check FAILs, the whole
    requirement is FAIL (FAIL > BLOCKED). If no sub-check FAILs but some BLOCK,
    the requirement is BLOCKED.

    v3 dual status (krav 6/10): when a single all_of has BOTH a FAIL sub-check
    AND a BLOCKED sub-check, the composite status is ``fail`` (FAIL drives the
    gate) AND ``also_blocked=True`` is set so the top-level gate adds the
    requirement ID to BOTH ``failing_ids`` and ``blocked_ids`` (deduplicated,
    stable order). failing_ids and blocked_ids may both contain the same ID.
    """
    expected = DETERMINISTIC_EXPECTED["all_of"]
    sub_checks = check.get("checks", [])
    if not isinstance(sub_checks, list) or not sub_checks:
        return CheckResult(
            rid, "all_of", "fail", "no_sub_checks", expected, "all_of_empty"
        )
    sub_results: list[CheckResult] = []
    any_fail = False
    any_blocked = False
    reasons: list[str] = []
    for sub in sub_checks:
        if not isinstance(sub, dict):
            sub_results.append(
                CheckResult(rid, "all_of", "fail", "sub_not_dict", expected, "sub_check_not_dict")
            )
            any_fail = True
            reasons.append(f"sub{len(sub_results)-1}:sub_check_not_dict")
            continue
        sub_rid = f"{rid}::sub{len(sub_results)}"
        if sub.get("type") == "all_of":
            sr = _evaluate_all_of(sub_rid, sub, base_dir=base_dir, trust_context=trust_context)
        else:
            sr = _evaluate_single_check(sub_rid, sub, base_dir=base_dir, trust_context=trust_context)
        sub_results.append(sr)
        if sr.status == "fail":
            any_fail = True
            reasons.append(f"{sub_rid}:{sr.reason}")
            # v4 recursive dual-status propagation: when an inner all_of
            # returns status=fail with also_blocked=True (meaning a BLOCKED
            # sub-check exists somewhere inside the composite), the outer
            # all_of must preserve that BLOCKED signal so the top-level gate
            # dual-lists the requirement ID. Without this, nesting an
            # all_of(with FAIL+BLOCKED) inside another all_of drops the
            # BLOCKED signal at the outer level.
            if getattr(sr, "also_blocked", False):
                any_blocked = True
        elif sr.status == "blocked":
            any_blocked = True
            reasons.append(f"{sub_rid}:{sr.reason}")
    if any_fail:
        # FAIL drives the gate. If a BLOCKED sub-check also exists, set
        # also_blocked so the requirement ID is dual-listed at the top level.
        return CheckResult(
            rid, "all_of", "fail", "not_all_pass", expected, "|".join(reasons),
            also_blocked=any_blocked,
        )
    if any_blocked:
        return CheckResult(
            rid, "all_of", "blocked", "sub_blocked", expected, "|".join(reasons)
        )
    return CheckResult(rid, "all_of", "pass", "all_pass", expected, "")


def evaluate_requirement(
    req: dict, base_dir: Path, trust_context: TrustContext | None = None
) -> CheckResult:
    rid = req.get("id", "<unknown>")
    check = req.get("check", {}) or {}
    ctype = check.get("type", "") if isinstance(check, dict) else ""

    if ctype == "all_of":
        return _evaluate_all_of(rid, check, base_dir=base_dir, trust_context=trust_context)
    return _evaluate_single_check(rid, check, base_dir=base_dir, trust_context=trust_context)


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
    trust_context: TrustContext | None = None,
    schema_path: str | None = None,
) -> GateResult:
    """Run the integrity gate and return a GateResult.

    ``base_dir`` is the worktree root for resolving relative paths (REQUIRED).
    ``trust_context`` is the injectable trust anchor for closeout. Without it,
    the CIC is fail-closed: closeout_permitted is never true.
    ``schema_path`` defaults to ``base_dir / control-plane/fas-a-requirements.schema.json``.
    """
    # base_dir is required (krav 8).
    if base_dir is None:
        return GateResult(
            gate="FAIL", manifest_id="", manifest_hash="",
            manifest_verified=False, schema_valid=False, id_set_valid=False,
            trust_context_present=False, reason="base_dir_required",
        )

    # Validate manifest path is within base_dir (krav 8).
    ok_m, msg_m, real_m = validate_path(manifest_path, base_dir=base_dir)
    if not ok_m or real_m is None:
        return GateResult(
            gate="FAIL", manifest_id="", manifest_hash="",
            manifest_verified=False, schema_valid=False, id_set_valid=False,
            trust_context_present=trust_context is not None,
            reason=f"manifest_not_in_base_dir:{msg_m}",
        )

    if not os.path.exists(real_m):
        return GateResult(
            gate="FAIL", manifest_id="", manifest_hash="",
            manifest_verified=False, schema_valid=False, id_set_valid=False,
            trust_context_present=trust_context is not None,
            reason=f"manifest_not_found:{manifest_path}",
        )

    # Read manifest bytes and compute sha256 of the raw bytes (krav 3).
    try:
        with open(real_m, "rb") as fh:
            raw_bytes = fh.read()
    except OSError as exc:
        return GateResult(
            gate="FAIL", manifest_id="", manifest_hash="",
            manifest_verified=False, schema_valid=False, id_set_valid=False,
            trust_context_present=trust_context is not None,
            reason=f"manifest_read_error:{exc}",
        )
    m_hash = sha256_bytes(raw_bytes)

    # Parse YAML.
    try:
        manifest = yaml.safe_load(raw_bytes.decode("utf-8", errors="replace"))
    except yaml.YAMLError as exc:
        return GateResult(
            gate="FAIL", manifest_id="", manifest_hash=m_hash,
            manifest_verified=False, schema_valid=False, id_set_valid=False,
            trust_context_present=trust_context is not None,
            reason=f"manifest_not_yaml:{type(exc).__name__}:{exc}",
        )
    if manifest is None:
        return GateResult(
            gate="FAIL", manifest_id="", manifest_hash=m_hash,
            manifest_verified=False, schema_valid=False, id_set_valid=False,
            trust_context_present=trust_context is not None,
            reason="manifest_empty",
        )
    if not isinstance(manifest, dict):
        return GateResult(
            gate="FAIL", manifest_id="", manifest_hash=m_hash,
            manifest_verified=False, schema_valid=False, id_set_valid=False,
            trust_context_present=trust_context is not None,
            reason="manifest_not_yaml_mapping",
        )

    manifest_id = manifest.get("manifest_id", "")

    # Schema validation BEFORE evaluation (krav 1).
    # v3 (schema-realpath): the CIC resolves the schema path via validate_path
    # and opens/validates EXACTLY the returned realpath. The raw relative
    # schema_path is NEVER opened after containment. There is no cwd-
    # dependent fallback: only the realpath under base_dir is used.
    if schema_path is None:
        schema_path = str(base_dir / "control-plane" / "fas-a-requirements.schema.json")
    # Validate schema path is within base_dir (krav 8) and capture the realpath.
    ok_sch, msg_sch, real_sch = validate_path(schema_path, base_dir=base_dir)
    if not ok_sch or real_sch is None:
        return GateResult(
            gate="FAIL", manifest_id=manifest_id, manifest_hash=m_hash,
            manifest_verified=False, schema_valid=False, id_set_valid=False,
            trust_context_present=trust_context is not None,
            reason=f"schema_not_in_base_dir:{msg_sch}",
        )
    # Existence check on the resolved realpath (not the raw schema_path).
    if not os.path.exists(real_sch):
        return GateResult(
            gate="FAIL", manifest_id=manifest_id, manifest_hash=m_hash,
            manifest_verified=False, schema_valid=False, id_set_valid=False,
            trust_context_present=trust_context is not None,
            reason=f"schema_not_found:{real_sch}",
        )
    # Validate the manifest against the schema at the resolved realpath.
    ok_s, err_s = validate_manifest_against_schema(manifest, real_sch)
    if not ok_s:
        return GateResult(
            gate="FAIL", manifest_id=manifest_id, manifest_hash=m_hash,
            manifest_verified=False, schema_valid=False, id_set_valid=False,
            trust_context_present=trust_context is not None,
            reason=f"schema_invalid:{err_s}",
        )

    # Canonical ID set — exactly FAS-A-001..013.
    ok_ids, err_ids, _off = validate_canonical_id_set(manifest.get("requirements", []))
    if not ok_ids:
        return GateResult(
            gate="FAIL", manifest_id=manifest_id, manifest_hash=m_hash,
            manifest_verified=False, schema_valid=True, id_set_valid=False,
            trust_context_present=trust_context is not None,
            reason=f"id_set_invalid:{err_ids}",
        )

    # Manifest binding (krav 3): trust context verifies manifest digest.
    # Without trust context: can compute digest but can't verify → manifest_verified=False.
    if trust_context is not None:
        manifest_verified = trust_context.verify_manifest_digest(m_hash)
        if not manifest_verified:
            # Manifest tampered: digest doesn't match approved digest → FAIL.
            return GateResult(
                gate="FAIL", manifest_id=manifest_id, manifest_hash=m_hash,
                manifest_verified=False, schema_valid=True, id_set_valid=True,
                trust_context_present=True,
                reason="manifest_tampered",
            )
    else:
        manifest_verified = False

    # Source-hash drift → FAIL (text was edited after pinning).
    source_mismatches = verify_source_hashes(manifest)
    if source_mismatches:
        return GateResult(
            gate="FAIL", manifest_id=manifest_id, manifest_hash=m_hash,
            manifest_verified=manifest_verified, schema_valid=True, id_set_valid=True,
            trust_context_present=trust_context is not None,
            reason=f"source_hash_mismatch:{','.join(source_mismatches)}",
            failing_ids=source_mismatches,
        )

    # Evaluate every requirement.
    results: list[CheckResult] = []
    failing_ids: list[str] = []
    blocked_ids: list[str] = []

    for req in manifest.get("requirements", []):
        result = evaluate_requirement(req, base_dir=base_dir, trust_context=trust_context)
        results.append(result)
        if result.status == "fail":
            # v3 dual status: an all_of composite with BOTH a FAIL and a
            # BLOCKED sub-check sets also_blocked. The ID is added to BOTH
            # failing_ids and blocked_ids (deduplicated, stable order) so the
            # dual-status signal survives. FAIL still drives the gate.
            failing_ids.append(result.requirement_id)
            if getattr(result, "also_blocked", False):
                blocked_ids.append(result.requirement_id)
        elif result.status == "blocked":
            blocked_ids.append(result.requirement_id)

    # Deduplicate while preserving stable first-seen order (v3 dual status).
    def _dedup(seq: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for x in seq:
            if x not in seen:
                seen.add(x)
                out.append(x)
        return out

    failing_ids = _dedup(failing_ids)
    blocked_ids = _dedup(blocked_ids)

    # Gate logic (krav 10): FAIL > BLOCKED > PASS.
    # closeout_permitted=true only if gate=PASS (which requires trust context).
    if failing_ids:
        gate = "FAIL"
        reason = f"failed_requirements:{','.join(failing_ids)}"
        closeout_permitted = False
    elif blocked_ids:
        gate = "BLOCKED"
        reason = f"blocked_requirements:{','.join(blocked_ids)}"
        closeout_permitted = False
    else:
        # No fails, no blocks. PASS only if trust context verified the manifest.
        if trust_context is not None and manifest_verified:
            gate = "PASS"
            reason = "all_requirements_pass"
            closeout_permitted = True
        else:
            # Without trust context: fail-closed (even if all technical reqs pass).
            gate = "FAIL"
            reason = "trust_context_missing"
            closeout_permitted = False

    return GateResult(
        gate=gate,
        manifest_id=manifest_id,
        manifest_hash=m_hash,
        manifest_verified=manifest_verified,
        schema_valid=True,
        id_set_valid=True,
        trust_context_present=trust_context is not None,
        results=results,
        failing_ids=failing_ids,
        blocked_ids=blocked_ids,
        closeout_permitted=closeout_permitted,
        reason=reason,
    )


# ── CLI ────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fas A completion-integrity-gate (read-only, deterministic, fail-closed)."
    )
    parser.add_argument(
        "--manifest", required=True,
        help="Path to fas-a-requirements.yaml (must be within --base-dir)."
    )
    parser.add_argument(
        "--base-dir", default=".",
        help="Worktree root for resolving relative check paths. All paths "
             "(manifest, schema, receipts, output artefacts) must be within "
             "realpath(base_dir)."
    )
    parser.add_argument(
        "--trust-context-source", default="none",
        help="Trust context source (default 'none' = fail-closed). No verified "
             "trust provider is implemented in this candidate; any value other "
             "than 'none' is still fail-closed. Future harness integration will "
             "add verified sources."
    )
    parser.add_argument(
        "--expected-manifest-hash", default=None,
        help="DEPRECATED: read-only informational hash. Does NOT grant PASS. "
             "Trust anchor provisioning is exclusively via trust context "
             "(not yet implemented in production CLI)."
    )
    parser.add_argument(
        "--schema-path", default=None,
        help="Override path to the JSON Schema (must be within --base-dir; "
             "defaults to <base-dir>/control-plane/fas-a-requirements.schema.json)."
    )
    parser.add_argument(
        "--json", action="store_true", default=True,
        help="Emit JSON to stdout (default)."
    )
    args = parser.parse_args(argv)

    base_dir = Path(args.base_dir).resolve(strict=False) if args.base_dir else None

    # Production CLI has NO trust context provider (krav 2).
    # The --trust-context-source flag is accepted but always results in no
    # trust context, because no verified trust provider exists yet. This is
    # fail-closed by design. The --expected-manifest-hash flag is read-only
    # informational and NEVER grants PASS.
    trust_context = None

    result = run_gate(
        manifest_path=args.manifest,
        base_dir=base_dir,
        trust_context=trust_context,
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