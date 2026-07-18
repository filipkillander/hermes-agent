"""Regression + integrity tests for the Fas A completion-integrity-gate (CIC).

These tests are hermetic: they build temporary worktrees, manifests, and
receipts on disk and exercise ``scripts/completion_integrity_check.py``
through its public ``run_gate`` API. No real Kanban board, no network, no
mutations outside the per-test tmpdir.

Coverage of the 11 required cases (task t_294c700d, krav 5 + 6):

  1.  test_manifest_hash_mismatch        — manifest_hash manipulerat → FAIL
  2.  test_unknown_check_type            — check.type="evil_shell_command" → FAIL
  3.  test_path_escape                   — path="../../../etc/passwd" → FAIL path_not_allowlisted
  4.  test_shell_metacharacters          — args contain ";", "|", "$(", "`" → FAIL
  5.  test_receipt_from_wrong_commit     — test_receipt commit mismatch → FAIL
  6.  test_missing_owner_receipt         — owner_decision_receipt without concrete receipt → BLOCKED
  7.  test_deterministic_identical_result— same manifest + evidence, two runs, identical → PASS
  8.  test_self_referential_closeout_rejected — regression (krav 5): cards done, requirements missing → FAIL
  9.  test_valid_manifest_all_pass       — valid manifest, all checks pass → PASS
  10. test_blocked_does_not_allow_completion — gate=BLOCKED never returns closeout_permitted
  11. test_fail_lists_exact_requirement_ids — FAIL lists exact requirement IDs
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
from pathlib import Path

import pytest
import yaml

# ── Helpers to load the CIC module from the worktree ────────────────────────

CIC_PATH = Path(__file__).resolve().parents[2] / "scripts" / "completion_integrity_check.py"


def _load_cic():
    # Register in sys.modules BEFORE exec_module so that dataclass
    # ``__module__`` resolves correctly (avoids the
    # ``AttributeError: 'NoneType' object has no attribute '__dict__'``
    # seen when the module is loaded spec-only).
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
    """Create a real (tiny) git repo so the CIC's subprocess git calls work.

    The CIC reads GIT_REPO from its module. We monkeypatch that constant to
    point at this tmp repo, then build a couple of commits and branches to
    exercise ancestor / not-ancestor checks deterministically.
    """
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

    # A child commit on main (ancestor of main)
    (repo / "f.txt").write_text("child\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-q", "-m", "child"], check=True, env=env)
    child_sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                               capture_output=True, text=True, check=True).stdout.strip()

    # A divergent branch whose tip is NOT an ancestor of main
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


def _write_manifest(base_dir: Path, requirements: list, manifest_id: str = "test-manifest-v1",
                    extra: dict | None = None) -> Path:
    """Write a manifest YAML into ``base_dir/control-plane/`` and return its path.

    Each requirement is a dict already shaped for YAML. We add source_hash
    automatically from each requirement's ``text`` field if not present.
    """
    cp = base_dir / "control-plane"
    cp.mkdir(parents=True, exist_ok=True)
    path = cp / "fas-a-requirements.yaml"
    doc = {
        "manifest_version": "1.0",
        "manifest_id": manifest_id,
        "description": "test manifest",
    }
    if extra:
        doc.update(extra)
    # Ensure source_hash present
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
    """Write a receipt JSON at ``base_dir / rel_path``. Returns the full path."""
    full = base_dir / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(json.dumps(payload, sort_keys=True))
    return full


def _run_gate(cic, manifest_path: Path, base_dir: Path, expected_hash: str | None = None,
              git_repo: str | None = None, allow_extra_prefix: str | None = None):
    """Run the CIC, optionally monkeypatching GIT_REPO to ``git_repo``.

    ``allow_extra_prefix`` (if given) is appended to the CIC's path allowlist
    for the duration of this call, so tests that build fixtures under a
    pytest tmp_path (which is NOT under the production allowlist prefixes)
    can still exercise path-validated checks (file_sha256, schema_valid,
    test_receipt, owner_decision_receipt). The production allowlist in the
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
        return cic.run_gate(manifest_path=str(manifest_path), base_dir=base_dir,
                             expected_manifest_hash=expected_hash)
    finally:
        for attr, original in reversed(patches):
            setattr(cic, attr, original)


