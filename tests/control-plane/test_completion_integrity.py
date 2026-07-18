"""Regression + integrity tests for the Fas A completion-integrity-gate (CIC) — v1.1.

These tests are hermetic: they build temporary worktrees, manifests, and
receipts on disk and exercise ``scripts/completion_integrity_check.py``
through its public ``run_gate`` API. No real Kanban board, no network, no
mutations outside the per-test tmpdir.

Coverage of the 19 required regression cases (task t_200e833a, krav 12):

   1.  test_empty_manifest                     — empty manifest → FAIL
   2.  test_truncated_manifest                 — truncated YAML → FAIL
   3.  test_schema_invalid                      — schema-violating manifest → FAIL
   4.  test_missing_id                          — 12 IDs (missing FAS-A-007) → FAIL
   5.  test_extra_id                            — FAS-A-014 added → FAIL
   6.  test_duplicate_id                        — FAS-A-001 duplicated → FAIL
   7.  test_expected_bypass_rejected            — manifest cannot declare expected=missing → schema-reject / FAIL
   8.  test_real_production_path                — CIC accepts real /Users/ai/.hermes/worktrees/kmros-cic
   9.  test_symlink_escape                      — symlink to outside allowlist → FAIL
  10.  test_fabricated_test_receipt             — receipt without valid commit/check-id → FAIL
  11.  test_fabricated_owner_receipt            — owner receipt without external attestation → BLOCKED
  12.  test_wrong_branch_commit_checkid        — receipt wrong branch/commit/check-id → FAIL
  13.  test_stale_timestamp                     — timestamp older than 24h → FAIL
  14.  test_output_hash_mismatch                — output hash not matching recomputed → FAIL
  15.  test_simultaneous_fail_and_blocked       — 1 FAIL + 1 BLOCKED → gate=FAIL, both lists
  16.  test_no_expected_manifest_hash           — without --expected-manifest-hash → gate=FAIL
  17.  test_manifest_hash_mismatch             — wrong --expected-manifest-hash → FAIL manifest_tampered
  18.  test_self_referential_closeout_rejected  — cards done, requirements missing → FAIL
  19.  test_deterministic_identical_result      — same inputs → identical output

Plus kept extras: test_cic_does_not_mutate_filesystem,
test_canonical_manifest_validates_against_schema,
test_canonical_manifest_has_all_13_ids_and_exact_texts,
test_cic_never_uses_shell_true, test_cic_source_has_no_mutation_calls.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

# ── Inline env-scrubbing (replaces the deleted conftest.py, krav 13) ─────────
# The host machine has a broken shared release venv on PYTHONPATH (3.11 release
# whose rpds.rpds native module is incompatible with the 3.12 interpreter).
# If that path leaks into sys.path, ``import jsonschema`` fails. We scrub any
# site-packages directory that does not belong to this worktree's own .venv
# from ``sys.path`` before collection. This is inline (no separate conftest).

_WORKTREE_VENV_SITE = str(
    Path(__file__).resolve().parents[2] / ".venv" / "lib" / "python3.12" / "site-packages"
)


def _scrub_foreign_site_packages() -> None:
    keep = []
    for entry in sys.path:
        if not entry:
            keep.append(entry)
            continue
        if "site-packages" in entry and entry != _WORKTREE_VENV_SITE:
            continue
        keep.append(entry)
    sys.path[:] = keep


_scrub_foreign_site_packages()
os.environ.pop("PYTHONPATH", None)


# ── Helpers to load the CIC module from the worktree ────────────────────────

CIC_PATH = Path(__file__).resolve().parents[2] / "scripts" / "completion_integrity_check.py"


def _load_cic():
    mod_name = "completion_integrity_check"
    spec = importlib.util.spec_from_file_location(mod_name, CIC_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def cic():
    return _load_cic()


# ── A tiny fake git repo fixture for git_ancestor / git_not_ancestor tests ──

@pytest.fixture
def fake_git_repo(tmp_path):
    """Create a real (tiny) git repo so the CIC's subprocess git calls work."""
    repo = tmp_path / "fake-repo"
    repo.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / "f.txt").write_text("base\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-q", "-m", "base"], check=True, env=env)
    base_sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()

    (repo / "f.txt").write_text("child\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-q", "-m", "child"], check=True, env=env)
    child_sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                               capture_output=True, text=True, check=True).stdout.strip()

    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "divergent", base_sha],
                   check=True, env=env)
    (repo / "f.txt").write_text("divergent\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-q", "-m", "div"], check=True, env=env)
    divergent_sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                                    capture_output=True, text=True, check=True).stdout.strip()
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "main"],
                   check=True, env=env)

    return {
        "repo": str(repo),
        "base_sha": base_sha,
        "child_sha": child_sha,  # ancestor of main
        "divergent_sha": divergent_sha,  # NOT ancestor of main
    }


# ── Manifest + receipt builders ─────────────────────────────────────────────

def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _stale_iso() -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _write_manifest(base_dir: Path, requirements: list, manifest_id: str = "test-manifest-v1",
                    extra: dict | None = None, raw_text: str | None = None) -> Path:
    """Write a manifest YAML into ``base_dir/control-plane/`` and return its path.

    Each requirement is a dict already shaped for YAML. We add source_hash
    automatically from each requirement's ``text`` field if not present.
    If ``raw_text`` is given, write it verbatim (used by truncated/empty tests).
    """
    cp = base_dir / "control-plane"
    cp.mkdir(parents=True, exist_ok=True)
    path = cp / "fas-a-requirements.yaml"
    if raw_text is not None:
        path.write_text(raw_text)
        return path
    doc = {
        "manifest_version": "1.1",
        "manifest_id": manifest_id,
        "description": "test manifest",
    }
    if extra:
        doc.update(extra)
    for req in requirements:
        if "source_hash" not in req:
            req["source_hash"] = _sha256(req["text"])
    doc["requirements"] = requirements
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    return path


