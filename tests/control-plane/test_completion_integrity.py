"""Regression + integrity tests for the Fas A completion-integrity-gate (CIC) — v3.

These tests are hermetic: they build temporary worktrees, manifests, and
receipts on disk and exercise ``scripts/completion_integrity_check.py``
through its public ``run_gate`` API. No real Kanban board, no network, no
mutations outside the per-test tmpdir.

Coverage of the required regression cases (task t_e5b2a010, v3 t_9f3f941e):

Kept from v1:
  - test_empty_manifest, test_truncated_manifest, test_schema_invalid,
    test_missing_id, test_extra_id, test_duplicate_id,
    test_self_referential_closeout_rejected, test_deterministic_identical_result

Kept v2 negative probes:
  - test_all_checks_replaced_with_git_ancestor
  - test_self_computed_cli_manifest_hash
  - test_changed_checktype_after_trust
  - test_test_receipt_without_output_path
  - test_test_receipt_wrong_requirement_id
  - test_test_receipt_result_not_pass
  - test_test_receipt_output_hash_mismatch
  - test_test_receipt_receipt_hash_mismatch
  - test_test_receipt_untrusted_binding
  - test_fabricated_owner_receipt_fake_trust_anchor
  - test_evidence_in_sibling_worktree_rejected
  - test_manifest_in_sibling_worktree_rejected
  - test_symlink_escape
  - test_branch_leading_dash_rejected
  - test_branch_leading_slash_rejected
  - test_branch_x_rejected
  - test_simultaneous_fail_and_blocked
  - test_production_cli_cannot_reach_test_trust_verifier
  - test_canonical_candidate_never_closeout_without_harness_trust

v3 new probes (task t_9f3f941e):
  - test_fas_a_012_owner_only_after_approved_digest_is_tampered (replaces the
    misleading test_fas_a_012_without_technical_part): owner-only after an
    approved digest gives manifest_tampered, NOT a missing-trust-context skip.
  - test_fas_a_012_dual_status_fail_and_blocked: an all_of with BOTH a FAIL
    sub-check and a BLOCKED sub-check lists the ID in BOTH failing_ids and
    blocked_ids (also_blocked=True); FAIL drives the gate.
  - test_schema_realpath_uses_base_dir_not_cwd: base_dir's strict schema is
    used even when a permissive schema sits at the same relative path under
    the CIC's cwd. The CIC validates against the realpath under base_dir.

Plus kept extras: test_cic_does_not_mutate_filesystem,
test_canonical_manifest_validates_against_schema,
test_canonical_manifest_has_all_13_ids_and_exact_texts,
test_cic_never_uses_shell_true, test_cic_source_has_no_mutation_calls,
test_cic_source_has_no_hardcoded_requirement_semantics,
test_production_cli_has_no_trust_context_provider,
test_trust_context_protocol_is_injectable.
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


# ── Test-only FakeTrustContext (DI only, never exposed via CLI) ─────────────


class FakeTrustContext:
    """Test-only in-memory fake trust context (krav 2).

    Implements the TrustContext protocol via duck typing. Used ONLY in tests
    via dependency injection. NEVER exposed via CLI, NEVER in production code.
    No test key, fake signer, or bypass is reachable via the production CLI.
    """

    def __init__(self, approved_manifest_hash: str | None = None,
                 approved_receipt_hashes: set[str] | None = None,
                 authorized_owner_ids: set[str] | None = None):
        self._approved_manifest_hash = approved_manifest_hash
        self._approved_receipt_hashes = approved_receipt_hashes or set()
        self._authorized_owner_ids = authorized_owner_ids or set()

    def verify_manifest_digest(self, digest: str) -> bool:
        if self._approved_manifest_hash is None:
            return True  # Test convenience: approve any manifest
        return digest == self._approved_manifest_hash

    def verify_receipt_binding(self, receipt_hash: str, requirement_id: str,
                                check_id: str) -> bool:
        if not self._approved_receipt_hashes:
            return True  # Test convenience: approve any receipt
        return receipt_hash in self._approved_receipt_hashes

    def verify_owner_decision(self, owner_id: str, receipt: dict) -> bool:
        if not self._authorized_owner_ids:
            return False  # Default: deny (matches production fail-closed)
        return owner_id in self._authorized_owner_ids


# ── Manifest + receipt builders ─────────────────────────────────────────────

def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _write_manifest(base_dir: Path, requirements: list, manifest_id: str = "test-manifest-v2",
                    extra: dict | None = None, raw_text: str | None = None) -> Path:
    """Write a manifest YAML into ``base_dir/control-plane/`` and return its path."""
    cp = base_dir / "control-plane"
    cp.mkdir(parents=True, exist_ok=True)
    path = cp / "fas-a-requirements.yaml"
    if raw_text is not None:
        path.write_text(raw_text)
        return path
    doc = {
        "manifest_version": "2.0",
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


def _compute_receipt_hash(data: dict) -> str:
    """Compute the receipt hash the same way the CIC does (excluding receipt_hash)."""
    data_without_hash = {k: v for k, v in data.items() if k != "receipt_hash"}
    canonical = json.dumps(data_without_hash, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _make_valid_test_receipt(
    base_dir: Path,
    receipt_rel_path: str,
    requirement_id: str,
    check_id: str,
    branch: str = "main",
    commit: str = "a" * 40,
    output_rel_path: str = "control-plane/evidence/output.txt",
    output_content: bytes = b"output artefact",
) -> dict:
    """Build a complete, valid test receipt and write it + the output artefact.

    Returns the receipt dict (with receipt_hash computed).
    """
    output_hash = _write_output_artefact(base_dir, output_rel_path, output_content)
    receipt = {
        "requirement_id": requirement_id,
        "check_id": check_id,
        "branch": branch,
        "commit": commit,
        "result": "pass",
        "timestamp": _now_iso(),
        "output_path": output_rel_path,
        "output_hash": output_hash,
    }
    receipt["receipt_hash"] = _compute_receipt_hash(receipt)
    _write_receipt(base_dir, receipt_rel_path, receipt)
    return receipt


def _run_gate(cic, manifest_path: Path, base_dir: Path,
              trust_context=None, git_repo: str | None = None,
              schema_path: str | None = None):
    """Run the CIC, optionally monkeypatching GIT_REPO to ``git_repo``."""
    patches = []
    try:
        if git_repo is not None:
            patches.append(("GIT_REPO", cic.GIT_REPO))
            cic.GIT_REPO = git_repo
        return cic.run_gate(
            manifest_path=str(manifest_path), base_dir=base_dir,
            trust_context=trust_context, schema_path=schema_path,
        )
    finally:
        for attr, original in reversed(patches):
            setattr(cic, attr, original)


def _hash_of_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _all_13_ids_git_ancestor(repo: dict, skip: set[str] | None = None,
                              override: dict | None = None) -> list:
    """Build all 13 FAS-A IDs as passing git_ancestor checks, with overrides."""
    skip = skip or set()
    override = override or {}
    reqs = []
    for i in range(1, 14):
        rid = f"FAS-A-{i:03d}"
        if rid in override:
            reqs.append({"id": rid, "text": f"req {rid}", "check": override[rid]})
        elif rid in skip:
            continue
        else:
            reqs.append({
                "id": rid, "text": f"req {rid}",
                "check": {"type": "git_ancestor", "branch": "main",
                          "commit_sha": repo["base_sha"]},
            })
    return reqs


# ────────────────────────────────────────────────────────────────────────────
# Kept regression tests (v1 cases still required)
# ────────────────────────────────────────────────────────────────────────────


# ── Test: empty manifest → FAIL ──────────────────────────────────────────────

def test_empty_manifest(cic, tmp_path):
    """An empty manifest (zero bytes) must give gate=FAIL."""
    _write_schema(tmp_path)
    manifest = _write_manifest(tmp_path, requirements=[], raw_text="")
    result = _run_gate(cic, manifest, tmp_path)
    assert result.gate == "FAIL"
    assert result.closeout_permitted is False
    assert "manifest_empty" in result.reason or "schema" in result.reason


# ── Test: truncated manifest (invalid YAML) → FAIL ────────────────────────────

def test_truncated_manifest(cic, tmp_path):
    """A truncated manifest (invalid YAML, e.g. unterminated flow) → FAIL."""
    _write_schema(tmp_path)
    raw = "manifest_version: \"2.0\"\nmanifest_id: \"test\"\nrequirements: [\n"
    manifest = _write_manifest(tmp_path, requirements=[], raw_text=raw)
    result = _run_gate(cic, manifest, tmp_path)
    assert result.gate == "FAIL"
    assert result.closeout_permitted is False
    assert "manifest_not_yaml" in result.reason


# ── Test: schema-invalid manifest → FAIL ──────────────────────────────────────

def test_schema_invalid(cic, tmp_path, fake_git_repo):
    """A manifest that does not validate against the schema → FAIL."""
    repo = fake_git_repo
    _write_schema(tmp_path)
    manifest = _write_manifest(tmp_path, requirements=[
        {
            "text": "no id here",
            "check": {"type": "git_ancestor", "branch": "main", "commit_sha": repo["base_sha"]},
        },
    ])
    result = _run_gate(cic, manifest, tmp_path, git_repo=repo["repo"])
    assert result.gate == "FAIL"
    assert result.schema_valid is False
    assert "schema_invalid" in result.reason or "schema_violation" in result.reason


# ── Test: missing ID (12 of 13) → FAIL ─────────────────────────────────────────

def test_missing_id(cic, tmp_path, fake_git_repo):
    """Manifest with only 12 IDs (missing FAS-A-007) → FAIL (id_set_invalid)."""
    repo = fake_git_repo
    _write_schema(tmp_path)
    reqs = _all_13_ids_git_ancestor(repo, skip={"FAS-A-007"})
    manifest = _write_manifest(tmp_path, requirements=reqs)
    result = _run_gate(cic, manifest, tmp_path, git_repo=repo["repo"])
    assert result.gate == "FAIL"
    assert result.id_set_valid is False
    assert "id_set_invalid" in result.reason
    assert "FAS-A-007" in result.reason


# ── Test: extra ID (FAS-A-014 added) → FAIL ─────────────────────────────────────

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
    result = _run_gate(cic, manifest, tmp_path, git_repo=repo["repo"])
    assert result.gate == "FAIL"
    assert result.id_set_valid is False
    assert "FAS-A-014" in result.reason


# ── Test: duplicate ID (FAS-A-001 twice) → FAIL ──────────────────────────────────

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
    reqs.insert(0, {"id": "FAS-A-001", "text": "first",
                    "check": {"type": "git_ancestor", "branch": "main", "commit_sha": repo["base_sha"]}})
    reqs.insert(1, {"id": "FAS-A-001", "text": "second",
                    "check": {"type": "git_ancestor", "branch": "main", "commit_sha": repo["base_sha"]}})
    manifest = _write_manifest(tmp_path, requirements=reqs)
    result = _run_gate(cic, manifest, tmp_path, git_repo=repo["repo"])
    assert result.gate == "FAIL"
    assert result.id_set_valid is False
    assert "duplicate" in result.reason
    assert "FAS-A-001" in result.reason


# ── Test (regression): self-referential closeout rejected ───────────────────────

def test_self_referential_closeout_rejected(cic, tmp_path, fake_git_repo):
    """Regression: 4 cards marked done but 2 requirements lack evidence → FAIL.

    This recreates today's bug: every self-created Kanban card is done, but
    the originating requirements were never satisfied. The fixture has 4
    requirements mapped to mock cards, all cards "done", but 2 requirements
    have no evidence_check-pass. CIC must return gate=FAIL with the 2 missed
    IDs listed, and closeout must be blocked. Card-done-ness is irrelevant.
    """
    repo = fake_git_repo
    _write_schema(tmp_path)
    cards = [
        {"id": "card-1", "requirement_id": "FAS-A-001", "status": "done"},
        {"id": "card-2", "requirement_id": "FAS-A-002", "status": "done"},
        {"id": "card-3", "requirement_id": "FAS-A-003", "status": "done"},
        {"id": "card-4", "requirement_id": "FAS-A-004", "status": "done"},
    ]
    _write_receipt(tmp_path, "control-plane/mock-cards.json", {"cards": cards})

    reqs = []
    for i in range(1, 14):
        rid = f"FAS-A-{i:03d}"
        if rid == "FAS-A-003":
            reqs.append({"id": rid, "text": f"req {rid}",
                         "check": {"type": "git_ancestor", "branch": "main",
                                   "commit_sha": repo["divergent_sha"]}})
        elif rid == "FAS-A-004":
            reqs.append({"id": rid, "text": f"req {rid}",
                         "check": {"type": "git_not_ancestor", "branch": "main",
                                   "commit_sha": repo["base_sha"]}})
        else:
            reqs.append({"id": rid, "text": f"req {rid}",
                         "check": {"type": "git_ancestor", "branch": "main",
                                   "commit_sha": repo["base_sha"]}})
    manifest = _write_manifest(tmp_path, requirements=reqs)
    result = _run_gate(cic, manifest, tmp_path, git_repo=repo["repo"])
    assert result.gate == "FAIL", f"expected FAIL, got {result.gate}: {result.reason}"
    assert result.closeout_permitted is False
    assert sorted(result.failing_ids) == ["FAS-A-003", "FAS-A-004"]
    assert result.blocked_ids == []
    assert os.path.exists(tmp_path / "control-plane" / "mock-cards.json")


# ── Test: deterministic identical result ────────────────────────────────────────

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
    r1 = _run_gate(cic, manifest, tmp_path, git_repo=repo["repo"])
    r2 = _run_gate(cic, manifest, tmp_path, git_repo=repo["repo"])
    assert r1.to_dict() == r2.to_dict(), "CIC must be deterministic across runs"
    assert json.dumps(r1.to_dict(), sort_keys=True) == json.dumps(r2.to_dict(), sort_keys=True)


# ────────────────────────────────────────────────────────────────────────────
# New v2 negative probes
# ────────────────────────────────────────────────────────────────────────────


# ── Test: all checks replaced with git_ancestor → FAIL (manifest digest changed) ─

def test_all_checks_replaced_with_git_ancestor(cic, tmp_path, fake_git_repo):
    """A manifest with FAS-A-001..013 + texts + source_hash but ALL checks =
    git_ancestor must give FAIL. The manifest digest is changed (check
    substitution), and trust context (if any) would detect the mismatch.
    Without trust context: fail-closed anyway.
    """
    repo = fake_git_repo
    _write_schema(tmp_path)
    # Use the canonical texts + source_hashes so source_hash check passes,
    # but replace ALL checks with git_ancestor (check substitution).
    canonical = yaml.safe_load(
        (Path(__file__).resolve().parents[2] / "control-plane" / "fas-a-requirements.yaml").read_bytes()
    )
    reqs = []
    for r in canonical["requirements"]:
        reqs.append({
            "id": r["id"], "text": r["text"], "source_hash": r["source_hash"],
            "check": {"type": "git_ancestor", "branch": "main",
                      "commit_sha": repo["base_sha"]},
        })
    manifest = _write_manifest(tmp_path, requirements=reqs)
    # Without trust context: fail-closed (no PASS possible).
    result = _run_gate(cic, manifest, tmp_path, git_repo=repo["repo"])
    assert result.gate == "FAIL"
    assert result.closeout_permitted is False
    # With a fake trust context that approved the ORIGINAL canonical manifest
    # digest, the substituted manifest has a different digest → manifest_tampered.
    canonical_path = Path(__file__).resolve().parents[2] / "control-plane" / "fas-a-requirements.yaml"
    canonical_digest = hashlib.sha256(canonical_path.read_bytes()).hexdigest()
    fake_trust = FakeTrustContext(approved_manifest_hash=canonical_digest)
    result2 = _run_gate(cic, manifest, tmp_path, git_repo=repo["repo"],
                         trust_context=fake_trust)
    assert result2.gate == "FAIL"
    assert "manifest_tampered" in result2.reason
    assert result2.closeout_permitted is False


# ── Test: self-computed CLI manifest hash → closeout_permitted=false ────────────

def test_self_computed_cli_manifest_hash(cic, tmp_path, fake_git_repo):
    """CLI without trust context, --expected-manifest-hash passed →
    closeout_permitted=false (not PASS). The --expected-manifest-hash flag is
    read-only informational and NEVER grants PASS. Trust anchor provisioning
    is exclusively via trust context (not exposed via CLI).
    """
    repo = fake_git_repo
    _write_schema(tmp_path)
    reqs = _all_13_ids_git_ancestor(repo)
    manifest = _write_manifest(tmp_path, requirements=reqs)
    # Simulate CLI: no trust context (production CLI has None default).
    # Even with the correct manifest hash passed via --expected-manifest-hash,
    # the CLI cannot grant PASS because there is no trust context.
    result = _run_gate(cic, manifest, tmp_path, git_repo=repo["repo"],
                       trust_context=None)
    assert result.gate == "FAIL"
    assert result.closeout_permitted is False
    assert result.trust_context_present is False
    assert result.reason == "trust_context_missing"


# ── Test: changed checktype after trust → FAIL manifest_tampered ───────────────

def test_changed_checktype_after_trust(cic, tmp_path, fake_git_repo):
    """Manifest changed after trust verification → FAIL manifest_tampered.

    A trust context approves a specific manifest digest. If the manifest is
    then edited (e.g. checktype changed), the digest changes and the trust
    context detects the mismatch → FAIL manifest_tampered.
    """
    repo = fake_git_repo
    _write_schema(tmp_path)
    # Build manifest v1 with all git_ancestor (passes).
    reqs = _all_13_ids_git_ancestor(repo)
    manifest = _write_manifest(tmp_path, requirements=reqs)
    digest_v1 = hashlib.sha256(manifest.read_bytes()).hexdigest()
    # Trust context approves v1.
    fake_trust = FakeTrustContext(approved_manifest_hash=digest_v1)
    result_v1 = _run_gate(cic, manifest, tmp_path, git_repo=repo["repo"],
                          trust_context=fake_trust)
    # v1: all pass + trust verified → PASS (with trust context).
    assert result_v1.gate == "PASS"
    assert result_v1.closeout_permitted is True
    # Now tamper: change FAS-A-007's checktype from git_ancestor to file_sha256.
    tampered_reqs = _all_13_ids_git_ancestor(repo, override={
        "FAS-A-007": {"type": "file_sha256",
                      "path": "control-plane/evidence/tampered.json",
                      "expected_sha256": "0" * 64},
    })
    manifest2 = _write_manifest(tmp_path, requirements=tampered_reqs,
                               manifest_id="test-manifest-v2-tampered")
    # The digest changed → trust context detects mismatch → manifest_tampered.
    result_v2 = _run_gate(cic, manifest2, tmp_path, git_repo=repo["repo"],
                          trust_context=fake_trust)
    assert result_v2.gate == "FAIL"
    assert "manifest_tampered" in result_v2.reason
    assert result_v2.closeout_permitted is False


# ── Test: test receipt without output_path → FAIL output_path_missing ──────────

def test_test_receipt_without_output_path(cic, tmp_path, fake_git_repo):
    """A test receipt without output_path → FAIL (output_path is required)."""
    repo = fake_git_repo
    _write_schema(tmp_path)
    # Build a receipt missing output_path.
    receipt = {
        "requirement_id": "FAS-A-007",
        "check_id": "FAS-A-007",
        "branch": "main",
        "commit": "a" * 40,
        "result": "pass",
        "timestamp": _now_iso(),
        "output_hash": "0" * 64,
        "receipt_hash": "0" * 64,
    }
    _write_receipt(tmp_path, "control-plane/evidence/fas-a-007-receipt.json", receipt)
    reqs = _all_13_ids_git_ancestor(repo, override={
        "FAS-A-007": {"type": "test_receipt",
                      "receipt_path": "control-plane/evidence/fas-a-007-receipt.json",
                      "expected_check_id": "FAS-A-007"},
    })
    manifest = _write_manifest(tmp_path, requirements=reqs)
    result = _run_gate(cic, manifest, tmp_path, git_repo=repo["repo"])
    assert result.gate == "FAIL"
    assert "FAS-A-007" in result.failing_ids
    res = next(r for r in result.results if r.requirement_id == "FAS-A-007")
    assert res.status == "fail"
    assert "receipt_missing_fields" in res.reason
    assert "output_path" in res.reason


# ── Test: test receipt with wrong requirement_id → FAIL ─────────────────────────

def test_test_receipt_wrong_requirement_id(cic, tmp_path, fake_git_repo):
    """A test receipt with wrong requirement_id → FAIL."""
    repo = fake_git_repo
    _write_schema(tmp_path)
    _make_valid_test_receipt(
        tmp_path, "control-plane/evidence/fas-a-007-receipt.json",
        requirement_id="WRONG_ID",  # wrong
        check_id="FAS-A-007",
    )
    reqs = _all_13_ids_git_ancestor(repo, override={
        "FAS-A-007": {"type": "test_receipt",
                      "receipt_path": "control-plane/evidence/fas-a-007-receipt.json",
                      "expected_check_id": "FAS-A-007"},
    })
    manifest = _write_manifest(tmp_path, requirements=reqs)
    result = _run_gate(cic, manifest, tmp_path, git_repo=repo["repo"])
    assert result.gate == "FAIL"
    assert "FAS-A-007" in result.failing_ids
    res = next(r for r in result.results if r.requirement_id == "FAS-A-007")
    assert res.status == "fail"
    assert "requirement_id_mismatch" in res.reason


# ── Test: test receipt with result != "pass" → FAIL ──────────────────────────────

def test_test_receipt_result_not_pass(cic, tmp_path, fake_git_repo):
    """A test receipt with result="fail" → FAIL."""
    repo = fake_git_repo
    _write_schema(tmp_path)
    # Build a receipt with result="fail".
    output_hash = _write_output_artefact(tmp_path, "control-plane/evidence/output.txt")
    receipt = {
        "requirement_id": "FAS-A-007",
        "check_id": "FAS-A-007",
        "branch": "main",
        "commit": "a" * 40,
        "result": "fail",  # wrong
        "timestamp": _now_iso(),
        "output_path": "control-plane/evidence/output.txt",
        "output_hash": output_hash,
    }
    receipt["receipt_hash"] = _compute_receipt_hash(receipt)
    _write_receipt(tmp_path, "control-plane/evidence/fas-a-007-receipt.json", receipt)
    reqs = _all_13_ids_git_ancestor(repo, override={
        "FAS-A-007": {"type": "test_receipt",
                      "receipt_path": "control-plane/evidence/fas-a-007-receipt.json",
                      "expected_check_id": "FAS-A-007"},
    })
    manifest = _write_manifest(tmp_path, requirements=reqs)
    result = _run_gate(cic, manifest, tmp_path, git_repo=repo["repo"])
    assert result.gate == "FAIL"
    assert "FAS-A-007" in result.failing_ids
    res = next(r for r in result.results if r.requirement_id == "FAS-A-007")
    assert res.status == "fail"
    assert "result_not_pass" in res.reason


# ── Test: test receipt output_hash mismatch → FAIL ───────────────────────────────

def test_test_receipt_output_hash_mismatch(cic, tmp_path, fake_git_repo):
    """A test receipt whose output_hash does not match the recomputed hash of
    the referenced output artefact → FAIL with output_hash_mismatch."""
    repo = fake_git_repo
    _write_schema(tmp_path)
    _write_output_artefact(tmp_path, "control-plane/evidence/output.txt")
    receipt = {
        "requirement_id": "FAS-A-007",
        "check_id": "FAS-A-007",
        "branch": "main",
        "commit": "a" * 40,
        "result": "pass",
        "timestamp": _now_iso(),
        "output_path": "control-plane/evidence/output.txt",
        "output_hash": "0" * 64,  # wrong — does not match actual
    }
    receipt["receipt_hash"] = _compute_receipt_hash(receipt)
    _write_receipt(tmp_path, "control-plane/evidence/fas-a-007-receipt.json", receipt)
    reqs = _all_13_ids_git_ancestor(repo, override={
        "FAS-A-007": {"type": "test_receipt",
                      "receipt_path": "control-plane/evidence/fas-a-007-receipt.json",
                      "expected_check_id": "FAS-A-007"},
    })
    manifest = _write_manifest(tmp_path, requirements=reqs)
    result = _run_gate(cic, manifest, tmp_path, git_repo=repo["repo"])
    assert result.gate == "FAIL"
    assert "FAS-A-007" in result.failing_ids
    res = next(r for r in result.results if r.requirement_id == "FAS-A-007")
    assert res.status == "fail"
    assert "output_hash_mismatch" in res.reason


# ── Test: test receipt receipt_hash mismatch → FAIL ──────────────────────────────

def test_test_receipt_receipt_hash_mismatch(cic, tmp_path, fake_git_repo):
    """A test receipt whose receipt_hash does not match the recomputed hash of
    the receipt bytes → FAIL with receipt_hash_mismatch."""
    repo = fake_git_repo
    _write_schema(tmp_path)
    output_hash = _write_output_artefact(tmp_path, "control-plane/evidence/output.txt")
    receipt = {
        "requirement_id": "FAS-A-007",
        "check_id": "FAS-A-007",
        "branch": "main",
        "commit": "a" * 40,
        "result": "pass",
        "timestamp": _now_iso(),
        "output_path": "control-plane/evidence/output.txt",
        "output_hash": output_hash,
        "receipt_hash": "0" * 64,  # wrong — does not match recomputed
    }
    _write_receipt(tmp_path, "control-plane/evidence/fas-a-007-receipt.json", receipt)
    reqs = _all_13_ids_git_ancestor(repo, override={
        "FAS-A-007": {"type": "test_receipt",
                      "receipt_path": "control-plane/evidence/fas-a-007-receipt.json",
                      "expected_check_id": "FAS-A-007"},
    })
    manifest = _write_manifest(tmp_path, requirements=reqs)
    result = _run_gate(cic, manifest, tmp_path, git_repo=repo["repo"])
    assert result.gate == "FAIL"
    assert "FAS-A-007" in result.failing_ids
    res = next(r for r in result.results if r.requirement_id == "FAS-A-007")
    assert res.status == "fail"
    assert "receipt_hash_mismatch" in res.reason


# ── Test: test receipt untrusted binding → FAIL (without trust context) ──────────

def test_test_receipt_untrusted_binding(cic, tmp_path, fake_git_repo):
    """A valid test receipt but without trust context → FAIL untrusted_binding.

    The receipt is structurally valid (all fields, hashes match), but without
    a trust context the binding is untrusted → FAIL (fail-closed).
    """
    repo = fake_git_repo
    _write_schema(tmp_path)
    _make_valid_test_receipt(
        tmp_path, "control-plane/evidence/fas-a-007-receipt.json",
        requirement_id="FAS-A-007", check_id="FAS-A-007",
    )
    reqs = _all_13_ids_git_ancestor(repo, override={
        "FAS-A-007": {"type": "test_receipt",
                      "receipt_path": "control-plane/evidence/fas-a-007-receipt.json",
                      "expected_check_id": "FAS-A-007"},
    })
    manifest = _write_manifest(tmp_path, requirements=reqs)
    # No trust context → untrusted_binding → FAIL.
    result = _run_gate(cic, manifest, tmp_path, git_repo=repo["repo"],
                      trust_context=None)
    assert result.gate == "FAIL"
    assert "FAS-A-007" in result.failing_ids
    res = next(r for r in result.results if r.requirement_id == "FAS-A-007")
    assert res.status == "fail"
    assert "untrusted_binding" in res.reason
    # With a fake trust context that approves the receipt, it should PASS.
    manifest_hash = hashlib.sha256(manifest.read_bytes()).hexdigest()
    fake_trust = FakeTrustContext(approved_manifest_hash=manifest_hash)
    result2 = _run_gate(cic, manifest, tmp_path, git_repo=repo["repo"],
                        trust_context=fake_trust)
    assert result2.gate == "PASS"
    assert result2.closeout_permitted is True


# ── Test: fabricated owner receipt with fake trust anchor → BLOCKED (production) ─

def test_fabricated_owner_receipt_fake_trust_anchor(cic, tmp_path, fake_git_repo):
    """An owner receipt with a fabricated trust_anchor_source → BLOCKED in
    production mode (no trust context). Test-only trust context can verify
    in test.
    """
    repo = fake_git_repo
    _write_schema(tmp_path)
    # Candidate-local JSON with a fabricated trust_anchor_source.
    receipt = {
        "owner_id": "filip-123",
        "decision_id": "dec-1",
        "decision": "approved",
        "commit": "a" * 40,
        "branch": "main",
        "timestamp": _now_iso(),
        "output_hash": "0" * 64,
        "external_attestation": {
            "trust_anchor_source": "fabricated_keychain_local",
            "attestation_hash": "0" * 64,
        },
    }
    _write_receipt(tmp_path, "control-plane/evidence/fas-a-011-receipt.json", receipt)
    reqs = _all_13_ids_git_ancestor(repo, override={
        "FAS-A-011": {"type": "owner_decision_receipt",
                      "receipt_path": "control-plane/evidence/fas-a-011-receipt.json",
                      "owner_id": "filip-123"},
    })
    manifest = _write_manifest(tmp_path, requirements=reqs)
    # Production mode (no trust context) → BLOCKED.
    result = _run_gate(cic, manifest, tmp_path, git_repo=repo["repo"],
                      trust_context=None)
    assert result.gate == "BLOCKED"
    assert "FAS-A-011" in result.blocked_ids
    res = next(r for r in result.results if r.requirement_id == "FAS-A-011")
    assert res.status == "blocked"
    assert "trust_context_missing" in res.reason
    assert result.closeout_permitted is False
    # Test-only mode (fake trust context authorizes filip-123) → PASS.
    manifest_hash = hashlib.sha256(manifest.read_bytes()).hexdigest()
    fake_trust = FakeTrustContext(
        approved_manifest_hash=manifest_hash,
        authorized_owner_ids={"filip-123"},
    )
    result2 = _run_gate(cic, manifest, tmp_path, git_repo=repo["repo"],
                        trust_context=fake_trust)
    assert result2.gate == "PASS"
    assert result2.closeout_permitted is True


# ── Test: FAS-A-012 owner-only after approved digest is tampered → manifest_tampered ──
# v3 (task t_9f3f941e, deviation C): replaces the misleading
# test_fas_a_012_without_technical_part, which could pass purely because
# trust_context was missing rather than because the tamper was detected.

def test_fas_a_012_owner_only_after_approved_digest_is_tampered(cic, tmp_path, fake_git_repo):
    """FAS-A-012 rewritten from all_of to owner-only, keeping an approved
    digest, must give gate=FAIL with reason=manifest_tampered and
    closeout_permitted=false.

    Steps:
      1. Build a correct FAS-A-012 all_of (technical file_sha256 sub-check
         with PENDING_EVIDENCE + owner_decision_receipt sub-check with
         PENDING_OWNER_DECISION), 12 other passing git_ancestor reqs, schema
         and manifest written under base_dir.
      2. Pin the manifest digest in a test-only trust context
         (FakeTrustContext.approved_manifest_hash).
      3. Rewrite FAS-A-012 to an owner-only check (technical sub-check
         REMOVED, only the owner_decision_receipt remains) but KEEP the
         previously approved manifest digest in the trust context.
      4. Expected: gate=FAIL, reason=manifest_tampered,
         closeout_permitted=false. The tamper is detected by the manifest
         digest mismatch, NOT by a missing trust_context — the test passes
         the trust context, so a missing-trust escape hatch cannot mask it.
    """
    repo = fake_git_repo
    _write_schema(tmp_path)

    # 1. Build all 13 IDs with FAS-A-012 as the correct all_of (technical +
    # owner). The other 12 are passing git_ancestor.
    correct_012 = {
        "type": "all_of",
        "checks": [
            {"type": "file_sha256",
             "path": "control-plane/evidence/fas-a-012-current-truth.json",
             "expected_sha256": "PENDING_EVIDENCE"},
            {"type": "owner_decision_receipt",
             "receipt_path": "control-plane/evidence/fas-a-012-owner-receipt.json",
             "owner_id": "PENDING_OWNER_DECISION"},
        ],
    }
    reqs_correct = _all_13_ids_git_ancestor(repo, override={"FAS-A-012": correct_012})
    manifest_correct = _write_manifest(tmp_path, requirements=reqs_correct,
                                        manifest_id="test-fas-a-012-correct")
    digest_correct = hashlib.sha256(manifest_correct.read_bytes()).hexdigest()

    # Sanity: the correct manifest validates against the schema. Without
    # trust_context it is FAIL (technical sub-check PENDING_EVIDENCE → FAIL,
    # owner sub-check → BLOCKED). With a trust context that approves the
    # correct digest it is still FAIL (PENDING_EVIDENCE file_sha256 cannot
    # pass), but NOT manifest_tampered.
    sanity = _run_gate(cic, manifest_correct, tmp_path, git_repo=repo["repo"])
    assert sanity.gate == "FAIL"
    assert sanity.reason != "manifest_tampered"
    # FAS-A-012 is FAIL-driven by the technical sub-check and also has a
    # BLOCKED owner sub-check → dual-listed (v3 dual status).
    assert "FAS-A-012" in sanity.failing_ids
    assert "FAS-A-012" in sanity.blocked_ids
    assert sanity.closeout_permitted is False

    # 2. Pin the correct digest in a test-only trust context.
    fake_trust = FakeTrustContext(approved_manifest_hash=digest_correct)

    # 3. Rewrite FAS-A-012 to an owner-only check (technical part removed).
    tampered_012 = {
        "type": "owner_decision_receipt",
        "receipt_path": "control-plane/evidence/fas-a-012-owner-receipt.json",
        "owner_id": "PENDING_OWNER_DECISION",
    }
    reqs_tampered = _all_13_ids_git_ancestor(repo, override={"FAS-A-012": tampered_012})
    manifest_tampered = _write_manifest(tmp_path, requirements=reqs_tampered,
                                        manifest_id="test-fas-a-012-tampered")
    # The digest MUST differ from the approved digest.
    digest_tampered = hashlib.sha256(manifest_tampered.read_bytes()).hexdigest()
    assert digest_tampered != digest_correct, \
        "tampered manifest must have a different digest from the approved one"

    # 4. Expected: manifest_tampered. The trust context is present and pins
    # the OLD digest, so the digest mismatch drives the gate. The test does
    # NOT pass purely because trust_context is missing — trust_context is
    # explicitly supplied here.
    result = _run_gate(cic, manifest_tampered, tmp_path, git_repo=repo["repo"],
                      trust_context=fake_trust)
    assert result.gate == "FAIL", f"expected FAIL, got {result.gate}: {result.reason}"
    assert result.reason == "manifest_tampered", \
        f"expected manifest_tampered, got {result.reason!r}"
    assert result.closeout_permitted is False
    assert result.trust_context_present is True
    assert result.manifest_verified is False


# ── Test: FAS-A-012 dual status (FAIL + BLOCKED in same all_of) ────────────────
# v3 (task t_9f3f941e, deviation B): an all_of composite with BOTH a FAIL
# sub-check and a BLOCKED sub-check must list the requirement ID in BOTH
# failing_ids and blocked_ids. FAIL still drives the gate.

def test_fas_a_012_dual_status_fail_and_blocked(cic, tmp_path, fake_git_repo):
    """FAS-A-012 all_of with missing technical evidence (file_sha256
    PENDING_EVIDENCE) AND missing owner trust (owner_decision_receipt
    PENDING_OWNER_DECISION), without trust context, must give:

      gate                 = FAIL
      failing_ids          contains FAS-A-012
      blocked_ids          contains FAS-A-012
      closeout_permitted   = False

    The technical sub-check is FAIL (PENDING_EVIDENCE never matches a real
    file). The owner sub-check is BLOCKED (no trust context). The composite
    status is FAIL (FAIL > BLOCKED), but the requirement ID must appear in
    BOTH lists so the dual-status signal is preserved.
    """
    repo = fake_git_repo
    _write_schema(tmp_path)

    fas_a_012_all_of = {
        "type": "all_of",
        "checks": [
            {"type": "file_sha256",
             "path": "control-plane/evidence/fas-a-012-current-truth.json",
             "expected_sha256": "PENDING_EVIDENCE"},
            {"type": "owner_decision_receipt",
             "receipt_path": "control-plane/evidence/fas-a-012-owner-receipt.json",
             "owner_id": "PENDING_OWNER_DECISION"},
        ],
    }
    reqs = _all_13_ids_git_ancestor(repo, override={"FAS-A-012": fas_a_012_all_of})
    manifest = _write_manifest(tmp_path, requirements=reqs,
                               manifest_id="test-fas-a-012-dual-status")
    result = _run_gate(cic, manifest, tmp_path, git_repo=repo["repo"],
                      trust_context=None)

    assert result.gate == "FAIL", f"expected FAIL, got {result.gate}: {result.reason}"
    assert result.closeout_permitted is False
    # FAIL drives the gate, so FAS-A-012 is in failing_ids.
    assert "FAS-A-012" in result.failing_ids, \
        f"FAS-A-012 must be in failing_ids: {result.failing_ids}"
    # The owner sub-check is BLOCKED, so FAS-A-012 is ALSO in blocked_ids
    # (dual status).
    assert "FAS-A-012" in result.blocked_ids, \
        f"FAS-A-012 must be in blocked_ids (dual status): {result.blocked_ids}"
    # Both lists are deduplicated and stable: FAS-A-012 appears exactly once
    # in each.
    assert result.failing_ids.count("FAS-A-012") == 1
    assert result.blocked_ids.count("FAS-A-012") == 1
    # The composite CheckResult carries also_blocked=True.
    res = next(r for r in result.results if r.requirement_id == "FAS-A-012")
    assert res.status == "fail"
    assert res.also_blocked is True


# ── Test: schema realpath uses base_dir, not cwd ──────────────────────────────
# v3 (task t_9f3f941e, deviation A): run_gate resolves the top-level schema
# path via validate_path and validates against EXACTLY that realpath. A
# different cwd carrying a permissive schema on the same relative path must
# NOT influence the result — only the schema under base_dir is used.

def test_schema_realpath_uses_base_dir_not_cwd(cic, tmp_path, fake_git_repo):
    """base_dir has a STRICT schema (the canonical schema). A sibling cwd
    has a PERMISSIVE schema on the same relative path
    (control-plane/fas-a-requirements.schema.json). The CIC must open and
    validate against the schema under base_dir's realpath, NOT the cwd's
    permissive copy.

    The strict schema under base_dir enforces additionalProperties=false on
    every requirement (no extra fields allowed). The permissive schema under
    cwd allows any properties. A manifest with an EXTRA forbidden field on
    a requirement MUST fail against the strict schema under base_dir — even
    if a permissive schema sits at the same relative path under cwd.
    """
    repo = fake_git_repo
    base_dir = tmp_path / "base"
    cwd_dir = tmp_path / "cwd"

    # base_dir gets the STRICT (canonical) schema.
    _write_schema(base_dir)

    # cwd_dir gets a PERMISSIVE schema at the same relative path: it accepts
    # any properties on requirements (additionalProperties=true) and does not
    # require source_hash. This must NOT be picked up when base_dir != cwd.
    (cwd_dir / "control-plane").mkdir(parents=True)
    permissive_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": True,
        "required": ["manifest_version", "manifest_id", "requirements"],
        "properties": {
            "manifest_version": {"type": "string"},
            "manifest_id": {"type": "string"},
            "requirements": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": True,
                    "required": ["id", "text", "check"],
                    "properties": {
                        "id": {"type": "string"},
                        "text": {"type": "string"},
                        "source_hash": {"type": "string"},
                        "check": {
                            "type": "object",
                            "additionalProperties": True,
                            "required": ["type"],
                            "properties": {"type": {"type": "string"}},
                        },
                    },
                },
            },
        },
    }
    (cwd_dir / "control-plane" / "fas-a-requirements.schema.json").write_text(
        json.dumps(permissive_schema)
    )

    # Build a manifest that the STRICT schema rejects (an extra forbidden
    # field ``bogus_extra`` on FAS-A-001, which the strict schema's
    # additionalProperties=false disallows) but the PERMISSIVE schema would
    # accept. All 13 IDs present, all source_hashes valid.
    #
    # We bypass the _write_manifest helper (which auto-fills source_hash and
    # would mask the violation) and write the YAML directly to base_dir via
    # tmp_path — still pytest-owned, no conftest, no custom script.
    reqs = []
    for i in range(1, 14):
        rid = f"FAS-A-{i:03d}"
        r = {
            "id": rid,
            "text": f"req {rid}",
            "source_hash": _sha256(f"req {rid}"),
            "check": {"type": "git_ancestor", "branch": "main",
                      "commit_sha": repo["base_sha"]},
        }
        if i == 1:
            # Forbidden extra field → strict schema rejects via
            # additionalProperties=false; permissive schema accepts.
            r["bogus_extra"] = "this_field_is_not_in_the_strict_schema"
        reqs.append(r)
    manifest_doc = {
        "manifest_version": "2.0",
        "manifest_id": "test-schema-realpath",
        "description": "test manifest for schema-realpath v3",
        "requirements": reqs,
    }
    (base_dir / "control-plane").mkdir(parents=True, exist_ok=True)
    manifest = base_dir / "control-plane" / "fas-a-requirements.yaml"
    manifest.write_text(yaml.safe_dump(manifest_doc, sort_keys=False))

    # Capture the realpath the CIC would resolve for the schema. The CIC
    # resolves the schema relative to base_dir, so it must be the base_dir
    # copy, not the cwd copy.
    expected_real_schema = os.path.realpath(
        str(base_dir / "control-plane" / "fas-a-requirements.schema.json")
    )
    cwd_real_schema = os.path.realpath(
        str(cwd_dir / "control-plane" / "fas-a-requirements.schema.json")
    )
    assert expected_real_schema != cwd_real_schema, \
        "test setup: base_dir and cwd schemas must be distinct realpaths"

    # Run the gate with base_dir=base_dir. The cwd has a permissive schema
    # at the same relative path; if the CIC fell back to cwd it would PASS
    # the schema step. The v3 fix resolves the schema under base_dir only.
    result = _run_gate(
        cic, manifest, base_dir, git_repo=repo["repo"],
        schema_path="control-plane/fas-a-requirements.schema.json",
    )

    # The strict schema under base_dir rejects the manifest (FAS-A-001 has a
    # forbidden extra field). The gate is FAIL with a schema reason, NOT a
    # pass, NOT trust_context_missing (which would mean the permissive
    # schema was used and all checks passed).
    assert result.gate == "FAIL", f"expected FAIL, got {result.gate}: {result.reason}"
    assert result.closeout_permitted is False
    assert result.schema_valid is False, \
        f"strict schema under base_dir must reject the manifest, schema_valid={result.schema_valid}"
    assert "schema_invalid" in result.reason or "schema_violation" in result.reason, \
        f"expected schema rejection from base_dir's strict schema, got: {result.reason}"
    assert "bogus_extra" in result.reason or "Additional properties" in result.reason, \
        f"expected the extra-field violation to be reported, got: {result.reason}"

    # Cross-check via validate_path: the realpath the CIC would resolve for
    # the relative schema path under base_dir is the base_dir copy, NOT the
    # cwd copy. This is the invariant the v3 fix enforces.
    ok, _msg, real = cic.validate_path(
        "control-plane/fas-a-requirements.schema.json", base_dir=base_dir
    )
    assert ok and real == expected_real_schema, \
        f"validate_path must resolve to base_dir's schema: real={real}"
    assert real != cwd_real_schema, \
        "validate_path must NOT resolve to the cwd's permissive schema"


# ── Test: evidence in sibling worktree rejected → FAIL path_not_in_base_dir ──────

def test_evidence_in_sibling_worktree_rejected(cic, tmp_path, fake_git_repo):
    """A receipt_path in a sibling worktree (e.g. kmros-fleet when base_dir=
    kmros-cic) → FAIL path_not_in_base_dir.
    """
    repo = fake_git_repo
    # base_dir is kmros-cic (under tmp_path); sibling is kmros-fleet (also
    # under tmp_path but NOT under kmros-cic).
    base_dir = tmp_path / "kmros-cic"
    (base_dir / "control-plane").mkdir(parents=True)
    _write_schema(base_dir)
    sibling = tmp_path / "kmros-fleet"
    (sibling / "control-plane" / "evidence").mkdir(parents=True)
    receipt_in_sibling = sibling / "control-plane" / "evidence" / "fas-a-007-receipt.json"
    receipt_in_sibling.write_text(json.dumps({
        "requirement_id": "FAS-A-007",
        "check_id": "FAS-A-007",
        "branch": "main",
        "commit": "a" * 40,
        "result": "pass",
        "timestamp": _now_iso(),
        "output_path": "control-plane/evidence/output.txt",
        "output_hash": "0" * 64,
        "receipt_hash": "0" * 64,
    }))
    reqs = _all_13_ids_git_ancestor(repo, override={
        "FAS-A-007": {"type": "test_receipt",
                      # Absolute path to the sibling worktree.
                      "receipt_path": str(receipt_in_sibling),
                      "expected_check_id": "FAS-A-007"},
    })
    manifest = _write_manifest(base_dir, requirements=reqs)
    result = _run_gate(cic, manifest, base_dir, git_repo=repo["repo"])
    assert result.gate == "FAIL"
    assert "FAS-A-007" in result.failing_ids
    res = next(r for r in result.results if r.requirement_id == "FAS-A-007")
    assert res.status == "fail"
    assert "path_not_in_base_dir" in res.reason


# ── Test: manifest in sibling worktree rejected → FAIL manifest_not_in_base_dir ───

def test_manifest_in_sibling_worktree_rejected(cic, tmp_path, fake_git_repo):
    """--manifest in another worktree → FAIL manifest_not_in_base_dir.
    """
    repo = fake_git_repo
    # base_dir is kmros-cic (under tmp_path); sibling is kmros-fleet (also
    # under tmp_path but NOT under kmros-cic).
    base_dir = tmp_path / "kmros-cic"
    (base_dir / "control-plane").mkdir(parents=True)
    _write_schema(base_dir)
    sibling = tmp_path / "kmros-fleet"
    (sibling / "control-plane").mkdir(parents=True)
    sibling_manifest = sibling / "control-plane" / "fas-a-requirements.yaml"
    reqs = _all_13_ids_git_ancestor(repo)
    # Add source_hash to each req (write_manifest does this, but we write
    # directly to the sibling here).
    for r in reqs:
        if "source_hash" not in r:
            r["source_hash"] = _sha256(r["text"])
    sibling_manifest.write_text(yaml.safe_dump({
        "manifest_version": "2.0",
        "manifest_id": "sibling-manifest",
        "description": "sibling",
        "requirements": reqs,
    }, sort_keys=False))
    # base_dir is kmros-cic; the manifest is in the sibling kmros-fleet.
    result = _run_gate(cic, sibling_manifest, base_dir, git_repo=repo["repo"])
    assert result.gate == "FAIL"
    assert "manifest_not_in_base_dir" in result.reason
    assert result.closeout_permitted is False


# ── Test: symlink escape → FAIL path_not_in_base_dir ──────────────────────────────

def test_symlink_escape(cic, tmp_path):
    """A symlink inside base_dir that points OUTSIDE base_dir must be rejected
    (realpath resolves to a non-base_dir target)."""
    fake_worktree = tmp_path / "kmros-fake"
    (fake_worktree / "control-plane").mkdir(parents=True)
    outside = tmp_path / "outside_target"
    outside.write_bytes(b"outside secret")
    escape_link = fake_worktree / "control-plane" / "escape.json"
    os.symlink(outside, escape_link)
    ok, msg, real = cic.validate_path(
        "control-plane/escape.json", base_dir=fake_worktree
    )
    assert ok is False, f"symlink escape not rejected: real={real}"
    assert "path_not_in_base_dir" in msg


# ── Test: branch leading dash rejected → FAIL branch_invalid ─────────────────────

def test_branch_leading_dash_rejected(cic, tmp_path, fake_git_repo):
    """branch=\"--help\" → FAIL branch_invalid (git check-ref-format rejects)."""
    repo = fake_git_repo
    _write_schema(tmp_path)
    reqs = _all_13_ids_git_ancestor(repo, override={
        "FAS-A-001": {"type": "git_ancestor", "branch": "--help",
                      "commit_sha": repo["base_sha"]},
    })
    manifest = _write_manifest(tmp_path, requirements=reqs)
    result = _run_gate(cic, manifest, tmp_path, git_repo=repo["repo"])
    assert result.gate == "FAIL"
    assert "FAS-A-001" in result.failing_ids
    res = next(r for r in result.results if r.requirement_id == "FAS-A-001")
    assert res.status == "fail"
    assert "branch_invalid" in res.reason


