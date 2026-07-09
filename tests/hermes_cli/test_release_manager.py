from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli.release_manager import ImmutableReleaseManager, ReleaseError, run_probe


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _repo(tmp_path: Path, files: dict[str, str] | None = None) -> Path:
    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "tests@example.invalid")
    _git(repo, "config", "user.name", "Tests")
    for name, content in (files or {"app.py": "print('ok')\n"}).items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "fixture")
    return repo


def test_stage_is_immutable_secret_free_and_verifiable(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"app.py": "print('ok')\n", "bin/run": "#!/bin/sh\nexit 0\n"})
    (repo / "bin/run").chmod(0o755)
    _git(repo, "add", "bin/run")
    _git(repo, "commit", "-qm", "executable")
    manager = ImmutableReleaseManager(tmp_path / "home")

    release = manager.stage(repo, "r1")
    manifest = manager.verify("r1")

    assert manifest["release_id"] == "r1"
    assert manifest["commit"] == _git(repo, "rev-parse", "HEAD")
    assert stat_mode(release) == 0o555
    assert stat_mode(release / "app.py") == 0o444
    assert stat_mode(release / "bin/run") == 0o555
    assert not list(release.rglob(".env"))


@pytest.mark.parametrize("name", [".env", "auth.json", "nested/bws_cache.json", "client_secret_test.json"])
def test_stage_rejects_tracked_forbidden_entries(tmp_path: Path, name: str) -> None:
    repo = _repo(tmp_path, {"app.py": "ok\n", name: "not-a-real-secret\n"})
    manager = ImmutableReleaseManager(tmp_path / "home")
    with pytest.raises(ReleaseError, match="forbidden"):
        manager.stage(repo, "r1")


def test_verify_detects_release_tampering(tmp_path: Path) -> None:
    manager = ImmutableReleaseManager(tmp_path / "home")
    release = manager.stage(_repo(tmp_path), "r1")
    (release / "app.py").chmod(0o600)
    (release / "app.py").write_text("changed\n", encoding="utf-8")
    with pytest.raises(ReleaseError, match="checksum"):
        manager.verify("r1")


def test_stage_can_build_inside_release_before_sealing(tmp_path: Path) -> None:
    manager = ImmutableReleaseManager(tmp_path / "home")
    release = manager.stage(
        _repo(tmp_path),
        "r1",
        build=[sys.executable, "-c", "from pathlib import Path; Path('built.txt').write_text('ok')"],
    )
    assert (release / "built.txt").read_text(encoding="utf-8") == "ok"
    assert stat_mode(release / "built.txt") == 0o444
    assert "built.txt" in manager.verify("r1")["files"]


def test_failed_build_leaves_no_release(tmp_path: Path) -> None:
    manager = ImmutableReleaseManager(tmp_path / "home")
    with pytest.raises(ReleaseError, match="release build failed"):
        manager.stage(
            _repo(tmp_path),
            "r1",
            build=[sys.executable, "-c", "raise SystemExit(7)"],
        )
    assert not manager.release_path("r1").exists()


def test_release_size_budget_fails_closed(tmp_path: Path) -> None:
    manager = ImmutableReleaseManager(tmp_path / "home")
    with pytest.raises(ReleaseError, match="size budget"):
        manager.stage(_repo(tmp_path), "r1", max_bytes=1)
    assert not manager.release_path("r1").exists()


def test_snapshot_is_allowlisted_private_and_rejects_secrets(tmp_path: Path) -> None:
    home = tmp_path / "home"
    config = home / "profiles/lumi/config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("profile: lumi\n", encoding="utf-8")
    secret = home / "profiles/lumi/.env"
    secret.write_text("TOKEN=fake\n", encoding="utf-8")
    manager = ImmutableReleaseManager(home)

    snapshot = manager.snapshot("s1", [config])
    assert stat_mode(snapshot) == 0o700
    assert stat_mode(snapshot / "profiles/lumi/config.yaml") == 0o600
    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    assert list(manifest["files"]) == ["profiles/lumi/config.yaml"]
    with pytest.raises(ReleaseError, match="forbidden"):
        manager.snapshot("s2", [secret])
    with pytest.raises(ReleaseError, match="size budget"):
        manager.snapshot("s3", [config], max_bytes=1)