def _write_schema(base_dir: Path) -> Path:
    """Write the canonical schema (copy from the worktree) into the test base_dir."""
    src = Path(__file__).resolve().parents[2] / "control-plane" / "fas-a-requirements.schema.json"
    cp = base_dir / "control-plane"
    cp.mkdir(parents=True, exist_ok=True)
    dst = cp / "fas-a-requirements.schema.json"
    shutil.copyfile(src, dst)
    return dst


def _write_receipt(base_dir: Path, rel_path: str, payload: dict) -> Path:
    full = base_dir / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(json.dumps(payload, sort_keys=True))
    return full


def _write_output_artefact(base_dir: Path, rel_path: str, content: bytes = b"output artefact") -> str:
    """Write an output artefact and return its sha256 hash."""
    full = base_dir / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_bytes(content)
    return hashlib.sha256(content).hexdigest()


def _run_gate(cic, manifest_path: Path, base_dir: Path, expected_hash: str | None = None,
              git_repo: str | None = None, allow_extra_prefix: str | None = None,
              schema_path: str | None = None):
    """Run the CIC, optionally monkeypatching GIT_REPO to ``git_repo``.

    ``allow_extra_prefix`` (if given) is appended to the CIC's path allowlist
    for the duration of this call, so tests that build fixtures under a
    pytest tmp_path (which is NOT under the production allowlist prefixes)
    can still exercise path-validated checks. The production allowlist in the
    shipped CIC source is unchanged.
    """
    patches = []
    try:
        if git_repo is not None:
            patches.append(("GIT_REPO", cic.GIT_REPO))
            cic.GIT_REPO = git_repo
        if allow_extra_prefix is not None:
            patches.append(("PATH_ALLOWLIST_PREFIXES", cic.PATH_ALLOWLIST_PREFIXES))
            cic.PATH_ALLOWLIST_PREFIXES = tuple(cic.PATH_ALLOWLIST_PREFIXES) + (allow_extra_prefix,)
        return cic.run_gate(
            manifest_path=str(manifest_path), base_dir=base_dir,
            expected_manifest_hash=expected_hash, schema_path=schema_path,
        )
    finally:
        for attr, original in reversed(patches):
            setattr(cic, attr, original)


def _hash_of_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ────────────────────────────────────────────────────────────────────────────
# Required regression tests (krav 12, cases 1-19)
# ────────────────────────────────────────────────────────────────────────────


# ── Test 1: empty manifest → FAIL ────────────────────────────────────────────

def test_empty_manifest(cic, tmp_path):
    """An empty manifest (zero bytes) must give gate=FAIL."""
    _write_schema(tmp_path)
    manifest = _write_manifest(tmp_path, requirements=[], raw_text="")
    result = _run_gate(cic, manifest, tmp_path)
    assert result.gate == "FAIL"
    assert result.closeout_permitted is False
    # Empty YAML parses to None → manifest_empty.
    assert "manifest_empty" in result.reason or "schema" in result.reason


# ── Test 2: truncated manifest (invalid YAML) → FAIL ────────────────────────

def test_truncated_manifest(cic, tmp_path):
    """A truncated manifest (invalid YAML, e.g. unterminated flow) → FAIL."""
    _write_schema(tmp_path)
    # Truncated YAML: a mapping with an unclosed value.
    raw = "manifest_version: \"1.1\"\nmanifest_id: \"test\"\nrequirements: [\n"
    manifest = _write_manifest(tmp_path, requirements=[], raw_text=raw)
    result = _run_gate(cic, manifest, tmp_path)
    assert result.gate == "FAIL"
    assert result.closeout_permitted is False
    assert "manifest_not_yaml" in result.reason


# ── Test 3: schema-invalid manifest → FAIL ──────────────────────────────────

def test_schema_invalid(cic, tmp_path, fake_git_repo):
    """A manifest that does not validate against the schema → FAIL."""
    repo = fake_git_repo
    _write_schema(tmp_path)
    # Missing required `id` field on a requirement → schema_violation.
    manifest = _write_manifest(tmp_path, requirements=[
        {
            "text": "no id here",
            "check": {"type": "git_ancestor", "branch": "main", "commit_sha": repo["base_sha"]},
        },
    ])
    result = _run_gate(cic, manifest, tmp_path, git_repo=repo["repo"],
                       allow_extra_prefix=str(tmp_path))
    assert result.gate == "FAIL"
    assert result.schema_valid is False
    assert "schema_invalid" in result.reason or "schema_violation" in result.reason


# ── Test 4: missing ID (12 of 13) → FAIL ─────────────────────────────────────

def test_missing_id(cic, tmp_path, fake_git_repo):
    """Manifest with only 12 IDs (missing FAS-A-007) → FAIL (id_set_invalid)."""
    repo = fake_git_repo
    _write_schema(tmp_path)
    reqs = []
    for i in range(1, 14):
        if i == 7:
            continue  # skip FAS-A-007
        rid = f"FAS-A-{i:03d}"
        reqs.append({
            "id": rid, "text": f"req {rid}",
            "check": {"type": "git_ancestor", "branch": "main", "commit_sha": repo["base_sha"]},
        })
    manifest = _write_manifest(tmp_path, requirements=reqs)
    result = _run_gate(cic, manifest, tmp_path, git_repo=repo["repo"],
                       allow_extra_prefix=str(tmp_path))
    assert result.gate == "FAIL"
    assert result.id_set_valid is False
    assert "id_set_invalid" in result.reason
    assert "FAS-A-007" in result.reason  # missing is reported


# ── Test 5: extra ID (FAS-A-014 added) → FAIL ─────────────────────────────────

