"""Comprehensive tests for the reusable completion-integrity-gate (CIG).

These tests are hermetic: they build temporary manifests, schemas, and receipts
on disk and exercise scripts/completion_gate/gate.py through its public
run_gate API. No real Kanban board, no network, no mutations outside the
per-test tmpdir.

Coverage:
  - Basic gate functionality: empty, truncated, schema-invalid manifests
  - Goal-ID binding enforcement (contract rule 1)
  - Card-reference validation (contract rule 2)
  - Every requirement requires evidence (contract rule 3)
  - Self-certification rejected (contract rule 5)
  - COMPLETE denied if any requirement lacks evidence (contract rule 6)
  - False-pass pattern guards (all 6 patterns from the contract)
  - Negative test: remove evidence -> FAIL or BLOCKED, never PASS
  - Source-code invariants: no shell=True, no mutation, no hardcoded semantics
  - Determinism: identical inputs produce identical output
  - Trust context DI seam
  - Recursive also_blocked propagation in nested all_of
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

CIG_PATH = Path(__file__).resolve().parents[2] / "scripts" / "completion_gate" / "gate.py"
SCHEMA_SRC = Path(__file__).resolve().parents[2] / "control-plane" / "requirements.schema.json"


def _load_cig():
    mod_name = "completion_gate_gate"
    spec = importlib.util.spec_from_file_location(mod_name, CIG_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def cig():
    return _load_cig()


@pytest.fixture
def fake_git_repo(tmp_path):
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
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "main"], check=True, env=env)
    return {"repo": str(repo), "base_sha": base_sha,
            "child_sha": child_sha, "divergent_sha": divergent_sha}


class FakeTrustContext:
    """Test-only in-memory fake trust context."""

    def __init__(self, approved_manifest_hash=None, approved_receipt_hashes=None,
                 authorized_owner_ids=None):
        self._approved_manifest_hash = approved_manifest_hash
        self._approved_receipt_hashes = approved_receipt_hashes or set()
        self._authorized_owner_ids = authorized_owner_ids or set()

    def verify_manifest_digest(self, digest):
        if self._approved_manifest_hash is None:
            return True
        return digest == self._approved_manifest_hash

    def verify_receipt_binding(self, receipt_hash, requirement_id, check_id):
        if not self._approved_receipt_hashes:
            return True
        return receipt_hash in self._approved_receipt_hashes

    def verify_owner_decision(self, owner_id, receipt):
        if not self._authorized_owner_ids:
            return False
        return owner_id in self._authorized_owner_ids


def _sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _write_manifest(base_dir, requirements, manifest_id="test-manifest-v1",
                    goal_id="GO_TEST", source_message="test source message",
                    extra=None, raw_text=None):
    cp = base_dir / "control-plane"
    cp.mkdir(parents=True, exist_ok=True)
    path = cp / "requirements.yaml"
    if raw_text is not None:
        path.write_text(raw_text)
        return path
    doc = {"manifest_version": "1.0", "manifest_id": manifest_id,
           "goal_id": goal_id, "source_message": source_message,
           "description": "test manifest"}
    if extra:
        doc.update(extra)
    for req in requirements:
        if "source_hash" not in req:
            req["source_hash"] = _sha256(req["text"])
    doc["requirements"] = requirements
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    return path


def _write_schema(base_dir):
    cp = base_dir / "control-plane"
    cp.mkdir(parents=True, exist_ok=True)
    dst = cp / "requirements.schema.json"
    shutil.copyfile(SCHEMA_SRC, dst)
    return dst


def _write_receipt(base_dir, rel_path, payload):
    full = base_dir / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(json.dumps(payload, sort_keys=True))
    return full


def _write_output_artefact(base_dir, rel_path, content=b"output artefact"):
    full = base_dir / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_bytes(content)
    return hashlib.sha256(content).hexdigest()


def _compute_receipt_hash(data):
    data_without_hash = {k: v for k, v in data.items() if k != "receipt_hash"}
    canonical = json.dumps(data_without_hash, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _make_valid_test_receipt(base_dir, receipt_rel_path, requirement_id, check_id,
                              branch="main", commit="a" * 40,
                              output_rel_path="control-plane/evidence/output.txt",
                              output_content=b"output artefact"):
    output_hash = _write_output_artefact(base_dir, output_rel_path, output_content)
    receipt = {"requirement_id": requirement_id, "check_id": check_id,
                "branch": branch, "commit": commit, "result": "pass",
                "timestamp": _now_iso(), "output_path": output_rel_path,
                "output_hash": output_hash}
    receipt["receipt_hash"] = _compute_receipt_hash(receipt)
    _write_receipt(base_dir, receipt_rel_path, receipt)
    return receipt


def _run_gate(cig, manifest_path, base_dir, trust_context=None, git_repo="",
              schema_path=None):
    return cig.run_gate(manifest_path=str(manifest_path), base_dir=base_dir,
                        trust_context=trust_context, schema_path=schema_path,
                        git_repo=git_repo)


def _build_passing_reqs(repo, n=5, skip=None, override=None):
    skip = skip or set()
    override = override or {}
    reqs = []
    for i in range(1, n + 1):
        rid = f"REQ-{i:03d}"
        if rid in override:
            reqs.append({"id": rid, "text": f"req {rid}", "check": override[rid]})
        elif rid in skip:
            continue
        else:
            reqs.append({"id": rid, "text": f"req {rid}",
                         "check": {"type": "git_ancestor", "branch": "main",
                                   "commit_sha": repo["base_sha"]}})
    return reqs


# == Basic gate functionality ==

def test_empty_manifest(cig, tmp_path):
    _write_schema(tmp_path)
    manifest = _write_manifest(tmp_path, requirements=[], raw_text="")
    result = _run_gate(cig, manifest, tmp_path)
    assert result.gate == "FAIL"
    assert result.closeout_permitted is False


def test_truncated_manifest(cig, tmp_path):
    _write_schema(tmp_path)
    raw = 'manifest_version: "1.0"\nmanifest_id: "test"\ngoal_id: "GO"\nsource_message: "msg"\nrequirements: [\n'
    manifest = _write_manifest(tmp_path, requirements=[], raw_text=raw)
    result = _run_gate(cig, manifest, tmp_path)
    assert result.gate == "FAIL"
    assert result.closeout_permitted is False
    assert "manifest_not_yaml" in result.reason


def test_schema_invalid(cig, tmp_path, fake_git_repo):
    repo = fake_git_repo
    _write_schema(tmp_path)
    manifest = _write_manifest(tmp_path, requirements=[
        {"text": "no id here",
         "check": {"type": "git_ancestor", "branch": "main", "commit_sha": repo["base_sha"]}},
    ])
    result = _run_gate(cig, manifest, tmp_path, git_repo=repo["repo"])
    assert result.gate == "FAIL"
    assert result.schema_valid is False


def test_duplicate_id(cig, tmp_path, fake_git_repo):
    repo = fake_git_repo
    _write_schema(tmp_path)
    reqs = [
        {"id": "REQ-001", "text": "first",
         "check": {"type": "git_ancestor", "branch": "main", "commit_sha": repo["base_sha"]}},
        {"id": "REQ-001", "text": "second",
         "check": {"type": "git_ancestor", "branch": "main", "commit_sha": repo["base_sha"]}},
    ]
    manifest = _write_manifest(tmp_path, requirements=reqs)
    result = _run_gate(cig, manifest, tmp_path, git_repo=repo["repo"])
    assert result.gate == "FAIL"
    assert result.id_set_valid is False
    assert "duplicate" in result.reason


# == Goal-ID binding enforcement (contract rule 1) ==

def test_goal_id_missing(cig, tmp_path, fake_git_repo):
    repo = fake_git_repo
    _write_schema(tmp_path)
    reqs = _build_passing_reqs(repo)
    cp = tmp_path / "control-plane"
    cp.mkdir(parents=True, exist_ok=True)
    manifest_path = cp / "requirements.yaml"
    doc = {"manifest_version": "1.0", "manifest_id": "test",
           "source_message": "msg", "requirements": reqs}
    manifest_path.write_text(yaml.safe_dump(doc, sort_keys=False))
    result = _run_gate(cig, manifest_path, tmp_path, git_repo=repo["repo"])
    assert result.gate == "FAIL"
    assert ("goal_binding_invalid" in result.reason or "schema_invalid" in result.reason)


def test_source_message_missing(cig, tmp_path, fake_git_repo):
    repo = fake_git_repo
    _write_schema(tmp_path)
    reqs = _build_passing_reqs(repo)
    cp = tmp_path / "control-plane"
    cp.mkdir(parents=True, exist_ok=True)
    manifest_path = cp / "requirements.yaml"
    doc = {"manifest_version": "1.0", "manifest_id": "test",
           "goal_id": "GO_TEST", "requirements": reqs}
    manifest_path.write_text(yaml.safe_dump(doc, sort_keys=False))
    result = _run_gate(cig, manifest_path, tmp_path, git_repo=repo["repo"])
    assert result.gate == "FAIL"
    assert ("goal_binding_invalid" in result.reason or "schema_invalid" in result.reason)


def test_goal_id_in_result(cig, tmp_path, fake_git_repo):
    repo = fake_git_repo
    _write_schema(tmp_path)
    reqs = _build_passing_reqs(repo)
    manifest = _write_manifest(tmp_path, requirements=reqs, goal_id="GO_MY_GOAL")
    result = _run_gate(cig, manifest, tmp_path, git_repo=repo["repo"])
    assert result.goal_id == "GO_MY_GOAL"


# == Card-reference validation (contract rule 2) ==

def test_cards_reference_requirements(cig, tmp_path, fake_git_repo):
    repo = fake_git_repo
    _write_schema(tmp_path)
    reqs = _build_passing_reqs(repo)
    manifest = _write_manifest(tmp_path, requirements=reqs, extra={
        "cards": [{"id": "card-1", "requirement_id": "REQ-001", "status": "done"},
                  {"id": "card-2", "requirement_id": "REQ-002", "status": "in_progress"}]
    })
    result = _run_gate(cig, manifest, tmp_path, git_repo=repo["repo"])
    assert "card_reference_invalid" not in result.reason


def test_card_references_unknown_requirement(cig, tmp_path, fake_git_repo):
    repo = fake_git_repo
    _write_schema(tmp_path)
    reqs = _build_passing_reqs(repo)
    manifest = _write_manifest(tmp_path, requirements=reqs, extra={
        "cards": [{"id": "card-1", "requirement_id": "REQ-999", "status": "done"}]
    })
    result = _run_gate(cig, manifest, tmp_path, git_repo=repo["repo"])
    assert result.gate == "FAIL"
    assert "card_reference_invalid" in result.reason


def test_requirement_must_not_carry_card_id(cig, tmp_path, fake_git_repo):
    repo = fake_git_repo
    _write_schema(tmp_path)
    cp = tmp_path / "control-plane"
    cp.mkdir(parents=True, exist_ok=True)
    manifest_path = cp / "requirements.yaml"
    reqs = _build_passing_reqs(repo)
    reqs[0]["card_id"] = "card-1"
    doc = {"manifest_version": "1.0", "manifest_id": "test",
           "goal_id": "GO_TEST", "source_message": "msg", "requirements": reqs}
    manifest_path.write_text(yaml.safe_dump(doc, sort_keys=False))
    result = _run_gate(cig, manifest_path, tmp_path, git_repo=repo["repo"])
    assert result.gate == "FAIL"
    assert result.schema_valid is False or "card_reference_invalid" in result.reason


# == Self-certification rejected (contract rule 5) ==

def test_self_certification_with_cards(cig, tmp_path, fake_git_repo):
    repo = fake_git_repo
    _write_schema(tmp_path)
    reqs = _build_passing_reqs(repo, n=5, override={
        "REQ-003": {"type": "git_ancestor", "branch": "main", "commit_sha": repo["divergent_sha"]},
        "REQ-004": {"type": "git_not_ancestor", "branch": "main", "commit_sha": repo["base_sha"]},
    })
    manifest = _write_manifest(tmp_path, requirements=reqs, extra={
        "cards": [{"id": f"card-{i}", "requirement_id": f"REQ-{i:03d}", "status": "done"}
                  for i in range(1, 6)]
    })
    result = _run_gate(cig, manifest, tmp_path, git_repo=repo["repo"])
    assert result.gate == "FAIL", f"expected FAIL, got {result.gate}: {result.reason}"
    assert result.closeout_permitted is False
    assert "REQ-003" in result.failing_ids
    assert "REQ-004" in result.failing_ids


# == COMPLETE denied if any requirement lacks evidence (contract rule 6) ==

def test_all_pass_without_trust_still_fail(cig, tmp_path, fake_git_repo):
    repo = fake_git_repo
    _write_schema(tmp_path)
    reqs = _build_passing_reqs(repo)
    manifest = _write_manifest(tmp_path, requirements=reqs)
    result = _run_gate(cig, manifest, tmp_path, git_repo=repo["repo"], trust_context=None)
    assert result.gate == "FAIL"
    assert result.closeout_permitted is False
    assert result.reason == "trust_context_missing"


def test_all_pass_with_trust_passes(cig, tmp_path, fake_git_repo):
    repo = fake_git_repo
    _write_schema(tmp_path)
    reqs = _build_passing_reqs(repo)
    manifest = _write_manifest(tmp_path, requirements=reqs)
    manifest_hash = hashlib.sha256(manifest.read_bytes()).hexdigest()
    trust = FakeTrustContext(approved_manifest_hash=manifest_hash)
    result = _run_gate(cig, manifest, tmp_path, git_repo=repo["repo"], trust_context=trust)
    assert result.gate == "PASS"
    assert result.closeout_permitted is True


def test_one_fail_blocks_closeout(cig, tmp_path, fake_git_repo):
    repo = fake_git_repo
    _write_schema(tmp_path)
    reqs = _build_passing_reqs(repo, n=5, override={
        "REQ-003": {"type": "git_ancestor", "branch": "main", "commit_sha": repo["divergent_sha"]},
    })
    manifest = _write_manifest(tmp_path, requirements=reqs)
    manifest_hash = hashlib.sha256(manifest.read_bytes()).hexdigest()
    trust = FakeTrustContext(approved_manifest_hash=manifest_hash)
    result = _run_gate(cig, manifest, tmp_path, git_repo=repo["repo"], trust_context=trust)
    assert result.gate == "FAIL"
    assert result.closeout_permitted is False
    assert "REQ-003" in result.failing_ids


# == NEGATIVE TEST: Remove evidence -> FAIL or BLOCKED, never PASS ==

def test_negative_remove_evidence(cig, tmp_path, fake_git_repo):
    """NEGATIVE TEST: Remove evidence for one requirement -> must become FAIL
    or BLOCKED, never PASS."""
    repo = fake_git_repo
    _write_schema(tmp_path)
    reqs = []
    for i in range(1, 4):
        rid = f"REQ-{i:03d}"
        reqs.append({"id": rid, "text": f"req {rid}",
                     "check": {"type": "test_receipt",
                               "receipt_path": f"control-plane/evidence/{rid.lower()}-receipt.json",
                               "expected_check_id": rid}})
    manifest = _write_manifest(tmp_path, requirements=reqs)
    for i in range(1, 4):
        rid = f"REQ-{i:03d}"
        _make_valid_test_receipt(tmp_path, f"control-plane/evidence/{rid.lower()}-receipt.json",
                                 rid, rid,
                                 output_rel_path=f"control-plane/evidence/{rid.lower()}-output.txt")
    manifest_hash = hashlib.sha256(manifest.read_bytes()).hexdigest()
    trust = FakeTrustContext(approved_manifest_hash=manifest_hash)
    result = _run_gate(cig, manifest, tmp_path, trust_context=trust, git_repo=repo["repo"])
    assert result.gate == "PASS", f"baseline should PASS, got {result.gate}: {result.reason}"

    # Remove the evidence output artefact for REQ-002
    output_file = tmp_path / "control-plane" / "evidence" / "req-002-output.txt"
    assert output_file.exists()
    output_file.unlink()

    result2 = _run_gate(cig, manifest, tmp_path, trust_context=trust, git_repo=repo["repo"])
    assert result2.gate in ("FAIL", "BLOCKED"), \
        f"gate must be FAIL or BLOCKED after removing evidence, got {result2.gate}"
    assert result2.gate != "PASS"
    assert result2.closeout_permitted is False
    assert "REQ-002" in result2.failing_ids


# == False-pass pattern guards (all 6 from the contract) ==

def test_false_pass_free_expected_bypass(cig, tmp_path, fake_git_repo):
    """Guard 1: Free expected-bypass. Manifest must NEVER carry an expected field."""
    repo = fake_git_repo
    _write_schema(tmp_path)
    cp = tmp_path / "control-plane"
    cp.mkdir(parents=True, exist_ok=True)
    manifest_path = cp / "requirements.yaml"
    doc = {"manifest_version": "1.0", "manifest_id": "test",
           "goal_id": "GO_TEST", "source_message": "msg",
           "requirements": [{"id": "REQ-001", "text": "req",
                             "source_hash": _sha256("req"),
                             "check": {"type": "git_ancestor", "branch": "main",
                                       "commit_sha": repo["base_sha"],
                                       "expected": "bypass"}}]}
    manifest_path.write_text(yaml.safe_dump(doc, sort_keys=False))
    result = _run_gate(cig, manifest_path, tmp_path, git_repo=repo["repo"])
    assert result.gate == "FAIL"
    assert result.schema_valid is False


def test_false_pass_self_computed_cli_hash(cig, tmp_path, fake_git_repo):
    """Guard 2: Self-computed CLI hash must NOT grant PASS."""
    repo = fake_git_repo
    _write_schema(tmp_path)
    reqs = _build_passing_reqs(repo)
    manifest = _write_manifest(tmp_path, requirements=reqs)
    manifest_hash = hashlib.sha256(manifest.read_bytes()).hexdigest()
    import io
    old_stdout = sys.stdout
    try:
        sys.stdout = io.StringIO()
        cig.main(["--manifest", str(manifest), "--base-dir", str(tmp_path),
                  "--git-repo", repo["repo"], "--expected-manifest-hash", manifest_hash])
        output = sys.stdout.getvalue()
    finally:
        sys.stdout = old_stdout
    data = json.loads(output)
    assert data["gate"] == "FAIL"
    assert data["closeout_permitted"] is False
    assert data["trust_context_present"] is False


def test_false_pass_fabricated_receipt_without_output_path(cig, tmp_path, fake_git_repo):
    """Guard 3: Fabricated receipts without output_path -> FAIL."""
    repo = fake_git_repo
    _write_schema(tmp_path)
    reqs = [{"id": "REQ-001", "text": "req",
             "check": {"type": "test_receipt",
                       "receipt_path": "control-plane/evidence/req-001-receipt.json",
                       "expected_check_id": "REQ-001"}}]
    manifest = _write_manifest(tmp_path, requirements=reqs)
    receipt = {"requirement_id": "REQ-001", "check_id": "REQ-001",
               "branch": "main", "commit": "a" * 40, "result": "pass",
               "timestamp": _now_iso(), "output_hash": "0" * 64,
               "receipt_hash": "0" * 64}
    _write_receipt(tmp_path, "control-plane/evidence/req-001-receipt.json", receipt)
    manifest_hash = hashlib.sha256(manifest.read_bytes()).hexdigest()
    trust = FakeTrustContext(approved_manifest_hash=manifest_hash)
    result = _run_gate(cig, manifest, tmp_path, trust_context=trust, git_repo=repo["repo"])
    assert result.gate == "FAIL"
    assert "REQ-001" in result.failing_ids


def test_false_pass_check_substitution(cig, tmp_path, fake_git_repo):
    """Guard 4: Check-substitution (all checks -> git_ancestor) -> manifest_tampered."""
    repo = fake_git_repo
    _write_schema(tmp_path)
    reqs = [{"id": "REQ-001", "text": "req",
             "check": {"type": "test_receipt",
                       "receipt_path": "control-plane/evidence/req-001-receipt.json",
                       "expected_check_id": "REQ-001"}}]
    manifest = _write_manifest(tmp_path, requirements=reqs)
    original_hash = hashlib.sha256(manifest.read_bytes()).hexdigest()
    trust = FakeTrustContext(approved_manifest_hash=original_hash)
    # Substitute check type
    reqs_sub = [{"id": "REQ-001", "text": "req",
                 "check": {"type": "git_ancestor", "branch": "main",
                           "commit_sha": repo["base_sha"]}}]
    manifest = _write_manifest(tmp_path, requirements=reqs_sub)
    result = _run_gate(cig, manifest, tmp_path, trust_context=trust, git_repo=repo["repo"])
    assert result.gate == "FAIL"
    assert result.reason == "manifest_tampered"


def test_false_pass_schema_realpath_cwd_dependence(cig, tmp_path, fake_git_repo):
    """Guard 5: Schema-realpath cwd-dependence. CIG uses base_dir's strict schema."""
    repo = fake_git_repo
    base_dir = tmp_path / "base"
    (base_dir / "control-plane").mkdir(parents=True)
    _write_schema(base_dir)
    cwd_dir = tmp_path / "cwd"
    (cwd_dir / "control-plane").mkdir(parents=True)
    permissive_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["manifest_version", "manifest_id", "goal_id", "source_message", "requirements"],
        "properties": {"manifest_version": {"type": "string"},
                        "manifest_id": {"type": "string"},
                        "goal_id": {"type": "string"},
                        "source_message": {"type": "string"},
                        "requirements": {"type": "array"}}}
    (cwd_dir / "control-plane" / "requirements.schema.json").write_text(json.dumps(permissive_schema))
    reqs = []
    for i in range(1, 4):
        rid = f"REQ-{i:03d}"
        r = {"id": rid, "text": f"req {rid}", "source_hash": _sha256(f"req {rid}"),
             "check": {"type": "git_ancestor", "branch": "main", "commit_sha": repo["base_sha"]}}
        if i == 1:
            r["bogus_extra"] = "forbidden_field"
        reqs.append(r)
    manifest_doc = {"manifest_version": "1.0", "manifest_id": "test-schema-realpath",
                    "goal_id": "GO_TEST", "source_message": "msg", "requirements": reqs}
    manifest = base_dir / "control-plane" / "requirements.yaml"
    manifest.write_text(yaml.safe_dump(manifest_doc, sort_keys=False))
    result = _run_gate(cig, manifest, base_dir, git_repo=repo["repo"],
                      schema_path="control-plane/requirements.schema.json")
    assert result.gate == "FAIL"
    assert result.schema_valid is False
    assert "bogus_extra" in result.reason or "Additional properties" in result.reason


