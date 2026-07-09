"""Tests for the secret-source tracking in ``hermes_cli.env_loader``.

These cover the small public surface that lets `hermes model` / `hermes setup`
label detected credentials with their origin ("from Bitwarden") so users
don't see an unexplained "credentials ✓" line when their .env is empty.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hermes_cli import env_loader  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_sources():
    """Each test starts with a clean source map and applied-home guard."""
    env_loader._SECRET_SOURCES.clear()
    env_loader.reset_secret_source_cache()
    yield
    env_loader._SECRET_SOURCES.clear()
    env_loader.reset_secret_source_cache()


def test_get_secret_source_returns_none_for_untracked_var():
    assert env_loader.get_secret_source("ANTHROPIC_API_KEY") is None


def test_get_secret_source_returns_label_for_tracked_var():
    env_loader._SECRET_SOURCES["ANTHROPIC_API_KEY"] = "bitwarden"
    assert env_loader.get_secret_source("ANTHROPIC_API_KEY") == "bitwarden"


def test_format_secret_source_suffix_empty_for_untracked():
    # Credentials from .env or the shell shouldn't add noise — the
    # implicit case stays unlabeled.
    assert env_loader.format_secret_source_suffix("ANTHROPIC_API_KEY") == ""


def test_format_secret_source_suffix_bitwarden_uses_proper_name():
    env_loader._SECRET_SOURCES["ANTHROPIC_API_KEY"] = "bitwarden"
    assert (
        env_loader.format_secret_source_suffix("ANTHROPIC_API_KEY")
        == " (from Bitwarden)"
    )


def test_format_secret_source_suffix_generic_label_for_future_sources():
    # Future-proofing: a new secret source (e.g. "vault") should still
    # produce a sensible label without needing to edit every call site.
    env_loader._SECRET_SOURCES["OPENAI_API_KEY"] = "vault"
    assert (
        env_loader.format_secret_source_suffix("OPENAI_API_KEY")
        == " (from vault)"
    )


def test_format_secret_source_suffix_onepassword_uses_proper_name():
    env_loader._SECRET_SOURCES["OPENAI_API_KEY"] = "onepassword"
    assert (
        env_loader.format_secret_source_suffix("OPENAI_API_KEY")
        == " (from 1Password)"
    )


def test_apply_external_secret_sources_records_bitwarden_origin(tmp_path, monkeypatch):
    """End-to-end: when the Bitwarden source fetches keys, applied vars
    end up in ``_SECRET_SOURCES`` so the UI can label them."""

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("BWS_ACCESS_TOKEN", "0.test-token")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "secrets:\n"
        "  bitwarden:\n"
        "    enabled: true\n"
        "    project_id: test-project\n"
        "    access_token_env: BWS_ACCESS_TOKEN\n",
        encoding="utf-8",
    )

    # Stub the fetch layer under the SecretSource adapter.
    import agent.secret_sources.bitwarden as bw_module

    monkeypatch.setattr(bw_module, "find_bws", lambda **_kw: Path("/fake/bws"))
    monkeypatch.setattr(
        bw_module,
        "fetch_bitwarden_secrets",
        lambda **_kw: ({"ANTHROPIC_API_KEY": "sk-ant-test"}, []),
    )

    from agent.secret_sources import registry as reg_module

    reg_module._reset_registry_for_tests()

    env_loader._apply_external_secret_sources(tmp_path)

    assert env_loader.get_secret_source("ANTHROPIC_API_KEY") == "bitwarden"
    assert (
        env_loader.format_secret_source_suffix("ANTHROPIC_API_KEY")
        == " (from Bitwarden)"
    )


def test_apply_external_secret_sources_noop_when_disabled(tmp_path, monkeypatch):
    """Disabled Bitwarden config must not touch the source map."""

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "secrets:\n"
        "  bitwarden:\n"
        "    enabled: false\n",
        encoding="utf-8",
    )

    env_loader._apply_external_secret_sources(tmp_path)

    assert env_loader.get_secret_source("ANTHROPIC_API_KEY") is None


def test_apply_external_secret_sources_dedupes_within_process(tmp_path, monkeypatch):
    """``load_hermes_dotenv()`` is called at module-import time from several
    hot modules (cli.py, hermes_cli/main.py, run_agent.py, ...).  The
    Bitwarden status line previously printed once per call — 3-5x per
    startup.  The applied-home guard must short-circuit subsequent calls
    so the heavy work (config re-parse, Bitwarden lookup, status print)
    runs exactly once per HERMES_HOME per process.
    """

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("BWS_ACCESS_TOKEN", "0.test-token")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "secrets:\n"
        "  bitwarden:\n"
        "    enabled: true\n"
        "    project_id: test-project\n"
        "    access_token_env: BWS_ACCESS_TOKEN\n",
        encoding="utf-8",
    )

    call_count = {"n": 0}

    def _fake_fetch(**_kwargs):
        call_count["n"] += 1
        return {"ANTHROPIC_API_KEY": "sk-ant-test"}, []

    import agent.secret_sources.bitwarden as bw_module
    monkeypatch.setattr(bw_module, "find_bws", lambda **_kw: Path("/fake/bws"))
    monkeypatch.setattr(bw_module, "fetch_bitwarden_secrets", _fake_fetch)

    from agent.secret_sources import registry as reg_module

    reg_module._reset_registry_for_tests()

    # Five calls in a row, simulating module-import-time invocations from
    # cli.py, hermes_cli/main.py, run_agent.py, trajectory_compressor.py,
    # gateway/run.py.  Only the first should actually call the backend.
    for _ in range(5):
        env_loader._apply_external_secret_sources(tmp_path)

    assert call_count["n"] == 1, (
        "Bitwarden backend was called {} time(s); expected exactly 1 — "
        "the applied-home guard is broken.".format(call_count["n"])
    )

    # Source tracking still works after dedup.
    assert env_loader.get_secret_source("ANTHROPIC_API_KEY") == "bitwarden"

    # reset_secret_source_cache() forces a fresh pull on the next call.
    env_loader.reset_secret_source_cache()
    env_loader._apply_external_secret_sources(tmp_path)
    assert call_count["n"] == 2


def test_apply_external_secret_sources_records_onepassword_origin(tmp_path, monkeypatch):
    """When the 1Password source resolves refs, applied vars end up in
    ``_SECRET_SOURCES`` labeled ``onepassword``."""

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    (tmp_path / "config.yaml").write_text(
        "secrets:\n"
        "  onepassword:\n"
        "    enabled: true\n"
        "    env:\n"
        "      ANTHROPIC_API_KEY: 'op://Private/Anthropic/credential'\n",
        encoding="utf-8",
    )

    import agent.secret_sources.onepassword as op_module

    monkeypatch.setattr(op_module, "find_op", lambda *_a, **_kw: Path("/fake/op"))
    monkeypatch.setattr(
        op_module,
        "fetch_onepassword_secrets",
        lambda **_kw: ({"ANTHROPIC_API_KEY": "sk-ant-test"}, []),
    )

    from agent.secret_sources import registry as reg_module

    reg_module._reset_registry_for_tests()

    env_loader._apply_external_secret_sources(tmp_path)

    assert env_loader.get_secret_source("ANTHROPIC_API_KEY") == "onepassword"
    assert (
        env_loader.format_secret_source_suffix("ANTHROPIC_API_KEY")
        == " (from 1Password)"
    )


def test_apply_external_secret_sources_survives_non_dict_section(tmp_path, monkeypatch):
    """A malformed `secrets:` section must not abort startup (fail-open).

    Both `onepassword: true` (non-dict) and a bad bitwarden section must be
    coerced to empty config instead of raising AttributeError up through
    load_hermes_dotenv().
    """

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "secrets:\n"
        "  bitwarden: true\n"
        "  onepassword: true\n",
        encoding="utf-8",
    )

    # Must not raise and must not record anything.
    env_loader._apply_external_secret_sources(tmp_path)
    assert env_loader.get_secret_source("ANYTHING") is None


def test_apply_external_secret_sources_bad_ttl_does_not_crash(tmp_path, monkeypatch):
    """A non-numeric cache_ttl_seconds must be coerced, not crash startup."""

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "secrets:\n"
        "  onepassword:\n"
        "    enabled: true\n"
        "    cache_ttl_seconds: not-a-number\n"
        "    env:\n"
        "      K: 'op://V/I/F'\n",
        encoding="utf-8",
    )

    captured = {}

    def _fake_fetch(**kwargs):
        captured.update(kwargs)
        return {}, []

    import agent.secret_sources.onepassword as op_module
    monkeypatch.setattr(op_module, "find_op", lambda *_a, **_kw: Path("/fake/op"))
    monkeypatch.setattr(op_module, "fetch_onepassword_secrets", _fake_fetch)

    from agent.secret_sources import registry as reg_module

    reg_module._reset_registry_for_tests()

    env_loader._apply_external_secret_sources(tmp_path)

    # Coerced to the 300s default rather than raising ValueError.
    assert captured["cache_ttl_seconds"] == 300


def test_failed_fetch_remains_retryable_until_success(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text(
        "secrets:\n  bitwarden:\n    enabled: true\n    project_id: fake\n",
        encoding="utf-8",
    )
    from agent.secret_sources import registry as reg
    from agent.secret_sources.base import ErrorKind, FetchResult

    calls = {"n": 0}

    def _apply(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            failed = FetchResult(
                error="sensitive backend diagnostic",
                error_kind=ErrorKind.NETWORK,
            )
            return reg.ApplyReport(sources=[
                reg.SourceReport("bitwarden", "Bitwarden", failed)
            ])
        return reg.ApplyReport()

    monkeypatch.setattr(reg, "apply_all", _apply)
    key = str(tmp_path.resolve())

    env_loader._apply_external_secret_sources(tmp_path)
    assert key not in env_loader._APPLIED_HOMES
    env_loader._apply_external_secret_sources(tmp_path)
    assert calls["n"] == 2
    assert key in env_loader._APPLIED_HOMES


def test_startup_logging_is_count_only(tmp_path, monkeypatch, capsys):
    (tmp_path / "config.yaml").write_text(
        "secrets:\n  bitwarden:\n    enabled: true\n",
        encoding="utf-8",
    )
    from agent.secret_sources import registry as reg
    from agent.secret_sources.base import ErrorKind, FetchResult

    result = FetchResult(
        warnings=["SECRET_NAME had forbidden value super-secret-value"]
    )
    source = reg.SourceReport(
        "bitwarden", "Bitwarden", result,
        applied=["SECRET_NAME"],
    )
    report = reg.ApplyReport(
        sources=[source],
        conflicts=["SECRET_NAME conflicts with super-secret-value"],
    )
    monkeypatch.setattr(reg, "apply_all", lambda *a, **k: report)

    env_loader._apply_external_secret_sources(tmp_path)
    stderr = capsys.readouterr().err
    assert "applied 1 secret" in stderr
    assert "1 warning suppressed" in stderr
    assert "1 conflict suppressed" in stderr
    assert "SECRET_NAME" not in stderr
    assert "super-secret-value" not in stderr


def test_read_only_diagnostic_skips_secret_bootstrap_and_file_repair(
    tmp_path, monkeypatch
):
    (tmp_path / ".env").write_text("DIAGNOSTIC_ONLY=value\n", encoding="utf-8")
    touched = {"bootstrap": 0, "repair": 0}
    monkeypatch.setenv("HERMES_SKIP_EXTERNAL_SECRETS", "1")
    monkeypatch.setenv("HERMES_ENV_LOADER_READ_ONLY", "1")
    monkeypatch.setattr(
        env_loader, "_apply_external_secret_sources",
        lambda *_a, **_k: touched.__setitem__("bootstrap", touched["bootstrap"] + 1),
    )
    monkeypatch.setattr(
        env_loader, "_sanitize_env_file_if_needed",
        lambda *_a, **_k: touched.__setitem__("repair", touched["repair"] + 1),
    )

    env_loader.load_hermes_dotenv(hermes_home=tmp_path)
    assert touched == {"bootstrap": 0, "repair": 0}


def test_external_secret_readiness_is_count_only(tmp_path, monkeypatch):
    from agent.secret_sources.base import FetchResult, SecretSource
    from agent.secret_sources.registry import _reset_registry_for_tests, register_source

    class ReadySource(SecretSource):
        name = "ready_source"
        label = "Ready source"

        def fetch(self, cfg, home_path):
            return FetchResult(secrets={"PRIVATE_PROVIDER_KEY": "must-not-appear"})

    _reset_registry_for_tests()
    register_source(ReadySource())
    (tmp_path / "config.yaml").write_text(
        "secrets:\n"
        "  ready_source:\n"
        "    enabled: true\n"
        "    allowed_env_vars: [PRIVATE_PROVIDER_KEY]\n"
        "    required_env_vars: [PRIVATE_PROVIDER_KEY]\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("PRIVATE_PROVIDER_KEY", raising=False)
    env_loader._apply_external_secret_sources(tmp_path)

    status = env_loader.get_external_secret_readiness(tmp_path)
    assert status == {
        "state": "ready",
        "ready": True,
        "enabled_sources": 1,
        "applied_count": 1,
        "failed_sources": 0,
        "missing_required_count": 0,
    }
    serialized = repr(status)
    assert "PRIVATE_PROVIDER_KEY" not in serialized
    assert "must-not-appear" not in serialized


def test_external_secret_readiness_marks_policy_failure(tmp_path):
    from agent.secret_sources.base import FetchResult, SecretSource
    from agent.secret_sources.registry import _reset_registry_for_tests, register_source

    class MissingSource(SecretSource):
        name = "missing_source"
        label = "Missing source"

        def fetch(self, cfg, home_path):
            return FetchResult(secrets={})

    _reset_registry_for_tests()
    register_source(MissingSource())
    (tmp_path / "config.yaml").write_text(
        "secrets:\n"
        "  missing_source:\n"
        "    enabled: true\n"
        "    required_env_vars: [NEEDED_KEY]\n",
        encoding="utf-8",
    )
    env_loader._apply_external_secret_sources(tmp_path)

    status = env_loader.get_external_secret_readiness(tmp_path)
    assert status["state"] == "degraded"
    assert status["ready"] is False
    assert status["failed_sources"] == 1
    assert status["missing_required_count"] == 1