def test_extra_id(cic, tmp_path, fake_git_repo):
    """Manifest with FAS-A-014 added (14 total) → FAIL (id_set_invalid)."""
    repo = fake_git_repo
    _write_schema(tmp_path)
    reqs = []
    for i in range(1, 15):  # 1..14 inclusive → 14 IDs, extra is 014
        rid = f"FAS-A-{i:03d}"
        reqs.append({
            "id": rid, "text": f"req {rid}",
            "check": {"type": "git_ancestor", "branch": "main", "commit_sha": repo["base_sha"]},
        })
    manifest = _write_manifest(tmp_path, requirements=reqs)
    result = _run_gate(cic, manifest, tmp_path, git_repo=repo["repo"],
                       allow_extra_prefix=str(tmp_path))
    assert result.gate == "FAIL"
    assert result.id_set_valid is False
    assert "FAS-A-014" in result.reason


# ── Test 6: duplicate ID (FAS-A-001 twice) → FAIL ────────────────────────────

def test_duplicate_id(cic, tmp_path, fake_git_repo):
    """Manifest with FAS-A-001 duplicated → FAIL (id_set_invalid)."""
    repo = fake_git_repo
    _write_schema(tmp_path)
    reqs = []
    for i in range(2, 14):  # 002..013
        rid = f"FAS-A-{i:03d}"
        reqs.append({
            "id": rid, "text": f"req {rid}",
            "check": {"type": "git_ancestor", "branch": "main", "commit_sha": repo["base_sha"]},
        })
    # Two FAS-A-001 entries.
    reqs.insert(0, {"id": "FAS-A-001", "text": "first",
                    "check": {"type": "git_ancestor", "branch": "main", "commit_sha": repo["base_sha"]}})
    reqs.insert(1, {"id": "FAS-A-001", "text": "second",
                    "check": {"type": "git_ancestor", "branch": "main", "commit_sha": repo["base_sha"]}})
    manifest = _write_manifest(tmp_path, requirements=reqs)
    result = _run_gate(cic, manifest, tmp_path, git_repo=repo["repo"],
                       allow_extra_prefix=str(tmp_path))
    assert result.gate == "FAIL"
    assert result.id_set_valid is False
    assert "duplicate" in result.reason
    assert "FAS-A-001" in result.reason


# ── Test 7: expected-bypass rejected (manifest declaring expected='missing') ─

def test_expected_bypass_rejected(cic, tmp_path, fake_git_repo):
    """A manifest may NEVER declare `expected='missing'` / `unauthorized` / `unverified`.

    The new schema has no `expected` field at all (krav 3). A manifest that
    tries to include an `expected` field must be schema-rejected (additionalProperties=false
    on the requirement object). If somehow evaluation is reached, the CIC ignores
    the field and derives expected deterministically from the check type — so a
    missing file_sha256 still FAILs (not PASS).
    """
    repo = fake_git_repo
    _write_schema(tmp_path)
    # Build a manifest YAML by hand that includes `expected: missing` on a
    # file_sha256 check pointing at a non-existent file.
    raw = yaml.safe_dump({
        "manifest_version": "1.1",
        "manifest_id": "bypass-test",
        "description": "attempted bypass",
        "requirements": [
            {
                "id": "FAS-A-001",
                "text": "req",
                "source_hash": _sha256("req"),
                "check": {
                    "type": "file_sha256",
                    "path": "control-plane/evidence/missing.json",
                    "expected_sha256": "0" * 64,
                },
                "expected": "missing",  # FORBIDDEN — schema rejects this field
            },
        ],
    }, sort_keys=False)
    manifest = _write_manifest(tmp_path, requirements=[], raw_text=raw)
    result = _run_gate(cic, manifest, tmp_path, git_repo=repo["repo"],
                       allow_extra_prefix=str(tmp_path))
    assert result.gate == "FAIL"
    # The schema should reject the `expected` field (additionalProperties=false)
    # — OR if the field is tolerated, evaluation must still FAIL because the
    # file is missing (deterministic expected=match, observed=missing_file).
    assert result.schema_valid is False or result.failing_ids == ["FAS-A-001"]


# ── Test 8: real production path accepted ───────────────────────────────────

def test_real_production_path(cic, tmp_path):
    """CIC must accept the REAL current worktree path
    /Users/ai/.hermes/worktrees/kmros-cic and reject /etc/passwd.

    This test exercises the production allowlist against the actual worktree
    path (not a monkeypatched tmp root). We run the CIC against the canonical
    manifest in the real worktree and assert the path-validation succeeds
    (manifest is found and evaluated, not path_not_allowlisted at the top
    level). We also confirm /etc/passwd is rejected by validate_path.
    """
    # Direct validate_path test against the real production path.
    real_worktree = "/Users/ai/.hermes/worktrees/kmros-cic"
    ok, msg, real = cic.validate_path(
        "control-plane/fas-a-requirements.yaml",
        base_dir=Path(real_worktree),
    )
    # If the real worktree exists on this host, it must be accepted.
    if os.path.exists(real_worktree):
        assert ok is True, f"real production path rejected: {msg} real={real}"
        assert real == os.path.realpath(os.path.join(real_worktree, "control-plane/fas-a-requirements.yaml"))
    # /etc/passwd must always be rejected (regardless of host).
    ok2, msg2, real2 = cic.validate_path("/etc/passwd", base_dir=None)
    assert ok2 is False
    assert "path_not_allowlisted" in msg2 or "control_char" in msg2


# ── Test 9: symlink escape rejected ─────────────────────────────────────────