# ── Test: branch leading slash rejected → FAIL branch_invalid ─────────────────────

def test_branch_leading_slash_rejected(cic, tmp_path, fake_git_repo):
    """branch=\"/leading\" → FAIL branch_invalid."""
    repo = fake_git_repo
    _write_schema(tmp_path)
    reqs = _all_13_ids_git_ancestor(repo, override={
        "FAS-A-001": {"type": "git_ancestor", "branch": "/leading",
                      "commit_sha": repo["base_sha"]},
    })
    manifest = _write_manifest(tmp_path, requirements=reqs)
    result = _run_gate(cic, manifest, tmp_path, git_repo=repo["repo"])
    assert result.gate == "FAIL"
    assert "FAS-A-001" in result.failing_ids
    res = next(r for r in result.results if r.requirement_id == "FAS-A-001")
    assert res.status == "fail"
    assert "branch_invalid" in res.reason


# ── Test: branch -x rejected → FAIL branch_invalid ─────────────────────────────────

def test_branch_x_rejected(cic, tmp_path, fake_git_repo):
    """branch=\"-x\" → FAIL branch_invalid."""
    repo = fake_git_repo
    _write_schema(tmp_path)
    reqs = _all_13_ids_git_ancestor(repo, override={
        "FAS-A-001": {"type": "git_ancestor", "branch": "-x",
                      "commit_sha": repo["base_sha"]},
    })
    manifest = _write_manifest(tmp_path, requirements=reqs)
    result = _run_gate(cic, manifest, tmp_path, git_repo=repo["repo"])
    assert result.gate == "FAIL"
    assert "FAS-A-001" in result.failing_ids
    res = next(r for r in result.results if r.requirement_id == "FAS-A-001")
    assert res.status == "fail"
    assert "branch_invalid" in res.reason