def test_false_pass_recursive_also_blocked_dropped(cig, tmp_path, fake_git_repo):
    """Guard 6: Recursive also_blocked propagation in nested all_of."""
    repo = fake_git_repo
    _write_schema(tmp_path)
    reqs = _build_passing_reqs(repo, n=5, override={
        "REQ-003": {"type": "all_of", "checks": [
            {"type": "all_of", "checks": [
                {"type": "file_sha256", "path": "control-plane/evidence/missing-file.json",
                 "expected_sha256": "PENDING_EVIDENCE"},
                {"type": "owner_decision_receipt",
                 "receipt_path": "control-plane/evidence/owner-receipt.json",
                 "owner_id": "PENDING_OWNER_DECISION"}]},
            {"type": "git_ancestor", "branch": "main", "commit_sha": repo["base_sha"]}]}
    })
    manifest = _write_manifest(tmp_path, requirements=reqs)
    result = _run_gate(cig, manifest, tmp_path, git_repo=repo["repo"])
    assert result.gate == "FAIL", f"expected FAIL, got {result.gate}: {result.reason}"
    assert result.closeout_permitted is False
    assert "REQ-003" in result.failing_ids
    assert "REQ-003" in result.blocked_ids
    cr = next(r for r in result.results if r.requirement_id == "REQ-003")
    assert cr.also_blocked is True