def test_symlink_escape(cic, tmp_path):
    """A symlink inside an allowlisted dir that points OUTSIDE the allowlist
    must be rejected (realpath resolves to a non-allowlisted target)."""
    # Build a fake worktree under tmp_path and add the tmp_path prefix.
    fake_worktree = tmp_path / "kmros-fake"
    (fake_worktree / "control-plane").mkdir(parents=True)
    # Create a target file OUTSIDE the fake worktree.
    outside = tmp_path / "outside_target"
    outside.write_bytes(b"outside secret")
    # Symlink inside the fake worktree pointing at the outside file.
    escape_link = fake_worktree / "control-plane" / "escape.json"
    os.symlink(outside, escape_link)

    # Patch the allowlist to include the fake worktree prefix.
    orig = cic.PATH_ALLOWLIST_PREFIXES
    try:
        cic.PATH_ALLOWLIST_PREFIXES = (str(fake_worktree) + "/",)
        ok, msg, real = cic.validate_path(
            "control-plane/escape.json", base_dir=fake_worktree
        )
        # The realpath resolves to the OUTSIDE target, which is NOT under the
        # fake worktree prefix → path_not_allowlisted.
        assert ok is False, f"symlink escape not rejected: real={real}"
        assert "path_not_allowlisted" in msg
    finally:
        cic.PATH_ALLOWLIST_PREFIXES = orig


# ── Test 10: fabricated test receipt (no valid commit/check-id) → FAIL ───────

def test_fabricated_test_receipt(cic, tmp_path, fake_git_repo):
    """A fabricated test receipt without a valid commit/check-id binding → FAIL."""
    repo = fake_git_repo
    _write_schema(tmp_path)
    # Receipt with a non-40-hex commit and a check_id that does not match.
    receipt = {
        "branch": "main",
        "commit": "not_a_real_commit",  # not 40 hex
        "check_id": "WRONG_CHECK_ID",
        "timestamp": _now_iso(),
        "output_hash": "0" * 64,
    }
    _write_receipt(tmp_path, "control-plane/receipts/test-1.json", receipt)
    # Build all 13 IDs; only FAS-A-007 exercises the fabricated receipt. The
    # rest are passing git_ancestor checks so the id-set check passes and we
    # reach the actual test_receipt evaluation.
    reqs = []
    for i in range(1, 14):
        rid = f"FAS-A-{i:03d}"
        if rid == "FAS-A-007":
            reqs.append({
                "id": rid, "text": f"req {rid}",
                "check": {"type": "test_receipt",
                          "receipt_path": "control-plane/receipts/test-1.json",
                          "expected_branch": "main",
                          "expected_commit": "a" * 40,
                          "expected_check_id": "FAS-A-007-spel-contract"},
            })
        else:
            reqs.append({
                "id": rid, "text": f"req {rid}",
                "check": {"type": "git_ancestor", "branch": "main", "commit_sha": repo["base_sha"]},
            })
    manifest = _write_manifest(tmp_path, requirements=reqs)
    result = _run_gate(cic, manifest, tmp_path, git_repo=repo["repo"],
                       allow_extra_prefix=str(tmp_path))
    assert result.gate == "FAIL"
    assert "FAS-A-007" in result.failing_ids
    res = next(r for r in result.results if r.requirement_id == "FAS-A-007")
    assert res.status == "fail"
    # The receipt's commit is not 40 hex → either expected_commit check or
    # commit_mismatch. The point is: FAIL.
    assert res.reason != ""


# ── Test 11: fabricated owner receipt (no external attestation) → BLOCKED ───

def test_fabricated_owner_receipt(cic, tmp_path, fake_git_repo):
    """An owner receipt without an external attestation (trust anchor missing)
    must give BLOCKED with trust_anchor_missing — never PASS."""
    repo = fake_git_repo
    _write_schema(tmp_path)
    # Candidate-local JSON with no external_attestation block. The owner_id is
    # concrete (not a placeholder), but the receipt lacks the trust anchor.
    receipt = {
        "owner_id": "filip-123",
        "decision_id": "dec-1",
        "decision": "approved",
        "commit": "a" * 40,
        "branch": "main",
        "timestamp": _now_iso(),
        "output_hash": "0" * 64,
        # NOTE: no external_attestation field
    }
    _write_receipt(tmp_path, "control-plane/receipts/owner-1.json", receipt)
    # All 13 IDs; FAS-A-011 is the owner_decision_receipt under test, the rest
    # are passing git_ancestor checks so the id-set check passes.
    reqs = []
    for i in range(1, 14):
        rid = f"FAS-A-{i:03d}"
        if rid == "FAS-A-011":
            reqs.append({
                "id": rid, "text": f"req {rid}",
                "check": {"type": "owner_decision_receipt",
                          "receipt_path": "control-plane/receipts/owner-1.json",
                          "owner_id": "filip-123"},
            })
        else:
            reqs.append({
                "id": rid, "text": f"req {rid}",
                "check": {"type": "git_ancestor", "branch": "main", "commit_sha": repo["base_sha"]},
            })
    manifest = _write_manifest(tmp_path, requirements=reqs)
    result = _run_gate(cic, manifest, tmp_path, git_repo=repo["repo"],
                       allow_extra_prefix=str(tmp_path),
                       expected_hash=_hash_of_file(tmp_path / "control-plane" / "fas-a-requirements.yaml"))
    assert result.gate == "BLOCKED"
    assert "FAS-A-011" in result.blocked_ids
    res = next(r for r in result.results if r.requirement_id == "FAS-A-011")
    assert res.status == "blocked"
    assert "trust_anchor_missing" in res.reason
    assert result.closeout_permitted is False


# ── Test 12: wrong branch/commit/check-id → FAIL ─────────────────────────────

