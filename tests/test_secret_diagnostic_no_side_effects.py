"""CLI diagnostics must not touch external secret providers or caches."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("argv", [["--version"], ["version"], ["--help"]])
def test_version_and_help_do_not_invoke_bws_or_write_cache(tmp_path, argv):
    home = tmp_path / "profile"
    bin_dir = home / "bin"
    bin_dir.mkdir(parents=True)
    marker = tmp_path / "bws-was-invoked"
    bws = bin_dir / "bws"
    bws.write_text(
        "#!/bin/sh\n"
        f"touch {str(marker)!r}\n"
        "if [ \"$1\" = \"--version\" ]; then echo 'bws 2.0.0'; exit 0; fi\n"
        "printf '[]'\n",
        encoding="utf-8",
    )
    bws.chmod(bws.stat().st_mode | stat.S_IXUSR)
    (home / "config.yaml").write_text(
        "secrets:\n"
        "  bitwarden:\n"
        "    enabled: true\n"
        "    project_id: diagnostic-fixture\n"
        "    access_token_env: DIAGNOSTIC_TOKEN\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.update({
        "HERMES_HOME": str(home),
        "DIAGNOSTIC_TOKEN": "fake-diagnostic-bootstrap",
        "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
    })

    proc = subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", *argv],
        env=env,
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert proc.returncode == 0, proc.stderr
    assert not marker.exists()
    assert not (home / "cache" / "bws_cache.json").exists()
