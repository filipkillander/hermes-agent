from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from hermes_cli.immutable_update_builder import (
    FOCUS_TESTS,
    CommandDigest,
    ImmutableUpdateBuilder,
    UpdateBuildError,
    _harness_sha256,
    _stage_build,
)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "tests@example.invalid")
    _git(repo, "config", "user.name", "Tests")
    (repo / "app.py").write_text("print('ok')\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "fixture")
    return repo, _git(repo, "rev-parse", "HEAD")


def test_validate_source_requires_clean_pinned_head(tmp_path: Path) -> None:
    repo, commit = _repo(tmp_path)
    builder = ImmutableUpdateBuilder(tmp_path / "home")

    assert builder.validate_source(repo, "HEAD", commit) == commit
    with pytest.raises(UpdateBuildError, match="invalid_expected_commit"):
        builder.validate_source(repo, "HEAD", "not-a-commit")
    with pytest.raises(UpdateBuildError, match="invalid_source_ref"):
        builder.validate_source(repo, "--help", commit)
    (repo / "untracked").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(UpdateBuildError, match="source_not_clean"):
        builder.validate_source(repo, "HEAD", commit)


def test_validate_source_rejects_ref_or_head_mismatch(tmp_path: Path) -> None:
    repo, old_commit = _repo(tmp_path)
    (repo / "app.py").write_text("print('new')\n", encoding="utf-8")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-qm", "new")
    builder = ImmutableUpdateBuilder(tmp_path / "home")

    with pytest.raises(UpdateBuildError, match="source_ref_mismatch"):
        builder.validate_source(repo, "HEAD", old_commit)


def test_compare_current_is_verified_and_detects_no_change(tmp_path: Path) -> None:
    repo, commit = _repo(tmp_path)
    builder = ImmutableUpdateBuilder(tmp_path / "home")
    release = builder.manager.stage(repo, "r1")
    profile = builder.manager.profile_dir("lumi")
    profile.mkdir(parents=True)
    (profile / "current").symlink_to(release)

    result = builder.compare_current(["lumi", "spark"], commit)

    assert result["profile_count"] == 2
    assert result["current_matching_count"] == 1
    assert result["current_missing_count"] == 1
    assert result["current_commit_hashes"] == [commit]


def test_compare_current_rejects_pointer_outside_release_root(tmp_path: Path) -> None:
    builder = ImmutableUpdateBuilder(tmp_path / "home")
    profile = builder.manager.profile_dir("lumi")
    profile.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (profile / "current").symlink_to(outside)

    with pytest.raises(UpdateBuildError, match="current_pointer_escape"):
        builder.compare_current(["lumi"], "a" * 40)


def test_builder_lock_is_nonblocking_and_private(tmp_path: Path) -> None:
    first = ImmutableUpdateBuilder(tmp_path / "home")
    second = ImmutableUpdateBuilder(tmp_path / "home")

    with first.lock():
        assert os.stat(first.lock_path).st_mode & 0o777 == 0o600
        with pytest.raises(UpdateBuildError, match="builder_busy"):
            with second.lock():
                raise AssertionError("unreachable")


def test_no_change_writes_private_count_hash_only_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, commit = _repo(tmp_path)
    builder = ImmutableUpdateBuilder(tmp_path / "home")
    monkeypatch.setattr(builder, "compare_current", lambda profiles, candidate: {
        "profile_count": 1,
        "current_missing_count": 0,
        "current_matching_count": 1,
        "current_unique_commit_count": 1,
        "current_commit_hashes": [candidate],
    })
    monkeypatch.setattr(
        builder,
        "run_focus_harness",
        lambda source, candidate: (_ for _ in ()).throw(AssertionError("must not run")),
    )

    result = builder.build(
        source=repo,
        ref="HEAD",
        expected_commit=commit,
        release_id="release-1",
        profiles=["lumi"],
    )

    assert result["state"] == "no_change"
    assert "release-1" not in json.dumps(result)
    assert builder.status_path.stat().st_mode & 0o777 == 0o600
    assert builder.status_path.parent.stat().st_mode & 0o777 == 0o700


def test_build_stages_only_after_focus_and_revalidation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, commit = _repo(tmp_path)
    builder = ImmutableUpdateBuilder(tmp_path / "home")
    calls: list[str] = []
    original_validate = builder.validate_source

    def validate(source: Path, ref: str, expected: str) -> str:
        calls.append("validate")
        return original_validate(source, ref, expected)

    monkeypatch.setattr(builder, "validate_source", validate)
    monkeypatch.setattr(builder, "compare_current", lambda profiles, candidate: {
        "profile_count": 1,
        "current_missing_count": 1,
        "current_matching_count": 0,
        "current_unique_commit_count": 0,
        "current_commit_hashes": [],
    })

    def focus(source: Path, candidate: str) -> CommandDigest:
        calls.append("focus")
        return CommandDigest(0, "f" * 64, 42)

    monkeypatch.setattr(builder, "run_focus_harness", focus)

    def stage(*args: object, **kwargs: object) -> Path:
        calls.append("stage")
        assert kwargs["ref"] == commit
        build = kwargs["build"]
        assert "_stage-build" in build
        return builder.manager.release_path("release-1")

    monkeypatch.setattr(builder.manager, "stage", stage)
    monkeypatch.setattr(builder, "verify_staged_release", lambda *args: {
        "file_count": 5,
        "size_bytes": 10,
        "manifest_sha256": "a" * 64,
        "import_output_sha256": "e" * 64,
        "import_output_bytes": 0,
    })
    uv = tmp_path / "uv"
    uv.write_text("#!/bin/sh\n", encoding="utf-8")

    result = builder.build(
        source=repo,
        ref="HEAD",
        expected_commit=commit,
        release_id="release-1",
        profiles=["lumi"],
        uv_path=uv,
    )

    assert calls == ["validate", "focus", "validate", "stage"]
    assert result["state"] == "staged"
    assert result["focus_output_sha256"] == "f" * 64


def test_official_harness_is_fixed_and_hashed() -> None:
    assert "tests/hermes_cli/test_release_manager.py" in FOCUS_TESTS
    assert "tests/gateway/test_delivery_envelope.py" in FOCUS_TESTS
    assert "tests/test_bitwarden_secrets.py" in FOCUS_TESTS
    assert len(_harness_sha256()) == 64


def test_stage_build_emits_digest_not_raw_tool_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    staging = tmp_path / "releases" / ".staging-test"
    staging.mkdir(parents=True)
    uv = tmp_path / "uv"
    uv.write_text(
        "#!/bin/sh\n"
        "echo raw-output-must-not-survive\n"
        "mkdir -p .venv/bin\n"
        "printf '#!/bin/sh\\nexit 0\\n' > .venv/bin/python\n"
        "chmod 700 .venv/bin/python\n",
        encoding="utf-8",
    )
    uv.chmod(0o700)
    monkeypatch.chdir(staging)

    assert _stage_build(uv) == 0
    rendered = capsys.readouterr().out
    assert "raw-output-must-not-survive" not in rendered
    record = json.loads(rendered)
    assert record["state"] == "passed"
    assert len(record["dependency_output_sha256"]) == 64


def test_stage_build_refuses_non_staging_worktree(tmp_path: Path) -> None:
    with pytest.raises(UpdateBuildError, match="not_release_staging"):
        _stage_build(tmp_path / "uv")