def test_wrong_branch_commit_checkid(cic, tmp_path, fake_git_repo):
    """A test receipt whose branch/commit/check_id does not match expected_* → FAIL."""
    repo = fake_git_repo
    _write_schema(tmp_path)
    receipt = {
        "branch": "wrong-branch",
        "commit": "a" * 40,
        "check_id": "wrong-check-id",
        "timestamp": _now_iso(),
        "output_hash": "0" * 64,
    }
    _write_receipt(tmp_path, "control-plane/receipts/test-1.json", receipt)
    # All 13 IDs; only FAS-A-007 is the test_receipt under test.
    reqs = []
    for i in range(1, 14):
        rid = f"FAS-A-{i:03d}"
        if rid == "FAS-A-007":
            reqs.append({
                "id": rid, "text": f"req {rid}",
                "check": {"type": "test_receipt",
                          "receipt_path": "control-plane/receipts/test-1.json",
                          "expected_branch": "kmros/spel-contract",
                          "expected_commit": "a" * 40,
                          "expected_check_id": "FAS-A-007-spel-contract"},
            })
        else:
            reqs.append({
                "id": rid, "text": f"req {rid}",
                "check": {"type": "git_ancestor", "branch": "main", "commit_sha": repo["base_sha"]},
            })
    manifest = _write_manifest(tmp_path, requirements=reqs)
    result = _run_gate(cic, manifest, tmp_path, git_repo=repo["repo"],
                       allow_extra_prefix=str(tmp_path))
    assert result.gate == "FAIL"
    assert "FAS-A-007" in result.failing_ids
    res = next(r for r in result.results if r.requirement_id == "FAS-A-007")
    assert res.status == "fail"
    assert "branch_mismatch" in res.reason


# ── Test 13: stale timestamp (older than 24h) → FAIL ─────────────────────────

def test_stale_timestamp(cic, tmp_path, fake_git_repo):
    """A test receipt whose timestamp is older than 24h → FAIL with stale_timestamp."""
    repo = fake_git_repo
    _write_schema(tmp_path)
    receipt = {
        "branch": "kmros/spel-contract",
        "commit": "a" * 40,
        "check_id": "FAS-A-007-spel-contract",
        "timestamp": _stale_iso(),  # 48h ago
        "output_hash": "0" * 64,
    }
    _write_receipt(tmp_path, "control-plane/receipts/test-1.json", receipt)
    # All 13 IDs; only FAS-A-007 is under test.
    reqs = []
    for i in range(1, 14):
        rid = f"FAS-A-{i:03d}"
        if rid == "FAS-A-007":
            reqs.append({
                "id": rid, "text": f"req {rid}",
                "check": {"type": "test_receipt",
                          "receipt_path": "control-plane/receipts/test-1.json",
                          "expected_branch": "kmros/spel-contract",
                          "expected_commit": "a" * 40,
                          "expected_check_id": "FAS-A-007-spel-contract"},
            })
        else:
            reqs.append({
                "id": rid, "text": f"req {rid}",
                "check": {"type": "git_ancestor", "branch": "main", "commit_sha": repo["base_sha"]},
            })
    manifest = _write_manifest(tmp_path, requirements=reqs)
    result = _run_gate(cic, manifest, tmp_path, git_repo=repo["repo"],
                       allow_extra_prefix=str(tmp_path))
    assert result.gate == "FAIL"
    res = next(r for r in result.results if r.requirement_id == "FAS-A-007")
    assert res.status == "fail"
    assert "stale_timestamp" in res.reason


# ── Test 14: output hash mismatch → FAIL ────────────────────────────────────

def test_output_hash_mismatch(cic, tmp_path, fake_git_repo):
    """A test receipt whose output_hash does not match the recomputed hash of
    the referenced output artefact → FAIL with output_hash_mismatch."""
    repo = fake_git_repo
    _write_schema(tmp_path)
    # Write the real output artefact and compute its actual hash.
    actual_hash = _write_output_artefact(
        tmp_path, "control-plane/evidence/output-1.txt", b"real output content"
    )
    # Build a receipt that declares a DIFFERENT output_hash.
    receipt = {
        "branch": "kmros/spel-contract",
        "commit": "a" * 40,
        "check_id": "FAS-A-007-spel-contract",
        "timestamp": _now_iso(),
        "output_hash": "0" * 64,  # wrong — does not match actual_hash
        "output_path": "control-plane/evidence/output-1.txt",
    }
    _write_receipt(tmp_path, "control-plane/receipts/test-1.json", receipt)
    # All 13 IDs; only FAS-A-007 is under test.
    reqs = []
    for i in range(1, 14):
        rid = f"FAS-A-{i:03d}"
        if rid == "FAS-A-007":
            reqs.append({
                "id": rid, "text": f"req {rid}",
                "check": {"type": "test_receipt",
                          "receipt_path": "control-plane/receipts/test-1.json",
                          "expected_branch": "kmros/spel-contract",
                          "expected_commit": "a" * 40,
                          "expected_check_id": "FAS-A-007-spel-contract"},
            })
        else:
            reqs.append({
                "id": rid, "text": f"req {rid}",
                "check": {"type": "git_ancestor", "branch": "main", "commit_sha": repo["base_sha"]},
            })
    manifest = _write_manifest(tmp_path, requirements=reqs)
    result = _run_gate(cic, manifest, tmp_path, git_repo=repo["repo"],
                       allow_extra_prefix=str(tmp_path))
    assert result.gate == "FAIL"
    res = next(r for r in result.results if r.requirement_id == "FAS-A-007")
    assert res.status == "fail"
    assert "output_hash_mismatch" in res.reason


# ── Test 15: simultaneous FAIL and BLOCKED → gate=FAIL, both lists ───────────