# ── Test 9: valid manifest, all pass → PASS ─────────────────────────────────

def test_valid_manifest_all_pass(cic, tmp_path, fake_git_repo):
    """A manifest where every evidence_check returns expected → gate=PASS."""
    repo = fake_git_repo
    schema = _write_schema(tmp_path)
    manifest = _write_manifest(tmp_path, requirements=[
        {
            "id": "FAS-A-001", "text": "Skapa isolerad kandidat från verifierad gemensam bas",
            "check": {"type": "git_ancestor", "branch": "main", "commit_sha": repo["base_sha"]},
            "expected": "exit_0",
        },
        {
            "id": "FAS-A-003", "text": "Håll worker-isolation och stop-semantics utanför",
            "check": {"type": "git_not_ancestor", "branch": "main",
                      "commit_sha": repo["divergent_sha"]},
            "expected": "exit_1",
        },
        {
            "id": "FAS-A-999", "text": "Schema valid check",
            "check": {"type": "schema_valid",
                      "yaml_path": "control-plane/fas-a-requirements.yaml",
                      "schema_path": "control-plane/fas-a-requirements.schema.json"},
            "expected": "valid",
        },
    ])
    # Schema-valid reads files under tmp_path, which must be allowlisted.
    result = _run_gate(cic, manifest, tmp_path, git_repo=repo["repo"],
                       allow_extra_prefix=str(tmp_path))
    assert result.gate == "PASS", f"expected PASS, got {result.gate}: {result.reason}"
    assert result.closeout_permitted is True
    assert result.failing_ids == []
    assert result.blocked_ids == []
    assert all(r.status == "pass" for r in result.results)


# ── Test 1: manifest hash mismatch → FAIL ───────────────────────────────────

def test_manifest_hash_mismatch(cic, tmp_path, fake_git_repo):
    """If --expected-manifest-hash does not match the recomputed hash → FAIL."""
    repo = fake_git_repo
    manifest = _write_manifest(tmp_path, requirements=[
        {
            "id": "FAS-A-001", "text": "Skapa isolerad kandidat från verifierad gemensam bas",
            "check": {"type": "git_ancestor", "branch": "main", "commit_sha": repo["base_sha"]},
            "expected": "exit_0",
        },
    ])
    wrong_hash = "0" * 64  # 64 hex chars, definitely not the real hash
    result = _run_gate(cic, manifest, tmp_path, expected_hash=wrong_hash,
                       git_repo=repo["repo"])
    assert result.gate == "FAIL"
    assert result.manifest_hash_matches is False
    assert "manifest_hash_mismatch" in result.reason
    assert result.closeout_permitted is False


# ── Test 2: unknown check type → FAIL ──────────────────────────────────────

def test_unknown_check_type(cic, tmp_path, fake_git_repo):
    """check.type='evil_shell_command' is not allowlisted → FAIL."""
    repo = fake_git_repo
    manifest = _write_manifest(tmp_path, requirements=[
        {
            "id": "FAS-A-001", "text": "Skapa isolerad kandidat från verifierad gemensam bas",
            "check": {"type": "evil_shell_command", "command": "rm -rf /"},
            "expected": "exit_0",
        },
    ])
    result = _run_gate(cic, manifest, tmp_path, git_repo=repo["repo"])
    assert result.gate == "FAIL"
    assert "FAS-A-001" in result.failing_ids
    # The result for that requirement should record the unknown type.
    res = next(r for r in result.results if r.requirement_id == "FAS-A-001")
    assert res.status == "fail"
    assert "unknown_check_type" in res.reason


# ── Test 3: path escape → FAIL with path_not_allowlisted ─────────────────────