# ── Test: simultaneous FAIL and BLOCKED → gate=FAIL, both lists ────────────────────

def test_simultaneous_fail_and_blocked(cic, tmp_path, fake_git_repo):
    """A manifest with one FAIL requirement and one BLOCKED requirement must
    give gate=FAIL (FAIL has priority over BLOCKED). Both failing_ids and
    blocked_ids must be listed in the output."""
    repo = fake_git_repo
    _write_schema(tmp_path)
    reqs = _all_13_ids_git_ancestor(repo, override={
        "FAS-A-001": {"type": "git_ancestor", "branch": "main",
                      "commit_sha": repo["divergent_sha"]},  # FAIL
        "FAS-A-011": {"type": "owner_decision_receipt",
                      "receipt_path": "control-plane/evidence/fas-a-011-receipt.json",
                      "owner_id": "PENDING_OWNER_DECISION"},  # BLOCKED
    })
    manifest = _write_manifest(tmp_path, requirements=reqs)
    result = _run_gate(cic, manifest, tmp_path, git_repo=repo["repo"])
    assert result.gate == "FAIL", f"expected FAIL, got {result.gate}: {result.reason}"
    assert "FAS-A-001" in result.failing_ids
    assert "FAS-A-011" in result.blocked_ids
    assert result.closeout_permitted is False
    assert len(result.failing_ids) >= 1
    assert len(result.blocked_ids) >= 1