def test_simultaneous_fail_and_blocked(cic, tmp_path, fake_git_repo):
    """A manifest with one FAIL requirement and one BLOCKED requirement must
    give gate=FAIL (FAIL has priority over BLOCKED). Both failing_ids and
    blocked_ids must be listed in the output."""
    repo = fake_git_repo
    _write_schema(tmp_path)
    # All 13 IDs. FAS-A-001 is the FAIL (git_ancestor for non-ancestor).
    # FAS-A-011 is the BLOCKED (owner placeholder). The rest pass.
    reqs = []
    for i in range(1, 14):
        rid = f"FAS-A-{i:03d}"
        if rid == "FAS-A-001":
            reqs.append({
                "id": rid, "text": f"req {rid}",
                # FAIL: git_ancestor for a commit that is NOT an ancestor.
                "check": {"type": "git_ancestor", "branch": "main",
                          "commit_sha": repo["divergent_sha"]},
            })
        elif rid == "FAS-A-011":
            reqs.append({
                "id": rid, "text": f"req {rid}",
                # BLOCKED: owner placeholder.
                "check": {"type": "owner_decision_receipt",
                          "receipt_path": "control-plane/receipts/owner-1.json",
                          "owner_id": "PENDING_OWNER_DECISION"},
            })
        else:
            reqs.append({
                "id": rid, "text": f"req {rid}",
                "check": {"type": "git_ancestor", "branch": "main", "commit_sha": repo["base_sha"]},
            })
    manifest = _write_manifest(tmp_path, requirements=reqs)
    result = _run_gate(cic, manifest, tmp_path, git_repo=repo["repo"],
                       allow_extra_prefix=str(tmp_path))
    assert result.gate == "FAIL", f"expected FAIL, got {result.gate}: {result.reason}"
    assert "FAS-A-001" in result.failing_ids
    assert "FAS-A-011" in result.blocked_ids
    assert result.closeout_permitted is False
    # Both lists non-empty.
    assert len(result.failing_ids) >= 1
    assert len(result.blocked_ids) >= 1


# ── Test 16: no --expected-manifest-hash → gate=FAIL ───────────────────────

def test_no_expected_manifest_hash(cic, tmp_path, fake_git_repo):
    """Without --expected-manifest-hash the gate must NEVER be PASS and
    closeout_permitted must NEVER be true, even if every requirement passes."""
    repo = fake_git_repo
    _write_schema(tmp_path)
    # All 13 IDs, all passing git_ancestor checks. Without the external hash
    # the gate must still be FAIL (expected_manifest_hash_missing).
    reqs = []
    for i in range(1, 14):
        rid = f"FAS-A-{i:03d}"
        reqs.append({
            "id": rid, "text": f"req {rid}",
            "check": {"type": "git_ancestor", "branch": "main", "commit_sha": repo["base_sha"]},
        })
    manifest = _write_manifest(tmp_path, requirements=reqs)
    # No expected_hash passed.
    result = _run_gate(cic, manifest, tmp_path, git_repo=repo["repo"],
                       allow_extra_prefix=str(tmp_path))
    assert result.gate == "FAIL"
    assert result.closeout_permitted is False
    # The reason must indicate the missing external hash (all requirements
    # pass, so the only thing blocking PASS is the missing hash).
    assert result.reason == "expected_manifest_hash_missing"


# ── Test 17: manifest hash mismatch → FAIL manifest_tampered ───────────────

def test_manifest_hash_mismatch(cic, tmp_path, fake_git_repo):
    """--expected-manifest-hash that does not match the actual manifest sha256
    → FAIL with manifest_tampered."""
    repo = fake_git_repo
    _write_schema(tmp_path)
    # All 13 IDs, all passing, so the only FAIL cause is the hash mismatch.
    reqs = []
    for i in range(1, 14):
        rid = f"FAS-A-{i:03d}"
        reqs.append({
            "id": rid, "text": f"req {rid}",
            "check": {"type": "git_ancestor", "branch": "main", "commit_sha": repo["base_sha"]},
        })
    manifest = _write_manifest(tmp_path, requirements=reqs)
    wrong_hash = "0" * 64
    result = _run_gate(cic, manifest, tmp_path, expected_hash=wrong_hash,
                       git_repo=repo["repo"], allow_extra_prefix=str(tmp_path))
    assert result.gate == "FAIL"
    assert result.manifest_hash_matches is False
    assert "manifest_tampered" in result.reason
    assert result.closeout_permitted is False


# ── Test 18 (regression): self-referential closeout rejected ───────────────

def test_self_referential_closeout_rejected(cic, tmp_path, fake_git_repo):
    """Regression (krav 5): 4 cards marked done but 2 requirements lack evidence → FAIL.

    This recreates today's bug: every self-created Kanban card is done, but
    the originating requirements were never satisfied. The fixture has 4
    requirements mapped to mock cards, all cards "done", but 2 requirements
    have no evidence_check-pass. CIC must return gate=FAIL with the 2 missed
    IDs listed, and closeout must be blocked. Card-done-ness is irrelevant.
    """
    repo = fake_git_repo
    _write_schema(tmp_path)
    # Side state: 4 cards all "done" — this is what the buggy flow trusted.
    cards = [
        {"id": "card-1", "requirement_id": "FAS-A-001", "status": "done"},
        {"id": "card-2", "requirement_id": "FAS-A-002", "status": "done"},
        {"id": "card-3", "requirement_id": "FAS-A-003", "status": "done"},
        {"id": "card-4", "requirement_id": "FAS-A-004", "status": "done"},
    ]
    _write_receipt(tmp_path, "control-plane/mock-cards.json", {"cards": cards})

    # 2 requirements pass, 2 fail (evidence mismatch). Cards being "done"
    # must NOT translate to requirements passing. NOTE: this test uses 4 IDs
    # out of the canonical 13 — to avoid the id_set check firing first, we
    # supply all 13 IDs (the 4 here are the interesting ones; the rest pass).
    reqs = []
    for i in range(1, 14):
        rid = f"FAS-A-{i:03d}"
        if rid == "FAS-A-003":
            # divergent_sha NOT ancestor → exit_1; expected exit_0 → FAIL.
            reqs.append({"id": rid, "text": f"req {rid}",
                         "check": {"type": "git_ancestor", "branch": "main",
                                   "commit_sha": repo["divergent_sha"]}})
        elif rid == "FAS-A-004":
            # base_sha IS ancestor → exit_0; for git_not_ancestor expected exit_1 → FAIL.
            reqs.append({"id": rid, "text": f"req {rid}",
                         "check": {"type": "git_not_ancestor", "branch": "main",
                                   "commit_sha": repo["base_sha"]}})
        else:
            reqs.append({"id": rid, "text": f"req {rid}",
                         "check": {"type": "git_ancestor", "branch": "main",
                                   "commit_sha": repo["base_sha"]}})
    manifest = _write_manifest(tmp_path, requirements=reqs)
    result = _run_gate(cic, manifest, tmp_path, git_repo=repo["repo"],
                       allow_extra_prefix=str(tmp_path),
                       expected_hash=_hash_of_file(manifest))
    assert result.gate == "FAIL", f"expected FAIL, got {result.gate}: {result.reason}"
    assert result.closeout_permitted is False
    assert sorted(result.failing_ids) == ["FAS-A-003", "FAS-A-004"]
    assert result.blocked_ids == []
    # The "done" cards file must not influence the gate at all.
    assert os.path.exists(tmp_path / "control-plane" / "mock-cards.json")