def test_path_escape(cic, tmp_path, fake_git_repo):
    """A file_sha256 path of '../../../etc/passwd' must FAIL with path_not_allowlisted."""
    repo = fake_git_repo
    manifest = _write_manifest(tmp_path, requirements=[
        {
            "id": "FAS-A-001", "text": "Skapa isolerad kandidat från verifierad gemensam bas",
            "check": {"type": "file_sha256", "path": "../../../etc/passwd",
                      "expected_sha256": "0" * 64},
            "expected": "match",
        },
    ])
    result = _run_gate(cic, manifest, tmp_path, git_repo=repo["repo"])
    assert result.gate == "FAIL"
    assert "FAS-A-001" in result.failing_ids
    res = next(r for r in result.results if r.requirement_id == "FAS-A-001")
    assert res.status == "fail"
    assert "path_not_allowlisted" in res.reason or "shell_metacharacters" in res.reason


# ── Test 4: shell metacharacters in check args → FAIL ───────────────────────

@pytest.mark.parametrize("metachar", [";", "|", "$(", "`"])
def test_shell_metacharacters(cic, tmp_path, fake_git_repo, metachar):
    """Check args containing shell metacharacters → FAIL."""
    repo = fake_git_repo
    # Inject the metacharacter into a check argument string.
    malicious = f"control-plane/some{metachar}evil"
    manifest = _write_manifest(tmp_path, requirements=[
        {
            "id": "FAS-A-001", "text": "Skapa isolerad kandidat från verifierad gemensam bas",
            "check": {"type": "file_sha256", "path": malicious,
                      "expected_sha256": "0" * 64},
            "expected": "match",
        },
    ])
    result = _run_gate(cic, manifest, tmp_path, git_repo=repo["repo"])
    assert result.gate == "FAIL"
    res = next(r for r in result.results if r.requirement_id == "FAS-A-001")
    assert res.status == "fail"
    assert "shell_metacharacters" in res.reason


# ── Test 5: test_receipt from wrong commit → FAIL ───────────────────────────

def test_receipt_from_wrong_commit(cic, tmp_path, fake_git_repo):
    """A test_receipt whose commit does not match expected_commit → FAIL."""
    repo = fake_git_repo
    # Build a receipt with a different commit than expected.
    receipt = {
        "branch": "main",
        "commit": "a" * 40,  # wrong commit
        "check_id": "FAS-A-T-1",
        "timestamp": "2026-07-18T00:00:00Z",
        "output_hash": "b" * 64,
    }
    _write_receipt(tmp_path, "control-plane/receipts/test-1.json", receipt)
    manifest = _write_manifest(tmp_path, requirements=[
        {
            "id": "FAS-A-T-1", "text": "Test receipt requirement",
            "check": {"type": "test_receipt",
                      "receipt_path": "control-plane/receipts/test-1.json",
                      "expected_branch": "main",
                      "expected_commit": "c" * 40,  # different from receipt
                      "expected_check_id": "FAS-A-T-1"},
            "expected": "verified",
        },
    ])
    # Receipt lives under tmp_path; allow it so we reach the commit-mismatch
    # branch (otherwise path_not_allowlisted fires first).
    result = _run_gate(cic, manifest, tmp_path, git_repo=repo["repo"],
                       allow_extra_prefix=str(tmp_path))
    assert result.gate == "FAIL"
    assert "FAS-A-T-1" in result.failing_ids
    res = next(r for r in result.results if r.requirement_id == "FAS-A-T-1")
    assert res.status == "fail"
    assert "commit_mismatch" in res.reason


# ── Test 6: owner_decision_receipt without concrete receipt → BLOCKED ─────

def test_missing_owner_receipt(cic, tmp_path, fake_git_repo):
    """owner_decision_receipt with owner_id=PENDING_OWNER_DECISION → BLOCKED."""
    repo = fake_git_repo
    manifest = _write_manifest(tmp_path, requirements=[
        {
            "id": "FAS-A-004", "text": "Provenance i release-manifest",
            "check": {"type": "owner_decision_receipt",
                      "receipt_path": "control-plane/receipts/owner-1.json",
                      "owner_id": "PENDING_OWNER_DECISION"},
            "expected": "authorized",
        },
    ])
    result = _run_gate(cic, manifest, tmp_path, git_repo=repo["repo"])
    assert result.gate == "BLOCKED"
    assert "FAS-A-004" in result.blocked_ids
    assert result.closeout_permitted is False
    res = next(r for r in result.results if r.requirement_id == "FAS-A-004")
    assert res.status == "blocked"
    assert "owner_id_placeholder" in res.reason