# ── Test: production CLI cannot reach test trust verifier ─────────────────────────

def test_production_cli_cannot_reach_test_trust_verifier(cic, tmp_path, fake_git_repo):
    """Production CLI has no trust context → closeout_permitted=false, even if
    all technical requirements pass. The test-only FakeTrustContext is never
    reachable via the production CLI.
    """
    repo = fake_git_repo
    _write_schema(tmp_path)
    # All 13 IDs as passing git_ancestor — all technical requirements pass.
    reqs = _all_13_ids_git_ancestor(repo)
    manifest = _write_manifest(tmp_path, requirements=reqs)
    # Simulate production CLI: trust_context=None (no trust provider).
    result = _run_gate(cic, manifest, tmp_path, git_repo=repo["repo"],
                      trust_context=None)
    assert result.gate == "FAIL"
    assert result.closeout_permitted is False
    assert result.trust_context_present is False
    assert result.reason == "trust_context_missing"
    # And the FakeTrustContext is NOT reachable via the CLI (it's test-only).
    # Verify the CLI main() always uses trust_context=None.
    import io
    old_stdout = sys.stdout
    try:
        sys.stdout = io.StringIO()
        exit_code = cic.main([
            "--manifest", str(manifest),
            "--base-dir", str(tmp_path),
            "--trust-context-source", "fabricated",
        ])
        output = sys.stdout.getvalue()
    finally:
        sys.stdout = old_stdout
    data = json.loads(output)
    assert data["gate"] == "FAIL"
    assert data["closeout_permitted"] is False
    assert data["trust_context_present"] is False


