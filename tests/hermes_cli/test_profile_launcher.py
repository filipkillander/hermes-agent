from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "hermes-profile-launcher.py"
SPEC = importlib.util.spec_from_file_location("hermes_profile_launcher", SCRIPT)
assert SPEC and SPEC.loader
launcher = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = launcher
SPEC.loader.exec_module(launcher)


def _runtime(tmp_path: Path, profile: str = "spark") -> tuple[Path, Path]:
    root = tmp_path / ".hermes"
    release = root / "releases" / "r1"
    python = release / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\n", encoding="utf-8")
    python.chmod(0o755)
    (release / ".hermes-release.json").write_text(
        json.dumps({"release_id": "r1", "commit": "a" * 40}),
        encoding="utf-8",
    )
    links = root / "runtime-links" / profile
    links.mkdir(parents=True)
    (links / "current").symlink_to(release)
    (root / "profiles" / profile).mkdir(parents=True)
    (root / "profiles" / profile / "config.yaml").write_text("model: test\n", encoding="utf-8")
    (root / "runtime-registry.yaml").write_text("schema_version: 1\nprofiles: {}\n", encoding="utf-8")
    key = root / "control-plane/bot-fingerprint.key"
    key.parent.mkdir(parents=True)
    key.write_bytes(b"k" * 32)
    key.chmod(0o600)
    return root, release


def test_resolve_launch_pins_profile_release_and_clean_path(tmp_path: Path) -> None:
    root, release = _runtime(tmp_path)
    inherited = {
        "PATH": f"{root}/hermes-agent/venv/bin:/usr/local/bin:/usr/bin",
        "PYTHONPATH": "/unsafe/live/source",
    }
    spec = launcher.resolve_launch("spark", root, inherited)
    assert spec.release == release
    assert spec.env["HERMES_HOME"] == str(root / "profiles/spark")
    assert spec.env["HERMES_RELEASE_REVISION"] == "a" * 40
    assert spec.env["HERMES_RUNTIME_REGISTRY"] == str(root / "runtime-registry.yaml")
    assert spec.env["HERMES_BOT_FINGERPRINT_KEY_FILE"].endswith("bot-fingerprint.key")
    assert "hermes-agent" not in spec.env["PATH"]
    assert spec.env["PATH"].startswith(str(release / ".venv/bin"))
    assert "PYTHONPATH" not in spec.env
    assert spec.argv[-3:] == ("spark", "gateway", "run")


def test_dashboard_launch_uses_same_pinned_release(tmp_path: Path) -> None:
    root, release = _runtime(tmp_path, profile="lumi")
    spec = launcher.resolve_launch(
        "lumi",
        root,
        {"PATH": "/usr/bin:/bin"},
        service="dashboard",
        service_args=("--host", "127.0.0.1", "--port", "9119", "--no-open", "--skip-build"),
    )
    assert spec.service == "dashboard"
    assert spec.release == release
    assert spec.argv[-7:] == (
        "dashboard",
        "--host",
        "127.0.0.1",
        "--port",
        "9119",
        "--no-open",
        "--skip-build",
    )


def test_gateway_rejects_passthrough_arguments(tmp_path: Path) -> None:
    root, _ = _runtime(tmp_path)
    with pytest.raises(launcher.LaunchRejected, match="passthrough"):
        launcher.resolve_launch(
            "spark",
            root,
            {"PATH": "/usr/bin:/bin"},
            service_args=("--replace",),
        )


@pytest.mark.parametrize("failure", ["missing-link", "outside-link", "open-key", "missing-venv"])
def test_launcher_fails_closed_on_invalid_runtime(tmp_path: Path, failure: str) -> None:
    root, release = _runtime(tmp_path)
    current = root / "runtime-links/spark/current"
    if failure == "missing-link":
        current.unlink()
    elif failure == "outside-link":
        current.unlink()
        current.symlink_to(tmp_path)
    elif failure == "open-key":
        (root / "control-plane/bot-fingerprint.key").chmod(0o644)
    else:
        (release / ".venv/bin/python").unlink()
    with pytest.raises(launcher.LaunchRejected):
        launcher.resolve_launch("spark", root, {"PATH": "/usr/bin"})


def test_check_mode_is_side_effect_free_and_non_secret(tmp_path: Path) -> None:
    root, _ = _runtime(tmp_path)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "spark", "--root", str(root), "--check"],
        capture_output=True,
        text=True,
        check=False,
        env={"HOME": str(tmp_path), "PATH": os.environ.get("PATH", "/usr/bin:/bin")},
    )
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["profile"] == "spark"
    assert payload["service"] == "gateway"
    assert "token" not in result.stdout.lower()