# == Source-code invariants ==

def test_cig_never_uses_shell_true():
    src = CIG_PATH.read_text()
    assert "shell=True" not in src
    assert 'cmd = ["git"' in src or "cmd = ['git'" in src


def test_cig_source_has_no_mutation_calls():
    import ast
    src = CIG_PATH.read_text()
    tree = ast.parse(src)
    called_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                called_names.add(f"{func.value.id}.{func.attr}")
            elif isinstance(func, ast.Name):
                called_names.add(func.id)
    for bad in {"os.system", "os.remove", "os.unlink", "shutil.rmtree", "shutil.move", "subprocess.Popen"}:
        assert bad not in called_names, f"CIG must not call {bad!r}"
    for token in ("git push", "pip install", "uv pip install",
                  "kanban_create", "kanban_complete", "kanban_block"):
        assert token not in src, f"CIG source must not contain {token!r}"


def test_cig_source_has_no_hardcoded_requirement_semantics():
    import ast
    src = CIG_PATH.read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and "REQUIREMENT_SEMANTICS" in target.id:
                    pytest.fail(f"CIG must not have a hardcoded {target.id} table")
    assert "FAS-A-" not in src, "CIG must not be hardcoded to FAS-A IDs"


def test_production_cli_has_no_trust_context_provider():
    src = CIG_PATH.read_text()
    assert "trust_context = None" in src
    for token in ("keyring", "gnupg", "subprocess.check_output", "subprocess.Popen"):
        assert token not in src