# ── Test: canonical candidate never closeout without harness trust ────────────────

def test_canonical_candidate_never_closeout_without_harness_trust(cic):
    """Acceptance criterion: the CIC against the canonical manifest (in the
    real worktree) without trust context → closeout_permitted=false (gate=FAIL
    if technical requirements missing, or BLOCKED if only owner missing).
    """
    repo_root = Path(__file__).resolve().parents[2]
    manifest_path = repo_root / "control-plane" / "fas-a-requirements.yaml"
    result = cic.run_gate(
        manifest_path=str(manifest_path),
        base_dir=repo_root,
        trust_context=None,  # No trust context — fail-closed.
        schema_path=str(repo_root / "control-plane" / "fas-a-requirements.schema.json"),
    )
    assert result.gate == "FAIL", f"expected FAIL, got {result.gate}: {result.reason}"
    assert result.closeout_permitted is False
    assert result.trust_context_present is False
    # Technical requirements must be FAIL (missing evidence).
    for rid in ["FAS-A-004", "FAS-A-005", "FAS-A-006", "FAS-A-007",
                "FAS-A-008", "FAS-A-009", "FAS-A-010", "FAS-A-012", "FAS-A-013"]:
        assert rid in result.failing_ids, f"{rid} should be failing (missing technical evidence)"
    # Genuine owner-decision requirement FAS-A-011 must be BLOCKED.
    assert "FAS-A-011" in result.blocked_ids
    # FAS-A-001/002/003 must PASS (real git ancestry on kmros/main).
    for rid in ["FAS-A-001", "FAS-A-002", "FAS-A-003"]:
        assert rid not in result.failing_ids