# ── Test 7: deterministic identical result → PASS ───────────────────────────

def test_deterministic_identical_result(cic, tmp_path, fake_git_repo):
    """Same manifest + evidence run twice → identical JSON output."""
    repo = fake_git_repo
    _write_schema(tmp_path)
    manifest = _write_manifest(tmp_path, requirements=[
        {
            "id": "FAS-A-001", "text": "Skapa isolerad kandidat från verifierad gemensam bas",
            "check": {"type": "git_ancestor", "branch": "main", "commit_sha": repo["base_sha"]},
            "expected": "exit_0",
        },
    ])
    r1 = _run_gate(cic, manifest, tmp_path, git_repo=repo["repo"])
    r2 = _run_gate(cic, manifest, tmp_path, git_repo=repo["repo"])
    assert r1.to_dict() == r2.to_dict(), "CIC must be deterministic across runs"
    assert r1.gate == "PASS"
    # JSON serialization must be byte-identical too (sort_keys=True in CIC).
    assert json.dumps(r1.to_dict(), sort_keys=True) == json.dumps(r2.to_dict(), sort_keys=True)


# ── Test 8 (regression, krav 5): self-referential closeout rejected ─────────

def test_self_referential_closeout_rejected(cic, tmp_path, fake_git_repo):
    """Regression (krav 5): 4 cards marked done but 2 requirements lack evidence → FAIL.

    This recreates today's bug: every self-created Kanban card is done, but
    the originating requirements were never satisfied. The fixture has 4
    requirements mapped to mock cards, all cards "done", but 2 requirements
    have no evidence_check-pass. CIC must return gate=FAIL with the 2 missed
    IDs listed, and closeout must be blocked.

    We model "cards done but requirements missing" as: 2 requirements have a
    check that returns a mismatch (e.g. git_ancestor for a commit that is NOT
    an ancestor), while 2 requirements pass. The mock "card done" state lives
    in a side JSON file that the CIC does NOT consult — the point of the test
    is that card-done-ness is irrelevant; only evidence checks decide.
    """
    repo = fake_git_repo
    # Side state: 4 cards all "done" — this is what the buggy flow trusted.
    cards = [
        {"id": "card-1", "requirement_id": "FAS-A-001", "status": "done"},
        {"id": "card-2", "requirement_id": "FAS-A-002", "status": "done"},
        {"id": "card-3", "requirement_id": "FAS-A-003", "status": "done"},
        {"id": "card-4", "requirement_id": "FAS-A-004", "status": "done"},
    ]
    _write_receipt(tmp_path, "control-plane/mock-cards.json", {"cards": cards})

    # 2 requirements pass, 2 fail (evidence mismatch). Cards being "done"
    # must NOT translate to requirements passing.
    manifest = _write_manifest(tmp_path, requirements=[
        {
            "id": "FAS-A-001", "text": "Skapa isolerad kandidat från verifierad gemensam bas",
            "check": {"type": "git_ancestor", "branch": "main", "commit_sha": repo["base_sha"]},
            "expected": "exit_0",  # pass
        },
        {
            "id": "FAS-A-002", "text": "Integrera exakt shared-live + Lumi + Igor patchar i kmros/main",
            "check": {"type": "git_ancestor", "branch": "main", "commit_sha": repo["base_sha"]},
            "expected": "exit_0",  # pass
        },
        {
            "id": "FAS-A-003", "text": "Håll worker-isolation och stop-semantics utanför",
            # divergent_sha IS an ancestor of main? No — it's on a divergent
            # branch, so git_ancestor returns exit_1. expected=exit_0 → FAIL.
            "check": {"type": "git_ancestor", "branch": "main",
                      "commit_sha": repo["divergent_sha"]},
            "expected": "exit_0",  # FAIL: observed exit_1 != expected exit_0
        },
        {
            "id": "FAS-A-004", "text": "Provenance i release-manifest",
            # base_sha IS an ancestor of main, so git merge-base returns
            # exit_0 (the commit IS an ancestor). For git_not_ancestor the
            # CIC reports observed=exit_0; expected=exit_1 → FAIL.
            "check": {"type": "git_not_ancestor", "branch": "main",
                      "commit_sha": repo["base_sha"]},
            "expected": "exit_1",  # FAIL: observed exit_0 != expected exit_1
        },
    ])
    result = _run_gate(cic, manifest, tmp_path, git_repo=repo["repo"])
    assert result.gate == "FAIL", f"expected FAIL, got {result.gate}: {result.reason}"
    assert result.closeout_permitted is False
    # Exactly the 2 missed requirement IDs must be listed, no more, no less.
    assert sorted(result.failing_ids) == ["FAS-A-003", "FAS-A-004"]
    assert result.blocked_ids == []
    # The "done" cards file must not influence the gate at all.
    assert os.path.exists(tmp_path / "control-plane/mock-cards.json")