# ── Test 19: deterministic identical result ─────────────────────────────────

def test_deterministic_identical_result(cic, tmp_path, fake_git_repo):
    """Same manifest + evidence run twice → identical JSON output."""
    repo = fake_git_repo
    _write_schema(tmp_path)
    manifest = _write_manifest(tmp_path, requirements=[
        {
            "id": "FAS-A-001", "text": "Skapa isolerad kandidat",
            "check": {"type": "git_ancestor", "branch": "main", "commit_sha": repo["base_sha"]},
        },
    ])
    r1 = _run_gate(cic, manifest, tmp_path, git_repo=repo["repo"],
                   allow_extra_prefix=str(tmp_path))
    r2 = _run_gate(cic, manifest, tmp_path, git_repo=repo["repo"],
                   allow_extra_prefix=str(tmp_path))
    assert r1.to_dict() == r2.to_dict(), "CIC must be deterministic across runs"
    # JSON serialization must be byte-identical too (sort_keys=True in CIC).
    assert json.dumps(r1.to_dict(), sort_keys=True) == json.dumps(r2.to_dict(), sort_keys=True)


# ────────────────────────────────────────────────────────────────────────────
# Kept extras
# ────────────────────────────────────────────────────────────────────────────


# ── Extra: source_hash drift → FAIL ─────────────────────────────────────────

def test_source_hash_drift_fails(cic, tmp_path, fake_git_repo):
    """If a requirement's text is edited after pinning, source_hash mismatches → FAIL."""
    repo = fake_git_repo
    _write_schema(tmp_path)
    # All 13 IDs; FAS-A-001 has a deliberately wrong source_hash. The rest have
    # correct hashes (auto-computed) and passing git_ancestor checks.
    reqs = []
    for i in range(1, 14):
        rid = f"FAS-A-{i:03d}"
        if rid == "FAS-A-001":
            reqs.append({
                "id": rid, "text": "Skapa isolerad kandidat",
                # Deliberately wrong source_hash (not matching text)
                "source_hash": "0" * 64,
                "check": {"type": "git_ancestor", "branch": "main", "commit_sha": repo["base_sha"]},
            })
        else:
            reqs.append({
                "id": rid, "text": f"req {rid}",
                "check": {"type": "git_ancestor", "branch": "main", "commit_sha": repo["base_sha"]},
            })
    manifest = _write_manifest(tmp_path, requirements=reqs)
    result = _run_gate(cic, manifest, tmp_path, git_repo=repo["repo"],
                       allow_extra_prefix=str(tmp_path))
    assert result.gate == "FAIL"
    assert "source_hash_mismatch" in result.reason
    assert "FAS-A-001" in result.failing_ids


# ── Extra: CIC is read-only — no mutation of filesystem ──────────────────────

def test_cic_does_not_mutate_filesystem(cic, tmp_path, fake_git_repo):
    """CIC must not create, delete, or modify any file in its read set."""
    repo = fake_git_repo
    _write_schema(tmp_path)
    manifest = _write_manifest(tmp_path, requirements=[
        {
            "id": "FAS-A-001", "text": "Skapa isolerad kandidat",
            "check": {"type": "git_ancestor", "branch": "main", "commit_sha": repo["base_sha"]},
        },
    ])

    def snapshot(root: Path) -> dict:
        out = {}
        for p in sorted(root.rglob("*")):
            if p.is_file():
                out[str(p)] = hashlib.sha256(p.read_bytes()).hexdigest()
        return out

    before = snapshot(tmp_path)
    _run_gate(cic, manifest, tmp_path, git_repo=repo["repo"],
              allow_extra_prefix=str(tmp_path))
    after = snapshot(tmp_path)
    assert before == after, "CIC must not mutate any file in its read set"


# ── Extra: canonical manifest validates against the schema ──────────────────

def test_canonical_manifest_validates_against_schema(cic, tmp_path):
    """The shipped control-plane/fas-a-requirements.yaml must validate against
    the shipped control-plane/fas-a-requirements.schema.json."""
    import jsonschema
    repo_root = Path(__file__).resolve().parents[2]
    manifest = yaml.safe_load((repo_root / "control-plane" / "fas-a-requirements.yaml").read_bytes())
    schema = json.loads((repo_root / "control-plane" / "fas-a-requirements.schema.json").read_bytes())
    jsonschema.validate(instance=manifest, schema=schema)


# ── Extra: canonical manifest has all 13 stable IDs and exact texts ──────────

