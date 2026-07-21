#!/usr/bin/env python3
"""Reusable Completion Integrity Gate (CIG) — fail-closed, goal-ID-bound.

Generalized from the CIC candidate (commit 310284f7f on kmros/cic) to work
with ANY requirements manifest.  The gate is NOT hardcoded to a specific
goal or requirement-ID set; it reads canonical IDs, source hashes, check
types, and all_of structure from the manifest itself.

Design invariants (from P0-060 completion contract):

  1. EN AUKTORITET: The requirements manifest is the single semantic authority.
     No hardcoded REQUIREMENT_SEMANTICS table.  The CIG reads requirement texts,
     source_hash, check type, and all_of structure from the manifest.
  2. TRUST-GRÄNS: CLI/default mode has no trusted trust context and therefore
     NEVER sets closeout_permitted=True.  An injectable TrustContext protocol
     allows future harness integration.  Without verified trust context:
     fail-closed, closeout_permitted=False.
  3. MANIFESTBINDNING: A verified trust context binds the exact manifest
     bytes/digest.  Any change to ID, text, source_hash, check type, all_of
     structure, or check arguments changes the digest and gives
     manifest_tampered.  Without trust context: fail-closed, no PASS.
  4. STRICT TEST RECEIPTS: Exact strict schema with required fields:
     requirement_id, check_id, branch, commit, result (exactly "pass"),
     timestamp (valid ISO-8601 UTC, NO freshness window), output_path (required,
     must exist within base_dir), output_hash (sha256, recomputed from
     artefact), receipt_hash (sha256, recomputed from receipt bytes).
  5. OWNER RECEIPTS: Without trust context, owner decision receipts ALWAYS give
     BLOCKED in production/CLI mode, never authorized from self-declared JSON.
  6. DUAL STATUS: all_of of technical current-truth evidence (file_sha256) and
     separate owner decision (owner_decision_receipt).  Missing technical =
     FAIL; missing owner trust = BLOCKED; FAIL has priority; both ID lists
     reported.
  7. INGA PÅHITTADE IMPLEMENTATION PATHS: Neutral evidence-receipt-paths under
     control-plane/evidence/ in the manifest.  No hardcoded paths.
  8. PATH CONTAINMENT: Manifest, schema, receipts, and output artefacts bound
     to exact realpath(base_dir).
  9. GIT REF: Branch/ref validated with git check-ref-format semantics,
     shell=False, list-args.
  10. STATUS: Technical errors → FAIL.  Genuine missing owner/harness trust →
      BLOCKED.  FAIL has priority over BLOCKED and both lists are always
      preserved.  Without trusted trust, closeout_permitted is never true.

Output: a single JSON object on stdout.  Exit 0 = PASS, 1 = FAIL, 2 = BLOCKED.
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

# ── Constants (never read from YAML) ────────────────────────────────────────

# Git repo for git_ancestor / git_not_ancestor checks.  Can be overridden via
# the --git-repo CLI flag or the run_gate(git_repo=...) parameter.
GIT_REPO = os.environ.get("CIG_GIT_REPO", "")

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

# Deterministic expected outcome per check type.  The CIG compares the
# computed actual against this — never against a YAML expected field.
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


# ── TrustContext protocol (invariant 2) ──────────────────────────────────────


@runtime_checkable
class TrustContext(Protocol):
    """Injectable trust context for the CIG.

    Production CLI has no trust context (None default).  Without trust context,
    the CIG is fail-closed: closeout_permitted is never True.
    """

    def verify_manifest_digest(self, digest: str) -> bool:
        ...

    def verify_receipt_binding(
        self, receipt_hash: str, requirement_id: str, check_id: str
    ) -> bool:
        ...

    def verify_owner_decision(self, owner_id: str, receipt: dict) -> bool:
        ...


# ── Data model ──────────────────────────────────────────────────────────────


@dataclass
class CheckResult:
    """Result of evaluating a single requirement (or all_of sub-check).

    also_blocked is set ONLY for an all_of composite whose gate-driving
    status is fail but which also contains at least one blocked sub-check.
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
    goal_id: str = ""
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
            "goal_id": self.goal_id,
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
    """Validate path_str is within base_dir using realpath (invariant 8)."""
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
    """Validate branch/ref name using git check-ref-format (invariant 9)."""
    if not isinstance(name, str) or not name:
        return False
    if ".." in name:
        return False
    if "\x00" in name or "\n" in name:
        return False
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
        return bool(re.match(r"^[A-Za-z0-9._][A-Za-z0-9._/-]{0,199}$", name))