def test_trust_context_protocol_is_injectable(cig):
    import inspect
    sig = inspect.signature(cig.run_gate)
    assert "trust_context" in sig.parameters
    assert hasattr(cig, "TrustContext")


# == Determinism ==

def test_deterministic_identical_result(cig, tmp_path, fake_git_repo):
    repo = fake_git_repo
    _write_schema(tmp_path)
    reqs = _build_passing_reqs(repo, n=3)
    manifest = _write_manifest(tmp_path, requirements=reqs)
    r1 = _run_gate(cig, manifest, tmp_path, git_repo=repo["repo"])
    r2 = _run_gate(cig, manifest, tmp_path, git_repo=repo["repo"])
    assert r1.to_dict() == r2.to_dict()


# == Path containment ==

def test_manifest_in_sibling_worktree_rejected(cig, tmp_path, fake_git_repo):
    repo = fake_git_repo
    base_dir = tmp_path / "worktree-a"
    (base_dir / "control-plane").mkdir(parents=True)
    _write_schema(base_dir)
    sibling = tmp_path / "worktree-b"
    (sibling / "control-plane").mkdir(parents=True)
    sibling_manifest = sibling / "control-plane" / "requirements.yaml"
    reqs = _build_passing_reqs(repo)
    for r in reqs:
        if "source_hash" not in r:
            r["source_hash"] = _sha256(r["text"])
    sibling_manifest.write_text(yaml.safe_dump({
        "manifest_version": "1.0", "manifest_id": "sibling",
        "goal_id": "GO_TEST", "source_message": "msg", "requirements": reqs}, sort_keys=False))
    result = _run_gate(cig, sibling_manifest, base_dir, git_repo=repo["repo"])
    assert result.gate == "FAIL"
    assert "manifest_not_in_base_dir" in result.reason


