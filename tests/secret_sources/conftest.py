"""Hermetic fake secret backends shared by production-bootstrap tests."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest


@pytest.fixture
def fake_bws_factory(tmp_path):
    """Create a pinned-version fake ``bws`` executable with static output."""

    def _make(items: list[dict], *, exit_code: int = 0) -> Path:
        path = tmp_path / f"fake-bws-{len(list(tmp_path.iterdir()))}"
        payload = json.dumps(items)
        script = (
            "#!/bin/sh\n"
            "if [ \"$1\" = \"--version\" ]; then echo 'bws 2.0.0'; exit 0; fi\n"
            f"printf '%s' {json.dumps(payload)}\n"
            f"exit {int(exit_code)}\n"
        )
        path.write_text(script, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    return _make


@pytest.fixture
def fake_keychain(monkeypatch):
    """Install a deterministic Keychain reader without touching macOS state."""
    from agent.secret_sources import bitwarden

    calls: list[tuple[str, str]] = []

    def _install(token: str = "fake-bootstrap-token") -> list[tuple[str, str]]:
        def _read(service: str, account: str) -> str:
            calls.append((service, account))
            return token

        monkeypatch.setattr(
            bitwarden, "read_macos_keychain_generic_password", _read
        )
        return calls

    return _install