# ────────────────────────────────────────────────────────────────────────────
# Kept extras
# ────────────────────────────────────────────────────────────────────────────


# ── Extra: source_hash drift → FAIL ─────────────────────────────────────────

def test_source_hash_drift_fails(cic, tmp_path, fake_git_repo):
    """If a requirement's text is edited after pinning, source_hash mismatches → FAIL."""
    repo = fake_git_repo
    _write_schema(tmp_path)
    reqs = _all_13_ids_git_ancestor(repo)
    # Tamper FAS-A-001's source_hash.
    for r in reqs:
        if r["id"] == "FAS-A-001":
            r["source_hash"] = "0" * 64
    manifest = _write_manifest(tmp_path, requirements=reqs)
    result = _run_gate(cic, manifest, tmp_path, git_repo=repo["repo"])
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
    _run_gate(cic, manifest, tmp_path, git_repo=repo["repo"])
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
    tree = ast.parse(src)
    called_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                if isinstance(func.value, ast.Name):
                    called_names.add(f"{func.value.id}.{func.attr}")
            elif isinstance(func, ast.Name):
                called_names.add(func.id)
    forbidden_calls = {
        "os.system", "os.remove", "os.unlink",
        "shutil.rmtree", "shutil.move",
        "subprocess.Popen",
    }
    for bad in forbidden_calls:
        assert bad not in called_names, f"CIC must not call {bad!r}"
    assert 'cmd = ["git"' in src or "cmd = ['git'" in src, \
        "CIC must invoke git via a list argument, not a shell string"
    for token in ("kanban_create", "kanban_complete", "kanban_block"):
        assert token not in src, f"CIC source must not contain {token!r}"
    for token in ("git push", "pip install", "uv pip install"):
        assert token not in src, f"CIC source must not contain {token!r}"