def validate_iso_timestamp(ts: str) -> bool:
    """Validate that ts is a valid ISO-8601 UTC timestamp (invariant 4)."""
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
        return False
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
    """Compute the receipt hash from receipt data, excluding receipt_hash."""
    data_without_hash = {k: v for k, v in data.items() if k != "receipt_hash"}
    canonical = json.dumps(data_without_hash, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ── Schema validation (invariant 1) ─────────────────────────────────────────


def validate_manifest_against_schema(
    manifest: dict, schema_path: str
) -> tuple[bool, str]:
    """Validate the manifest dict against the JSON Schema at schema_path."""
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
    except Exception as exc:  # noqa: BLE001
        return False, f"schema_check_error:{type(exc).__name__}:{exc}"


# ── Canonical ID set check (generalized — not hardcoded) ────────────────────


def validate_canonical_id_set(requirements: list) -> tuple[bool, str, list[str]]:
    """Validate that all requirement IDs are present, unique, and non-empty.

    Unlike the CIC candidate which hardcoded the canonical ID set, this
    generalized version validates that all declared requirements have unique
    non-empty IDs.

    Returns (ok, reason, offending_ids).
    """
    if not isinstance(requirements, list):
        return False, "requirements_not_list", []
    ids = []
    for r in requirements:
        if not isinstance(r, dict) or "id" not in r:
            return False, "requirement_missing_id", []
        rid = r["id"]
        if not isinstance(rid, str) or not rid:
            return False, "requirement_id_empty", []
        ids.append(rid)
    seen: set[str] = set()
    dupes: list[str] = []
    for i in ids:
        if i in seen and i not in dupes:
            dupes.append(i)
        seen.add(i)
    if dupes:
        return False, f"id_set_violation:duplicate:{','.join(dupes)}", dupes
    return True, "", []


# ── Goal-ID binding check ──────────────────────────────────────────────────


def validate_goal_binding(manifest: dict) -> tuple[bool, str, str]:
    """Validate that the manifest is version-bound to a goal_id and source_message.

    Returns (ok, reason, goal_id).

    Contract rule 1: "Ledger is version-bound to goal-ID and source message."
    """
    if not isinstance(manifest, dict):
        return False, "manifest_not_dict", ""
    goal_id = manifest.get("goal_id", "")
    source_message = manifest.get("source_message", "")
    if not isinstance(goal_id, str) or not goal_id:
        return False, "goal_id_missing", ""
    if not isinstance(source_message, str) or not source_message:
        return False, "source_message_missing", ""
    return True, "", goal_id


# ── Card-reference validation ──────────────────────────────────────────────


def validate_card_references(manifest: dict) -> tuple[bool, str]:
    """Validate that cards reference ledger requirements, not vice versa.

    Contract rule 2: "Cards reference ledger requirements, not the other way
    around."
    """
    if not isinstance(manifest, dict):
        return True, ""
    requirements = manifest.get("requirements", [])
    if not isinstance(requirements, list):
        return True, ""
    req_ids = set()
    for r in requirements:
        if isinstance(r, dict):
            rid = r.get("id", "")
            if rid:
                req_ids.add(rid)
            if "card_id" in r:
                return False, f"requirement {rid} carries card_id — cards must reference requirements, not vice versa"
    cards = manifest.get("cards", [])
    if not isinstance(cards, list):
        return True, ""
    for card in cards:
        if not isinstance(card, dict):
            return False, "card_not_dict"
        card_rid = card.get("requirement_id", "")
        if not card_rid:
            return False, "card_missing_requirement_id"
        if card_rid not in req_ids:
            return False, f"card references unknown requirement: {card_rid}"
    return True, ""


# ── Check evaluators (shell=False, base_dir containment) ────────────────────


def _git_ancestor(repo: str, commit: str, branch: str, expect_ancestor: bool) -> tuple[str, str]:
    """Run git merge-base --is-ancestor with shell=False."""
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
    """Verify a test receipt with strict schema and trust binding (invariant 4)."""
    receipt_path = check.get("receipt_path", "")
    expected_check_id = check.get("expected_check_id", "")

    for key in ("receipt_path", "expected_check_id"):
        v = check.get(key, "")
        if isinstance(v, str) and _contains_shell_metacharacters(v):
            return "unverified", f"shell_metacharacters_in:{key}"

    if not expected_check_id:
        return "unverified", "expected_check_id_empty"

    data, err = _parse_receipt(receipt_path, base_dir=base_dir)
    if data is None:
        return "unverified", err

    extra = set(data.keys()) - REQUIRED_RECEIPT_FIELDS
    if extra:
        return "unverified", f"receipt_extra_fields:{','.join(sorted(extra))}"
    missing = REQUIRED_RECEIPT_FIELDS - set(data.keys())
    if missing:
        return "unverified", f"receipt_missing_fields:{','.join(sorted(missing))}"

    if str(data.get("requirement_id", "")) != requirement_id:
        return "unverified", f"requirement_id_mismatch:{data.get('requirement_id')}"

    if str(data.get("check_id", "")) != expected_check_id:
        return "unverified", f"check_id_mismatch:{data.get('check_id')}"

    branch = str(data.get("branch", ""))
    if not validate_branch_name(branch):
        return "unverified", f"branch_invalid:{branch}"

    commit = str(data.get("commit", ""))
    if not validate_commit_sha(commit):
        return "unverified", "commit_not_40char_hex"

    if str(data.get("result", "")) != "pass":
        return "unverified", f"result_not_pass:{data.get('result')}"

    timestamp = str(data.get("timestamp", ""))
    if not validate_iso_timestamp(timestamp):
        return "unverified", "timestamp_invalid"

    output_path = str(data.get("output_path", ""))
    ok_op, msg_op, real_op = validate_path(output_path, base_dir=base_dir)
    if not ok_op or real_op is None:
        return "unverified", f"output_path_not_in_base_dir:{msg_op}"
    if not os.path.exists(real_op):
        return "unverified", f"output_artefact_not_found:{output_path}"

    output_hash = str(data.get("output_hash", ""))
    if not validate_sha256(output_hash):
        return "unverified", "output_hash_not_sha256"
    recomputed_output_hash = sha256_file(real_op)
    if recomputed_output_hash != output_hash:
        return "unverified", f"output_hash_mismatch:{recomputed_output_hash}"

    receipt_hash = str(data.get("receipt_hash", ""))
    if not validate_sha256(receipt_hash):
        return "unverified", "receipt_hash_not_sha256"
    recomputed_receipt_hash = _compute_receipt_hash(data)
    if recomputed_receipt_hash != receipt_hash:
        return "unverified", f"receipt_hash_mismatch:{recomputed_receipt_hash}"

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
    """Verify an owner-decision receipt (invariant 5).

    Returns (observed, reason, is_blocked).

    Without trust context: ALWAYS returns ("blocked", "trust_context_missing",
    True).  Self-declared JSON alone NEVER counts as authorization.
    """
    if trust_context is None:
        return "blocked", "trust_context_missing", True

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
    rid: str, check: dict, base_dir: Path, trust_context: TrustContext | None,
    git_repo: str = "",
) -> CheckResult:
    """Evaluate one check dict (NOT all_of — caller handles all_of separately)."""
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
        observed, reason = _git_ancestor(git_repo, commit, branch, expect_ancestor=True)
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
        observed, reason = _git_ancestor(git_repo, commit, branch, expect_ancestor=False)
        status = "pass" if observed == expected else "fail"
        return CheckResult(rid, ctype, status, observed, expected, reason)

    if ctype == "file_sha256":
        path_str = check.get("path", "")
        exp = check.get("expected_sha256", "")
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

    return CheckResult(rid, ctype, "fail", "unreachable", expected, "unreachable_branch")