def test_promote_tracks_previous_and_rollback_swaps(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    manager = ImmutableReleaseManager(tmp_path / "home")
    r1 = manager.stage(repo, "r1")
    (repo / "app.py").write_text("print('new')\n", encoding="utf-8")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-qm", "new")
    r2 = manager.stage(repo, "r2")

    manager.promote("spark", "r1")
    state = manager.promote("spark", "r2")
    assert Path(state["current"]) == r2
    assert Path(state["previous"]) == r1
    rolled = manager.rollback("spark")
    assert Path(rolled["current"]) == r1
    assert Path(rolled["previous"]) == r2


def test_failed_postflight_restores_old_pointer(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    manager = ImmutableReleaseManager(tmp_path / "home")
    r1 = manager.stage(repo, "r1")
    (repo / "app.py").write_text("new\n", encoding="utf-8")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-qm", "new")
    manager.stage(repo, "r2")
    manager.promote("lumi", "r1")

    with pytest.raises(ReleaseError, match="postflight"):
        manager.promote("lumi", "r2", postflight=[sys.executable, "-c", "raise SystemExit(9)"])
    current = (manager.profile_dir("lumi") / "current").resolve()
    assert current == r1


def test_preflight_failure_never_creates_current_link(tmp_path: Path) -> None:
    manager = ImmutableReleaseManager(tmp_path / "home")
    manager.stage(_repo(tmp_path), "r1")
    with pytest.raises(ReleaseError, match="preflight"):
        manager.promote("lumi", "r1", preflight=[sys.executable, "-c", "raise SystemExit(2)"])
    assert not (manager.profile_dir("lumi") / "current").exists()


def test_probe_uses_no_shell_and_minimal_environment(tmp_path: Path) -> None:
    marker = tmp_path / "should-not-exist"
    result = run_probe([sys.executable, "-c", "import os; print(sorted(os.environ))", ";", "touch", str(marker)])
    assert result.returncode == 0
    assert not marker.exists()
    assert "BWS_ACCESS_TOKEN" not in result.stdout


def test_invalid_profile_and_release_ids_are_rejected(tmp_path: Path) -> None:
    manager = ImmutableReleaseManager(tmp_path / "home")
    with pytest.raises(ReleaseError, match="release id"):
        manager.release_path("../escape")
    with pytest.raises(ReleaseError, match="profile"):
        manager.profile_dir("bad/profile")


def test_prune_is_dry_run_and_never_removes_linked_releases(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    manager = ImmutableReleaseManager(tmp_path / "home")
    for index in range(1, 6):
        if index > 1:
            (repo / "app.py").write_text(f"print({index})\n", encoding="utf-8")
            _git(repo, "add", "app.py")
            _git(repo, "commit", "-qm", f"r{index}")
        manager.stage(repo, f"r{index}")
    manager.promote("spark", "r1")
    manager.promote("spark", "r2")

    planned = manager.prune(keep=2)
    assert planned == ["r3"]
    assert manager.release_path("r3").exists()
    applied = manager.prune(keep=2, apply=True)
    assert applied == ["r3"]
    assert not manager.release_path("r3").exists()
    assert manager.release_path("r1").exists()
    assert manager.release_path("r2").exists()
    assert manager.release_path("r4").exists()
    assert manager.release_path("r5").exists()


def test_prune_refuses_unsafe_retention(tmp_path: Path) -> None:
    manager = ImmutableReleaseManager(tmp_path / "home")
    with pytest.raises(ReleaseError, match="at least two"):
        manager.prune(keep=1)


def stat_mode(path: Path) -> int:
    return os.stat(path, follow_symlinks=False).st_mode & 0o777