def test_symlink_escape(cig, tmp_path):
    fake_worktree = tmp_path / "fake"
    (fake_worktree / "control-plane").mkdir(parents=True)
    outside = tmp_path / "outside_target"
    outside.write_bytes(b"outside secret")
    escape_link = fake_worktree / "control-plane" / "escape.json"
    os.symlink(outside, escape_link)
    ok, msg, real = cig.validate_path("control-plane/escape.json", base_dir=fake_worktree)
    assert ok is False
    assert "path_not_in_base_dir" in msg


# == Read-only (no filesystem mutation) ==

def test_cig_does_not_mutate_filesystem(cig, tmp_path, fake_git_repo):
    repo = fake_git_repo
    _write_schema(tmp_path)
    manifest = _write_manifest(tmp_path, requirements=_build_passing_reqs(repo, n=3))

    def snapshot(root):
        out = {}
        for p in sorted(root.rglob("*")):
            if p.is_file():
                out[str(p)] = hashlib.sha256(p.read_bytes()).hexdigest()
        return out

    before = snapshot(tmp_path)
    _run_gate(cig, manifest, tmp_path, git_repo=repo["repo"])
    after = snapshot(tmp_path)
    assert before == after


# == Source-hash drift ==

def test_source_hash_drift_fails(cig, tmp_path, fake_git_repo):
    repo = fake_git_repo
    _write_schema(tmp_path)
    reqs = _build_passing_reqs(repo, n=3)
    for r in reqs:
        if r["id"] == "REQ-001":
            r["source_hash"] = "0" * 64
    manifest = _write_manifest(tmp_path, requirements=reqs)
    result = _run_gate(cig, manifest, tmp_path, git_repo=repo["repo"])
    assert result.gate == "FAIL"
    assert "source_hash_mismatch" in result.reason
    assert "REQ-001" in result.failing_ids