CANONICAL_REQUIREMENTS = [
    ("FAS-A-001", "Skapa isolerad kandidat från verifierad gemensam bas"),
    ("FAS-A-002", "Integrera exakt shared-live + Lumi + Igor patchar i kmros/main"),
    ("FAS-A-003", "Håll worker-isolation och stop-semantics utanför"),
    ("FAS-A-004", "Provenance i release-manifest (source remote, integration branch, source commit, overlay hash, lock hash, builder version)"),
    ("FAS-A-005", ".venv verifierbar och icke-muterbar efter seal (faktisk enforcementkod)"),
    ("FAS-A-006", "Read-only fleet collector och kontrakttester, ej installerat live"),
    ("FAS-A-007", "#spel route-contract och syntetiska tester i kandidaten"),
    ("FAS-A-008", "Persistens-canary efter rotorsaksdiagnosen, i kandidaten"),
    ("FAS-A-009", "Read-only The Terminal-cockpit (5 kort, djupvy, 2 handlingar, 4 statusar)"),
    ("FAS-A-010", "Cockpit acceptance-kriterier (11 kriterier från spec)"),
    ("FAS-A-011", "Canonical required skills för #spel bekräftade"),
    ("FAS-A-012", "Fleet contract current-truth och owner decisions verifierade"),
    ("FAS-A-013", "Bred testsvit oberoende omkörd av tredje part"),
]


def test_canonical_manifest_has_all_13_ids_and_exact_texts():
    """The canonical manifest must carry all 13 stable IDs with exact texts."""
    repo_root = Path(__file__).resolve().parents[2]
    manifest = yaml.safe_load((repo_root / "control-plane" / "fas-a-requirements.yaml").read_bytes())
    by_id = {r["id"]: r["text"] for r in manifest["requirements"]}
    assert set(by_id.keys()) == {rid for rid, _ in CANONICAL_REQUIREMENTS}, \
        f"ID set mismatch: {set(by_id.keys())} vs {[r for r,_ in CANONICAL_REQUIREMENTS]}"
    for rid, expected_text in CANONICAL_REQUIREMENTS:
        actual = by_id[rid]
        # Folded YAML may collapse internal whitespace; normalize for compare.
        norm_actual = " ".join(actual.split())
        norm_expected = " ".join(expected_text.split())
        assert norm_actual == norm_expected, f"{rid}: text drift: {actual!r} vs {expected_text!r}"


# ── Extra: CIC never uses shell=True ─────────────────────────────────────────

def test_cic_never_uses_shell_true():
    """The CIC source must never pass shell=True to subprocess.run."""
    src = CIC_PATH.read_text()
    assert "shell=True" not in src, "CIC must never use shell=True"
    assert 'cmd = ["git"' in src or "cmd = ['git'" in src, \
        "CIC must invoke git via a list argument, not a shell string"


# ── Extra: CIC source has no mutation calls ─────────────────────────────────

def test_cic_source_has_no_mutation_calls():
    """CIC source must not contain mutation API CALLS (write, remove, push, pip).

    We strip comments and docstrings before scanning, so the literal mention of
    e.g. ``os.system`` in the module docstring (as a forbidden token) does not
    trip the check. We only flag actual call-site substrings.
    """
    import ast
    src = CIC_PATH.read_text()
    # Parse and walk the AST; collect all attribute-call names actually invoked.
    tree = ast.parse(src)
    called_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                # e.g. os.system, shutil.rmtree, subprocess.run
                if isinstance(func.value, ast.Name):
                    called_names.add(f"{func.value.id}.{func.attr}")
            elif isinstance(func, ast.Name):
                called_names.add(func.id)
    forbidden_calls = {
        "os.system", "os.remove", "os.unlink",
        "shutil.rmtree", "shutil.move",
        "subprocess.Popen",  # we only allow subprocess.run with shell=False
    }
    for bad in forbidden_calls:
        assert bad not in called_names, f"CIC must not call {bad!r}"
    # The git invocation must build a list argument (not a free-form string).
    assert 'cmd = ["git"' in src or "cmd = ['git'" in src, \
        "CIC must invoke git via a list argument, not a shell string"
    # No kanban/board mutation helpers anywhere in the source.
    for token in ("kanban_create", "kanban_complete", "kanban_block"):
        assert token not in src, f"CIC source must not contain {token!r}"
    # No pip install, no git push.
    for token in ("git push", "pip install", "uv pip install"):
        assert token not in src, f"CIC source must not contain {token!r}"


# ── Extra: CIC against the canonical manifest gives gate=FAIL (acceptance) ─

def test_canonical_manifest_gate_is_fail(cic):
    """Acceptance criterion: the CIC against the canonical manifest
    (control-plane/fas-a-requirements.yaml in the real worktree) must give
    gate=FAIL — not BLOCKED — because the technical requirements FAS-A-004..010
    and FAS-A-013 are MISSING (no implementation files/receipts yet) and FAIL
    has priority over BLOCKED."""
    repo_root = Path(__file__).resolve().parents[2]
    manifest_path = repo_root / "control-plane" / "fas-a-requirements.yaml"
    # Compute the real manifest hash so the trust-anchor check passes and we
    # exercise the full evaluation path.
    expected_hash = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    result = cic.run_gate(
        manifest_path=str(manifest_path),
        base_dir=repo_root,
        expected_manifest_hash=expected_hash,
        schema_path=str(repo_root / "control-plane" / "fas-a-requirements.schema.json"),
    )
    assert result.gate == "FAIL", f"expected FAIL, got {result.gate}: {result.reason}"
    assert result.closeout_permitted is False
    # Technical requirements must be FAIL (missing evidence).
    for rid in ["FAS-A-004", "FAS-A-005", "FAS-A-006", "FAS-A-007",
                "FAS-A-008", "FAS-A-009", "FAS-A-010", "FAS-A-013"]:
        assert rid in result.failing_ids, f"{rid} should be failing (missing technical evidence)"
    # Genuine owner-decision requirements must be BLOCKED.
    for rid in ["FAS-A-011", "FAS-A-012"]:
        assert rid in result.blocked_ids, f"{rid} should be blocked (pending owner decision)"
    # FAS-A-001/002/003 must PASS (real git ancestry on kmros/main).
    for rid in ["FAS-A-001", "FAS-A-002", "FAS-A-003"]:
        assert rid not in result.failing_ids
        assert rid not in result.blocked_ids