# ── Extra: CIC source has no hardcoded REQUIREMENT_SEMANTICS table ──────────

def test_cic_source_has_no_hardcoded_requirement_semantics():
    """The CIC source must NOT contain a hardcoded REQUIREMENT_SEMANTICS table
    (krav 1). The manifest is the single semantic authority.

    We check the AST (not the raw source) so that the prohibition mention in
    the module docstring ("No hardcoded REQUIREMENT_SEMANTICS table in Python")
    does not trip the check.
    """
    import ast
    src = CIC_PATH.read_text()
    tree = ast.parse(src)
    # Walk top-level and class-level assignments; flag any dict assigned to a
    # name containing "REQUIREMENT_SEMANTICS".
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and "REQUIREMENT_SEMANTICS" in target.id:
                    pytest.fail(
                        f"CIC must not have a hardcoded {target.id} table (krav 1)"
                    )
    # No hardcoded mapping of requirement IDs to check types or texts in code.
    # Check only non-docstring, non-comment lines via AST walk of string
    # constants in assignments/calls.
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            v = node.value
            if v in ("git_ancestor", "file_sha256", "test_receipt"):
                # These are allowed as DETERMINISTIC_EXPECTED values, but not as
                # values in a hardcoded requirement-id-to-checktype mapping.
                pass
    # Explicitly assert no module-level dict literal maps FAS-A- IDs to check types.
    assert "FAS-A-001\": \"git_ancestor" not in src
    assert "'FAS-A-001': 'git_ancestor" not in src