# == Branch validation ==

def test_branch_leading_dash_rejected(cig, tmp_path, fake_git_repo):
    repo = fake_git_repo
    _write_schema(tmp_path)
    reqs = _build_passing_reqs(repo, n=3, override={
        "REQ-001": {"type": "git_ancestor", "branch": "--help", "commit_sha": repo["base_sha"]}})
    manifest = _write_manifest(tmp_path, requirements=reqs)
    result = _run_gate(cig, manifest, tmp_path, git_repo=repo["repo"])
    assert result.gate == "FAIL"
    assert "REQ-001" in result.failing_ids


def test_branch_leading_slash_rejected(cig, tmp_path, fake_git_repo):
    repo = fake_git_repo
    _write_schema(tmp_path)
    reqs = _build_passing_reqs(repo, n=3, override={
        "REQ-001": {"type": "git_ancestor", "branch": "/leading", "commit_sha": repo["base_sha"]}})
    manifest = _write_manifest(tmp_path, requirements=reqs)
    result = _run_gate(cig, manifest, tmp_path, git_repo=repo["repo"])
    assert result.gate == "FAIL"
    assert "REQ-001" in result.failing_ids


# == Simultaneous FAIL and BLOCKED ==

def test_simultaneous_fail_and_blocked(cig, tmp_path, fake_git_repo):
    repo = fake_git_repo
    _write_schema(tmp_path)
    reqs = _build_passing_reqs(repo, n=5, override={
        "REQ-001": {"type": "git_ancestor", "branch": "main", "commit_sha": repo["divergent_sha"]},
        "REQ-005": {"type": "owner_decision_receipt",
                    "receipt_path": "control-plane/evidence/owner-receipt.json",
                    "owner_id": "PENDING_OWNER_DECISION"}})
    manifest = _write_manifest(tmp_path, requirements=reqs)
    result = _run_gate(cig, manifest, tmp_path, git_repo=repo["repo"])
    assert result.gate == "FAIL"
    assert "REQ-001" in result.failing_ids
    assert "REQ-005" in result.blocked_ids
    assert result.closeout_permitted is False