# ── Test 10: BLOCKED does not allow completion ──────────────────────────────

def test_blocked_does_not_allow_completion(cic, tmp_path, fake_git_repo):
    """gate=BLOCKED must never set closeout_permitted=True."""
    repo = fake_git_repo
    # Mix of a passing check and a blocked owner-decision placeholder.
    manifest = _write_manifest(tmp_path, requirements=[
        {
            "id": "FAS-A-001", "text": "Skapa isolerad kandidat från verifierad gemensam bas",
            "check": {"type": "git_ancestor", "branch": "main", "commit_sha": repo["base_sha"]},
            "expected": "exit_0",  # pass
        },
        {
            "id": "FAS-A-004", "text": "Provenance i release-manifest",
            "check": {"type": "owner_decision_receipt",
                      "receipt_path": "control-plane/receipts/owner-1.json",
                      "owner_id": "PENDING_OWNER_DECISION"},
            "expected": "authorized",  # blocked
        },
    ])
    result = _run_gate(cic, manifest, tmp_path, git_repo=repo["repo"])
    assert result.gate == "BLOCKED"
    assert result.closeout_permitted is False
    assert "Fas A klar" not in json.dumps(result.to_dict())  # no "Fas A klar" string emitted
    assert "FAS-A-004" in result.blocked_ids
    assert "FAS-A-001" not in result.blocked_ids
    assert "FAS-A-001" not in result.failing_ids


# ── Test 11: FAIL lists exact requirement IDs ──────────────────────────────

def test_fail_lists_exact_requirement_ids(cic, tmp_path, fake_git_repo):
    """FAIL must list exactly the failing requirement IDs, no extras."""
    repo = fake_git_repo
    manifest = _write_manifest(tmp_path, requirements=[
        {
            "id": "FAS-A-001", "text": "Skapa isolerad kandidat från verifierad gemensam bas",
            "check": {"type": "git_ancestor", "branch": "main", "commit_sha": repo["base_sha"]},
            "expected": "exit_0",  # pass
        },
        {
            "id": "FAS-A-007", "text": "#spel route-contract och syntetiska tester i kandidaten",
            "check": {"type": "git_ancestor", "branch": "main",
                      "commit_sha": repo["divergent_sha"]},  # not ancestor → exit_1
            "expected": "exit_0",  # FAIL
        },
        {
            "id": "FAS-A-013", "text": "Bred testsvit oberoende omkörd av tredje part",
            "check": {"type": "git_not_ancestor", "branch": "main",
                      "commit_sha": repo["base_sha"]},  # base IS ancestor → exit_0 for not_ancestor means... 
            "expected": "exit_1",  # observed exit_0 != expected exit_1 → FAIL
        },
    ])
    result = _run_gate(cic, manifest, tmp_path, git_repo=repo["repo"])
    assert result.gate == "FAIL"
    assert sorted(result.failing_ids) == ["FAS-A-007", "FAS-A-013"]
    assert result.blocked_ids == []
    # The passing one must not appear in failing_ids.
    assert "FAS-A-001" not in result.failing_ids