def _evaluate_all_of(
    rid: str, check: dict, base_dir: Path, trust_context: TrustContext | None,
    git_repo: str = "",
) -> CheckResult:
    """Evaluate an all_of composite: every sub-check must pass (invariant 6).

    v4 recursive dual-status propagation: when an inner all_of returns
    status=fail with also_blocked=True, the outer all_of preserves that
    BLOCKED signal so the top-level gate dual-lists the requirement ID.
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
            sr = _evaluate_all_of(sub_rid, sub, base_dir=base_dir,
                                   trust_context=trust_context, git_repo=git_repo)
        else:
            sr = _evaluate_single_check(sub_rid, sub, base_dir=base_dir,
                                         trust_context=trust_context, git_repo=git_repo)
        sub_results.append(sr)
        if sr.status == "fail":
            any_fail = True
            reasons.append(f"{sub_rid}:{sr.reason}")
            if getattr(sr, "also_blocked", False):
                any_blocked = True
        elif sr.status == "blocked":
            any_blocked = True
            reasons.append(f"{sub_rid}:{sr.reason}")
    if any_fail:
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
    req: dict, base_dir: Path, trust_context: TrustContext | None = None,
    git_repo: str = "",
) -> CheckResult:
    rid = req.get("id", "<unknown>")
    check = req.get("check", {}) or {}
    ctype = check.get("type", "") if isinstance(check, dict) else ""

    if ctype == "all_of":
        return _evaluate_all_of(rid, check, base_dir=base_dir,
                                 trust_context=trust_context, git_repo=git_repo)
    return _evaluate_single_check(rid, check, base_dir=base_dir,
                                   trust_context=trust_context, git_repo=git_repo)


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
    git_repo: str = "",
) -> GateResult:
    """Run the integrity gate and return a GateResult.

    base_dir is the worktree root for resolving relative paths (REQUIRED).
    trust_context is the injectable trust anchor for closeout.  Without it,
    the CIG is fail-closed: closeout_permitted is never True.
    schema_path defaults to base_dir / control-plane/requirements.schema.json.
    git_repo is the git repository path for git_ancestor / git_not_ancestor.
    """
    if base_dir is None:
        return GateResult(
            gate="FAIL", manifest_id="", manifest_hash="",
            manifest_verified=False, schema_valid=False, id_set_valid=False,
            trust_context_present=False, reason="base_dir_required",
        )

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

    # Schema validation BEFORE evaluation (invariant 1).
    if schema_path is None:
        schema_path = str(base_dir / "control-plane" / "requirements.schema.json")
    ok_sch, msg_sch, real_sch = validate_path(schema_path, base_dir=base_dir)
    if not ok_sch or real_sch is None:
        return GateResult(
            gate="FAIL", manifest_id=manifest_id, manifest_hash=m_hash,
            manifest_verified=False, schema_valid=False, id_set_valid=False,
            trust_context_present=trust_context is not None,
            reason=f"schema_not_in_base_dir:{msg_sch}",
        )
    if not os.path.exists(real_sch):
        return GateResult(
            gate="FAIL", manifest_id=manifest_id, manifest_hash=m_hash,
            manifest_verified=False, schema_valid=False, id_set_valid=False,
            trust_context_present=trust_context is not None,
            reason=f"schema_not_found:{real_sch}",
        )
    ok_s, err_s = validate_manifest_against_schema(manifest, real_sch)
    if not ok_s:
        return GateResult(
            gate="FAIL", manifest_id=manifest_id, manifest_hash=m_hash,
            manifest_verified=False, schema_valid=False, id_set_valid=False,
            trust_context_present=trust_context is not None,
            reason=f"schema_invalid:{err_s}",
        )

    # Goal-ID binding (contract rule 1).
    ok_goal, err_goal, goal_id = validate_goal_binding(manifest)
    if not ok_goal:
        return GateResult(
            gate="FAIL", manifest_id=manifest_id, manifest_hash=m_hash,
            manifest_verified=False, schema_valid=True, id_set_valid=False,
            trust_context_present=trust_context is not None,
            goal_id=goal_id,
            reason=f"goal_binding_invalid:{err_goal}",
        )

    # Card-reference validation (contract rule 2).
    ok_cards, err_cards = validate_card_references(manifest)
    if not ok_cards:
        return GateResult(
            gate="FAIL", manifest_id=manifest_id, manifest_hash=m_hash,
            manifest_verified=False, schema_valid=True, id_set_valid=False,
            trust_context_present=trust_context is not None,
            goal_id=goal_id,
            reason=f"card_reference_invalid:{err_cards}",
        )

    # Canonical ID set — unique, non-empty.
    ok_ids, err_ids, _off = validate_canonical_id_set(manifest.get("requirements", []))
    if not ok_ids:
        return GateResult(
            gate="FAIL", manifest_id=manifest_id, manifest_hash=m_hash,
            manifest_verified=False, schema_valid=True, id_set_valid=False,
            trust_context_present=trust_context is not None,
            goal_id=goal_id,
            reason=f"id_set_invalid:{err_ids}",
        )

    # Manifest binding (invariant 3): trust context verifies manifest digest.
    if trust_context is not None:
        manifest_verified = trust_context.verify_manifest_digest(m_hash)
        if not manifest_verified:
            return GateResult(
                gate="FAIL", manifest_id=manifest_id, manifest_hash=m_hash,
                manifest_verified=False, schema_valid=True, id_set_valid=True,
                trust_context_present=True,
                goal_id=goal_id,
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
            goal_id=goal_id,
            reason=f"source_hash_mismatch:{','.join(source_mismatches)}",
            failing_ids=source_mismatches,
        )

    # Evaluate every requirement.
    results: list[CheckResult] = []
    failing_ids: list[str] = []
    blocked_ids: list[str] = []

    for req in manifest.get("requirements", []):
        result = evaluate_requirement(req, base_dir=base_dir,
                                       trust_context=trust_context,
                                       git_repo=git_repo)
        results.append(result)
        if result.status == "fail":
            failing_ids.append(result.requirement_id)
            if getattr(result, "also_blocked", False):
                blocked_ids.append(result.requirement_id)
        elif result.status == "blocked":
            blocked_ids.append(result.requirement_id)

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

    # Gate logic (invariant 10): FAIL > BLOCKED > PASS.
    if failing_ids:
        gate = "FAIL"
        reason = f"failed_requirements:{','.join(failing_ids)}"
        closeout_permitted = False
    elif blocked_ids:
        gate = "BLOCKED"
        reason = f"blocked_requirements:{','.join(blocked_ids)}"
        closeout_permitted = False
    else:
        if trust_context is not None and manifest_verified:
            gate = "PASS"
            reason = "all_requirements_pass"
            closeout_permitted = True
        else:
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
        goal_id=goal_id,
        results=results,
        failing_ids=failing_ids,
        blocked_ids=blocked_ids,
        closeout_permitted=closeout_permitted,
        reason=reason,
    )


# ── CLI ────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Completion Integrity Gate (read-only, deterministic, fail-closed)."
    )
    parser.add_argument(
        "--manifest", required=True,
        help="Path to requirements manifest YAML (must be within --base-dir)."
    )
    parser.add_argument(
        "--base-dir", default=".",
        help="Worktree root for resolving relative check paths."
    )
    parser.add_argument(
        "--trust-context-source", default="none",
        help="Trust context source (default 'none' = fail-closed)."
    )
    parser.add_argument(
        "--expected-manifest-hash", default=None,
        help="DEPRECATED: read-only informational hash. Does NOT grant PASS."
    )
    parser.add_argument(
        "--schema-path", default=None,
        help="Override path to the JSON Schema (must be within --base-dir)."
    )
    parser.add_argument(
        "--git-repo", default="",
        help="Git repository path for git_ancestor / git_not_ancestor checks."
    )
    parser.add_argument(
        "--json", action="store_true", default=True,
        help="Emit JSON to stdout (default)."
    )
    args = parser.parse_args(argv)

    base_dir = Path(args.base_dir).resolve(strict=False) if args.base_dir else None

    # Production CLI has NO trust context provider (invariant 2).
    trust_context = None

    result = run_gate(
        manifest_path=args.manifest,
        base_dir=base_dir,
        trust_context=trust_context,
        schema_path=args.schema_path,
        git_repo=args.git_repo,
    )

    print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    if result.gate == "PASS":
        return 0
    if result.gate == "BLOCKED":
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