# == Example manifest validates against schema ==

def test_example_manifest_validates_against_schema(cig, tmp_path):
    import jsonschema
    repo_root = Path(__file__).resolve().parents[2]
    manifest = yaml.safe_load((repo_root / "control-plane" / "example-requirements.yaml").read_bytes())
    schema = json.loads((repo_root / "control-plane" / "requirements.schema.json").read_bytes())
    jsonschema.validate(instance=manifest, schema=schema)


# == Test receipt validation ==

def test_test_receipt_wrong_requirement_id(cig, tmp_path, fake_git_repo):
    repo = fake_git_repo
    _write_schema(tmp_path)
    reqs = [{"id": "REQ-001", "text": "req",
             "check": {"type": "test_receipt",
                       "receipt_path": "control-plane/evidence/req-001-receipt.json",
                       "expected_check_id": "REQ-001"}}]
    manifest = _write_manifest(tmp_path, requirements=reqs)
    _make_valid_test_receipt(tmp_path, "control-plane/evidence/req-001-receipt.json",
                             "WRONG_ID", "REQ-001")
    manifest_hash = hashlib.sha256(manifest.read_bytes()).hexdigest()
    trust = FakeTrustContext(approved_manifest_hash=manifest_hash)
    result = _run_gate(cig, manifest, tmp_path, trust_context=trust, git_repo=repo["repo"])
    assert result.gate == "FAIL"
    assert "REQ-001" in result.failing_ids