# ── Extra: source_hash drift → FAIL (defends the self-contained invariant) ─

def test_source_hash_drift_fails(cic, tmp_path, fake_git_repo):
    """If a requirement's text is edited after pinning, source_hash mismatches → FAIL."""
    repo = fake_git_repo
    manifest = _write_manifest(tmp_path, requirements=[
        {
            "id": "FAS-A-001", "text": "Skapa isolerad kandidat från verifierad gemensam bas",
            # Deliberately wrong source_hash (not matching text)
            "source_hash": "0" * 64,
            "check": {"type": "git_ancestor", "branch": "main", "commit_sha": repo["base_sha"]},
            "expected": "exit_0",
        },
    ])
    result = _run_gate(cic, manifest, tmp_path, git_repo=repo["repo"])
    assert result.gate == "FAIL"
    assert "source_hash_mismatch" in result.reason
    assert "FAS-A-001" in result.failing_ids


# ── Extra: CIC is read-only — no mutation of filesystem outside base_dir ────

def test_cic_does_not_mutate_filesystem(cic, tmp_path, fake_git_repo):
    """CIC must not create, delete, or modify any file outside its read set.

    We snapshot the tmp_path tree before and after running the gate and
    assert the set of paths and their contents are unchanged.
    """
    repo = fake_git_repo
    _write_schema(tmp_path)
    manifest = _write_manifest(tmp_path, requirements=[
        {
            "id": "FAS-A-001", "text": "Skapa isolerad kandidat från verifierad gemensam bas",
            "check": {"type": "git_ancestor", "branch": "main", "commit_sha": repo["base_sha"]},
            "expected": "exit_0",
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


# ── Extra: the real canonical manifest validates against the schema ─────────

def test_canonical_manifest_validates_against_schema(cic, tmp_path):
    """The shipped control-plane/fas-a-requirements.yaml must validate against
    the shipped control-plane/fas-a-requirements.schema.json."""
    import jsonschema
    repo_root = Path(__file__).resolve().parents[2]
    manifest = yaml.safe_load((repo_root / "control-plane" / "fas-a-requirements.yaml").read_bytes())
    schema = json.loads((repo_root / "control-plane" / "fas-a-requirements.schema.json").read_bytes())
    jsonschema.validate(instance=manifest, schema=schema)


# ── Extra: the canonical manifest has all 13 stable IDs and exact texts ─────

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


# ── Extra: CIC behaviour — no shell=True anywhere in the source ─────────────

def test_cic_never_uses_shell_true():
    """The CIC source must never pass shell=True to subprocess.run."""
    src = CIC_PATH.read_text()
    assert "shell=True" not in src, "CIC must never use shell=True"
    # The git invocation must build a list argument (not a free-form string).
    assert 'cmd = ["git"' in src or "cmd = ['git'" in src, \
        "CIC must invoke git via a list argument, not a shell string"


# ── Extra: CIC behaviour — does not write, create tasks, push, or install ──

def test_cic_source_has_no_mutation_calls():
    """CIC source must not contain mutation APIs (write, remove, push, pip)."""
    src = CIC_PATH.read_text()
    forbidden = [
        "kanban_create", "kanban_complete", "kanban_block",  # board mutations
        "git push", "git push", "subprocess.run([\"git\", \"push",
        "pip install", "uv pip install", "os.system", "os.remove",
        "shutil.rmtree", "Path(",  # Path() alone is fine; this is a coarse guard
    ]
    # Allow Path() since the CIC uses pathlib for path math; only flag the
    # genuinely dangerous mutation calls.
    strict_forbidden = [
        "kanban_create", "kanban_complete", "kanban_block",
        "git push", "pip install", "uv pip install", "os.system",
        "shutil.rmtree", "os.remove(", "os.unlink(",
    ]
    for token in strict_forbidden:
        assert token not in src, f"CIC source must not contain {token!r}"