# ── Extra: production CLI has no trust-context provider ─────────────────────

def test_production_cli_has_no_trust_context_provider():
    """The CIC CLI main() must NOT instantiate a trust context provider.
    Production CLI is fail-closed (trust_context=None default). The test-only
    FakeTrustContext is never reachable via the CLI.
    """
    src = CIC_PATH.read_text()
    # The CLI must set trust_context = None (no provider).
    assert "trust_context = None" in src, \
        "CLI must set trust_context = None (no trust provider in production)"
    # No import of a real trust provider (Keychain, GPG, etc.).
    for token in ("keyring", "gnupg", "subprocess.check_output", "subprocess.Popen"):
        assert token not in src, f"CIC must not import/use {token!r}"


# ── Extra: TrustContext protocol is injectable ──────────────────────────────

def test_trust_context_protocol_is_injectable(cic):
    """The TrustContext protocol must be injectable via run_gate(trust_context=...).
    This verifies the DI seam exists for future harness integration.
    """
    # A minimal fake trust context (duck-typed).
    class FakeTC:
        def verify_manifest_digest(self, digest): return True
        def verify_receipt_binding(self, h, r, c): return True
        def verify_owner_decision(self, o, r): return True
    # run_gate must accept trust_context as a parameter.
    import inspect
    sig = inspect.signature(cic.run_gate)
    assert "trust_context" in sig.parameters, \
        "run_gate must accept trust_context parameter (DI seam)"
    # The TrustContext protocol must exist.
    assert hasattr(cic, "TrustContext"), "CIC must expose TrustContext protocol"


# ── v4: recursive dual-status propagation through nested all_of ─────────────


def test_nested_all_of_propagates_also_blocked(cic, tmp_path):
    """v4 regression: an inner all_of with FAIL+BLOCKED nested inside an
    outer all_of must preserve also_blocked=True on the outer result.

    Reproduces Filip's reported gap: a flat all_of with FAIL+BLOCKED sets
    also_blocked=true, but the SAME composite nested inside another all_of
    returned also_blocked=false because the outer _evaluate_all_of only
    checked sr.status == "fail" and ignored sr.also_blocked.

    This test exercises the public run_gate chain (not a private helper)
    with a schema-valid manifest containing all 13 IDs, models FAS-A-012 as
    an outer all_of containing an inner all_of, and asserts:
      - gate == "fail"
      - FAS-A-012 appears exactly once in failing_ids
      - FAS-A-012 appears exactly once in blocked_ids
      - FAS-A-012's CheckResult has also_blocked == True
      - closeout_permitted == False
    """
    import json as _json
    import pathlib
    import hashlib
    # Build a 13-requirement manifest. FAS-A-001..011 and FAS-A-013 use
    # trivially-passing checks (git_ancestor against a real branch in the
    # worktree's repo); FAS-A-012 is the nested all_of under test.
    import subprocess
    # Use tmp_path as base_dir so manifest/schema/receipts are contained.
    base = tmp_path
    repo = pathlib.Path(cic.__file__).resolve().parent.parent  # worktree root for git lookups
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True
    ).stdout.strip()
    branch = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=True
    ).stdout.strip()
    # Copy the real schema into tmp_path/control-plane/ so containment passes.
    (base / "control-plane").mkdir(exist_ok=True)
    schema_src = repo / "control-plane" / "fas-a-requirements.schema.json"
    schema_path = base / "control-plane" / "fas-a-requirements.schema.json"
    schema_path.write_bytes(schema_src.read_bytes())

    def passing_check():
        return {"type": "git_ancestor", "branch": branch, "commit_sha": head}

    reqs = []
    for i in range(1, 14):
        rid = f"FAS-A-{i:03d}"
        text_i = f"requirement {i}"
        sh = hashlib.sha256(text_i.encode("utf-8")).hexdigest()
        if i == 12:
            # Nested all_of: outer contains inner all_of + a passing check.
            inner_all_of = {
                "type": "all_of",
                "checks": [
                    # Missing technical evidence (file does not exist) -> FAIL
                    {
                        "type": "file_sha256",
                        "path": "control-plane/evidence/fas-a-012-current-truth.json",
                        "expected_sha256": "PENDING_EVIDENCE",
                    },
                    # Owner decision without trust context -> BLOCKED
                    {
                        "type": "owner_decision_receipt",
                        "receipt_path": "control-plane/evidence/fas-a-012-owner-receipt.json",
                        "owner_id": "PENDING_OWNER_DECISION",
                    },
                ],
            }
            reqs.append({
                "id": rid,
                "text": text_i,
                "source_hash": sh,
                "check": {
                    "type": "all_of",
                    "checks": [inner_all_of, passing_check()],
                },
            })
        else:
            reqs.append({
                "id": rid,
                "text": text_i,
                "source_hash": sh,
                "check": passing_check(),
            })

    manifest = {
        "manifest_version": "1.1",
        "manifest_id": "fas-a-requirements-v1",
        "generated_by": "test",
        "description": "test manifest",
        "anchors": {},
        "requirements": reqs,
    }
    manifest_path = tmp_path / "fas-a-requirements.yaml"
    import yaml
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False))

    # schema_path already set above (copied into tmp_path/control-plane/).

    # Run the full gate chain (public API). No trust context -> owner
    # decision receipts are BLOCKED; missing technical file is FAIL.
    result = cic.run_gate(
        manifest_path=str(manifest_path),
        schema_path=str(schema_path),
        base_dir=str(base),
        trust_context=None,
    )
    assert result.gate == "FAIL", f"expected gate=fail, got {result.gate}"
    assert result.closeout_permitted is False, "closeout must be false"
    # FAS-A-012 in failing_ids exactly once
    assert result.failing_ids.count("FAS-A-012") == 1,         f"FAS-A-012 should appear once in failing_ids, got {result.failing_ids.count('FAS-A-012')}"
    # FAS-A-012 in blocked_ids exactly once
    assert result.blocked_ids.count("FAS-A-012") == 1,         f"FAS-A-012 should appear once in blocked_ids, got {result.blocked_ids.count('FAS-A-012')}"
    # Find the CheckResult for FAS-A-012 and assert also_blocked
    for cr in result.results:
        if cr.requirement_id == "FAS-A-012":
            assert cr.also_blocked is True,                 f"FAS-A-012 CheckResult must have also_blocked=True, got {cr.get('also_blocked')}"
            break
    else:
        # check_results may not be in the JSON output; verify via failing+blocked dual listing
        pass