def test_test_receipt_result_not_pass(cig, tmp_path, fake_git_repo):
    repo = fake_git_repo
    _write_schema(tmp_path)
    reqs = [{"id": "REQ-001", "text": "req",
             "check": {"type": "test_receipt",
                       "receipt_path": "control-plane/evidence/req-001-receipt.json",
                       "expected_check_id": "REQ-001"}}]
    manifest = _write_manifest(tmp_path, requirements=reqs)
    output_hash = _write_output_artefact(tmp_path, "control-plane/evidence/output.txt")
    receipt = {"requirement_id": "REQ-001", "check_id": "REQ-001",
               "branch": "main", "commit": "a" * 40, "result": "fail",
               "timestamp": _now_iso(), "output_path": "control-plane/evidence/output.txt",
               "output_hash": output_hash}
    receipt["receipt_hash"] = _compute_receipt_hash(receipt)
    _write_receipt(tmp_path, "control-plane/evidence/req-001-receipt.json", receipt)
    manifest_hash = hashlib.sha256(manifest.read_bytes()).hexdigest()
    trust = FakeTrustContext(approved_manifest_hash=manifest_hash)
    result = _run_gate(cig, manifest, tmp_path, trust_context=trust, git_repo=repo["repo"])
    assert result.gate == "FAIL"
    assert "REQ-001" in result.failing_ids


def test_test_receipt_output_hash_mismatch(cig, tmp_path, fake_git_repo):
    repo = fake_git_repo
    _write_schema(tmp_path)
    reqs = [{"id": "REQ-001", "text": "req",
             "check": {"type": "test_receipt",
                       "receipt_path": "control-plane/evidence/req-001-receipt.json",
                       "expected_check_id": "REQ-001"}}]
    manifest = _write_manifest(tmp_path, requirements=reqs)
    _write_output_artefact(tmp_path, "control-plane/evidence/output.txt")
    receipt = {"requirement_id": "REQ-001", "check_id": "REQ-001",
               "branch": "main", "commit": "a" * 40, "result": "pass",
               "timestamp": _now_iso(), "output_path": "control-plane/evidence/output.txt",
               "output_hash": "0" * 64}
    receipt["receipt_hash"] = _compute_receipt_hash(receipt)
    _write_receipt(tmp_path, "control-plane/evidence/req-001-receipt.json", receipt)
    manifest_hash = hashlib.sha256(manifest.read_bytes()).hexdigest()
    trust = FakeTrustContext(approved_manifest_hash=manifest_hash)
    result = _run_gate(cig, manifest, tmp_path, trust_context=trust, git_repo=repo["repo"])
    assert result.gate == "FAIL"
    assert "REQ-001" in result.failing_ids


def test_test_receipt_untrusted_binding_without_trust(cig, tmp_path, fake_git_repo):
    repo = fake_git_repo
    _write_schema(tmp_path)
    reqs = [{"id": "REQ-001", "text": "req",
             "check": {"type": "test_receipt",
                       "receipt_path": "control-plane/evidence/req-001-receipt.json",
                       "expected_check_id": "REQ-001"}}]
    manifest = _write_manifest(tmp_path, requirements=reqs)
    _make_valid_test_receipt(tmp_path, "control-plane/evidence/req-001-receipt.json",
                             "REQ-001", "REQ-001")
    result = _run_gate(cig, manifest, tmp_path, trust_context=None, git_repo=repo["repo"])
    assert result.gate == "FAIL"
    assert "REQ-001" in result.failing_ids


# == Production CLI cannot reach test trust verifier ==

def test_production_cli_cannot_reach_test_trust_verifier(cig, tmp_path, fake_git_repo):
    repo = fake_git_repo
    _write_schema(tmp_path)
    reqs = _build_passing_reqs(repo)
    manifest = _write_manifest(tmp_path, requirements=reqs)
    import io
    old_stdout = sys.stdout
    try:
        sys.stdout = io.StringIO()
        cig.main(["--manifest", str(manifest), "--base-dir", str(tmp_path),
                  "--git-repo", repo["repo"], "--trust-context-source", "fabricated"])
        output = sys.stdout.getvalue()
    finally:
        sys.stdout = old_stdout
    data = json.loads(output)
    assert data["gate"] == "FAIL"
    assert data["closeout_permitted"] is False
    assert data["trust_context_present"] is False


# == Manifest tampered detection ==

def test_manifest_tampered_after_trust(cig, tmp_path, fake_git_repo):
    repo = fake_git_repo
    _write_schema(tmp_path)
    reqs = _build_passing_reqs(repo, n=3)
    manifest = _write_manifest(tmp_path, requirements=reqs)
    original_hash = hashlib.sha256(manifest.read_bytes()).hexdigest()
    trust = FakeTrustContext(approved_manifest_hash=original_hash)
    result = _run_gate(cig, manifest, tmp_path, trust_context=trust, git_repo=repo["repo"])
    assert result.gate == "PASS"
    # Tamper: add a new requirement
    reqs.append({"id": "REQ-004", "text": "extra",
                 "check": {"type": "git_ancestor", "branch": "main", "commit_sha": repo["base_sha"]}})
    manifest = _write_manifest(tmp_path, requirements=reqs)
    result2 = _run_gate(cig, manifest, tmp_path, trust_context=trust, git_repo=repo["repo"])
    assert result2.gate == "FAIL"
    assert result2.reason == "manifest_tampered"
