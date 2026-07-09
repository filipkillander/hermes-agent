"""Tests for copilot auth silent skip of non-Copilot env tokens.

GITHUB_TOKEN and GH_TOKEN are general-purpose GitHub tokens (git, gh CLI, CI).
A classic PAT (ghp_*) in these env vars is the expected case, not a Copilot
misconfiguration — the Copilot resolver probes them opportunistically as a
fallback. Only COPILOT_GITHUB_TOKEN is Copilot-specific, so a classic PAT
there IS a user misconfiguration and should warn.
"""
import logging
from unittest.mock import patch


def _resolve_with_env(env_overrides: dict, caplog):
    """Call resolve_copilot_token with mocked env + no gh CLI fallback."""
    from hermes_cli.copilot_auth import resolve_copilot_token

    base_env = {
        "COPILOT_GITHUB_TOKEN": "",
        "GH_TOKEN": "",
        "GITHUB_TOKEN": "",
    }
    base_env.update(env_overrides)

    with patch.dict("os.environ", base_env, clear=False):
        with patch("hermes_cli.copilot_auth._try_gh_cli_token", return_value=""):
            caplog.set_level(logging.DEBUG, logger="hermes_cli.copilot_auth")
            token, source = resolve_copilot_token()

    return token, source


def test_github_token_classic_pat_skipped_silently(caplog):
    """GITHUB_TOKEN with classic PAT should not log WARNING."""
    token, source = _resolve_with_env(
        {"GITHUB_TOKEN": "ghp_abcdef1234567890"}, caplog
    )

    assert token == ""
    warnings = [
        r for r in caplog.records
        if r.levelno >= logging.WARNING
        and "not supported" in r.getMessage()
    ]
    assert len(warnings) == 0, (
        "GITHUB_TOKEN with classic PAT should not log WARNING — "
        "it's a general-purpose GitHub token, not a Copilot misconfiguration"
    )


def test_gh_token_classic_pat_skipped_silently(caplog):
    """GH_TOKEN with classic PAT should not log WARNING."""
    token, source = _resolve_with_env(
        {"GH_TOKEN": "ghp_abcdef1234567890"}, caplog
    )

    assert token == ""
    warnings = [
        r for r in caplog.records
        if r.levelno >= logging.WARNING
        and "not supported" in r.getMessage()
    ]
    assert len(warnings) == 0


def test_copilot_github_token_classic_pat_still_warns(caplog):
    """COPILOT_GITHUB_TOKEN with classic PAT SHOULD log WARNING."""
    token, source = _resolve_with_env(
        {"COPILOT_GITHUB_TOKEN": "ghp_abcdef1234567890"}, caplog
    )

    assert token == ""
    warnings = [
        r for r in caplog.records
        if r.levelno >= logging.WARNING
        and "not supported" in r.getMessage()
    ]
    assert len(warnings) == 1, (
        "COPILOT_GITHUB_TOKEN with classic PAT SHOULD log WARNING — "
        "the user explicitly set a Copilot-specific env var"
    )
