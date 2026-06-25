"""Tests for cron/profile_routing.py — multiprofile job routing.

Lumi gateway owns the master job store but jobs declare a ``profile`` field
that decides which Hermes profile actually executes the job body. This
module is the pure-logic helper that:

* resolves a profile name to its HERMES_HOME path,
* resolves a script path against the target profile's scripts dir (with a
  safe fallback to the gateway profile when the script is missing in the
  target profile),
* detects whether the gateway is currently running in a given profile, and
* provides the structured ``route_decision`` used by the scheduler to pick
  between in-process execution and delegation.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


def _gateway_home() -> Path:
    """Return the gateway's HERMES_HOME for the current test.

    Each test gets ``HERMES_HOME=<tmp>/hermes_test`` via the autouse
    ``_hermetic_environment`` fixture. We work inside that directory so
    code under test that calls ``get_hermes_home()`` sees a consistent
    layout.
    """
    return Path(os.environ["HERMES_HOME"]).resolve()


def _hermes_root() -> Path:
    """The Hermes root for the current test = HERMES_HOME itself, since
    conftest uses a private tempdir not under ~/.hermes."""
    return _gateway_home()


# ---------------------------------------------------------------------------
# Profile-home resolution
# ---------------------------------------------------------------------------


class TestResolveProfileHome:
    """resolve_profile_home maps a profile name to <hermes_root>/profiles/<name>."""

    def test_known_profile_returns_profile_dir(self):
        """A profile name resolves to <hermes_root>/profiles/<name>."""
        (_hermes_root() / "profiles" / "research").mkdir(parents=True, exist_ok=True)

        from cron.profile_routing import resolve_profile_home

        result = resolve_profile_home("research")
        assert result == (_hermes_root() / "profiles" / "research").resolve()

    def test_known_profile_creates_dir(self):
        """If the profile directory doesn't exist, it is created so the
        scheduler can write runs/ + output/ there without crashing."""
        from cron.profile_routing import resolve_profile_home

        result = resolve_profile_home("review")
        assert result.is_dir()
        assert result.name == "review"
        # Profiles live under <hermes_root>/profiles/.
        assert result.parent == _hermes_root() / "profiles"

    def test_default_profile_returns_hermes_root(self):
        """``profile=default`` and ``profile=lumi`` both resolve to the
        gateway's own HERMES_HOME — they should not nest into a profiles
        subdir that may not exist."""
        from cron.profile_routing import resolve_profile_home

        assert resolve_profile_home("default") == _gateway_home()
        assert resolve_profile_home("lumi") == _gateway_home()

    def test_empty_profile_returns_current(self):
        """Empty / missing profile falls back to the gateway's own
        HERMES_HOME — preserves backwards compat for legacy jobs without
        a profile field."""
        from cron.profile_routing import resolve_profile_home

        assert resolve_profile_home("") == _gateway_home()
        assert resolve_profile_home(None) == _gateway_home()

    def test_normalizes_case_and_whitespace(self):
        """Profile names are case-insensitive and whitespace-tolerant."""
        (_hermes_root() / "profiles" / "research").mkdir(parents=True, exist_ok=True)
        from cron.profile_routing import resolve_profile_home

        assert resolve_profile_home("Research") == (_hermes_root() / "profiles" / "research").resolve()
        assert resolve_profile_home("  research  ") == (_hermes_root() / "profiles" / "research").resolve()

    def test_rejects_path_traversal(self):
        """Profile names with path separators or traversal components are
        rejected — they would let a hand-edited jobs.json escape the
        profiles/ sandbox and target arbitrary paths on disk."""
        from cron.profile_routing import resolve_profile_home, ProfileRoutingError

        for bad in ("../escape", "research/..", "research/sub", "/etc/passwd", ".."):
            with pytest.raises(ProfileRoutingError):
                resolve_profile_home(bad)


# ---------------------------------------------------------------------------
# Active profile detection
# ---------------------------------------------------------------------------


class TestActiveProfileName:
    """get_active_profile_name returns the profile the current process is
    running in (i.e. HERMES_HOME's parent, or "default" if at the root)."""

    def test_returns_default_when_at_hermes_root(self, monkeypatch):
        """HERMES_HOME=<hermes_root> → "default"."""
        from cron.profile_routing import get_active_profile_name

        # The conftest fixture puts HERMES_HOME at <tmp>/hermes_test which
        # is itself the Hermes root in test mode.
        assert get_active_profile_name() == "default"

    def test_returns_profile_name_when_inside_profiles(self, monkeypatch):
        """HERMES_HOME=<hermes_root>/profiles/research → "research"."""
        prof = _hermes_root() / "profiles" / "research"
        prof.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("HERMES_HOME", str(prof))
        from cron.profile_routing import get_active_profile_name

        assert get_active_profile_name() == "research"

    def test_returns_lumi_for_lumi_profile(self, monkeypatch):
        """HERMES_HOME=<hermes_root>/profiles/lumi → "lumi"."""
        prof = _hermes_root() / "profiles" / "lumi"
        prof.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("HERMES_HOME", str(prof))
        from cron.profile_routing import get_active_profile_name

        assert get_active_profile_name() == "lumi"


# ---------------------------------------------------------------------------
# Script-path resolution against the target profile
# ---------------------------------------------------------------------------


class TestResolveScriptPath:
    """resolve_script_path chooses where to look for a job's script.

    Contract:
    * Relative paths resolve first against the target profile's scripts/
      dir, then against the gateway profile's scripts/ dir as a fallback
      (so jobs that lived entirely in the gateway don't suddenly break
      when re-routed).
    * Absolute paths must stay within the target profile's scripts/ dir
      (same sandbox contract as the existing _run_job_script) — paths
      outside it are rejected, paths inside it are returned as-is.
    * Missing scripts raise ScriptNotFoundError carrying both candidate
      locations so the scheduler log can surface them.
    """

    def test_relative_resolves_in_target_profile(self):
        """A script that exists in the target profile's scripts/ dir is
        preferred even if a same-named file exists in the gateway dir."""
        target = _hermes_root() / "profiles" / "research" / "scripts"
        target.mkdir(parents=True, exist_ok=True)
        (target / "watchdog.sh").write_text("#!/bin/bash\necho ok\n")
        # Also create a same-named file in the gateway dir to confirm
        # the target wins.
        gateway_scripts = _gateway_home() / "scripts"
        gateway_scripts.mkdir(parents=True, exist_ok=True)
        (gateway_scripts / "watchdog.sh").write_text("#!/bin/bash\necho gateway\n")

        from cron.profile_routing import resolve_script_path

        path = resolve_script_path("research", "watchdog.sh")
        assert path == (target / "watchdog.sh").resolve()

    def test_relative_falls_back_to_gateway_profile(self):
        """If the script only exists in the gateway profile's scripts/,
        the scheduler runs it from there (legacy behaviour preserved)."""
        gateway_scripts = _gateway_home() / "scripts"
        gateway_scripts.mkdir(parents=True, exist_ok=True)
        (gateway_scripts / "legacy.sh").write_text("#!/bin/bash\n")
        # Target profile's scripts/ exists but is empty.
        (_hermes_root() / "profiles" / "research" / "scripts").mkdir(parents=True, exist_ok=True)

        from cron.profile_routing import resolve_script_path

        path = resolve_script_path("research", "legacy.sh")
        assert path == (gateway_scripts / "legacy.sh").resolve()

    def test_relative_skips_target_if_missing_script_dir(self):
        """If the target profile has no scripts/ at all, fall through to
        the gateway profile without raising — the script may legitimately
        live only in the gateway."""
        gateway_scripts = _gateway_home() / "scripts"
        gateway_scripts.mkdir(parents=True, exist_ok=True)
        (gateway_scripts / "only-in-gateway.sh").write_text("#!/bin/bash\n")
        # No profiles/research/scripts/ dir created.

        from cron.profile_routing import resolve_script_path

        path = resolve_script_path("research", "only-in-gateway.sh")
        assert path == (gateway_scripts / "only-in-gateway.sh").resolve()

    def test_absolute_inside_target_profile_allowed(self):
        """Absolute scripts that live INSIDE the target profile's
        scripts/ dir are accepted as-is."""
        target = _hermes_root() / "profiles" / "research" / "scripts"
        target.mkdir(parents=True, exist_ok=True)
        script = target / "explicit.sh"
        script.write_text("#!/bin/bash\n")

        from cron.profile_routing import resolve_script_path

        path = resolve_script_path("research", str(script))
        assert path == script.resolve()

    def test_absolute_outside_target_profile_rejected(self):
        """Absolute scripts outside the target profile's scripts/ are
        rejected — this prevents a hand-edited job from running an
        arbitrary file under a different profile's identity."""
        target = _hermes_root() / "profiles" / "research" / "scripts"
        target.mkdir(parents=True, exist_ok=True)
        outside = _hermes_root() / "somewhere_else" / "evil.sh"
        outside.parent.mkdir(parents=True, exist_ok=True)
        outside.write_text("#!/bin/bash\n")

        from cron.profile_routing import resolve_script_path, ProfileRoutingError

        with pytest.raises(ProfileRoutingError):
            resolve_script_path("research", str(outside))

    def test_missing_script_raises_with_both_candidates(self):
        """ScriptNotFoundError names BOTH candidate locations so the
        scheduler can log a clear actionable error."""
        (_gateway_home() / "scripts").mkdir(parents=True, exist_ok=True)
        (_hermes_root() / "profiles" / "research" / "scripts").mkdir(parents=True, exist_ok=True)

        from cron.profile_routing import resolve_script_path, ScriptNotFoundError

        with pytest.raises(ScriptNotFoundError) as exc:
            resolve_script_path("research", "nope.sh")
        msg = str(exc.value)
        assert "nope.sh" in msg
        assert "profiles/research/scripts" in msg or "scripts" in msg


# ---------------------------------------------------------------------------
# Routing decision
# ---------------------------------------------------------------------------


class TestDecideRouting:
    """decide_routing picks between in-process and delegated execution.

    Rules:
    * profile empty / same as gateway → IN_PROCESS (legacy fast path).
    * profile different from gateway → DELEGATE.
    * profile refers to a missing dir → UNREACHABLE (don't silently fall
      back to gateway — the operator explicitly asked for a different
      profile and silently swallowing that would defeat the whole point).
    """

    def test_no_profile_means_in_process(self):
        from cron.profile_routing import decide_routing

        d = decide_routing({})
        assert d.action == "in_process"
        # Empty profile stays empty so logs read "profile=unset" rather
        # than the misleadingly-precise "profile=default".
        assert d.profile == ""
        assert d.reason  # human-readable

    def test_same_profile_means_in_process(self, monkeypatch):
        # Active profile = "lumi" via setting HERMES_HOME to profiles/lumi.
        # get_default_hermes_root walks up to find the parent with a
        # ``profiles`` child, so the root is the grandparent of the
        # HERMES_HOME we're about to set.
        gateway = _gateway_home()
        prof = gateway.parent / "profiles" / "lumi"
        prof.mkdir(parents=True, exist_ok=True)
        (gateway.parent / "profiles").mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("HERMES_HOME", str(prof))

        from cron.profile_routing import decide_routing

        d = decide_routing({"profile": "lumi"})
        assert d.action == "in_process"
        assert d.profile == "lumi"

    def test_different_profile_means_delegate(self, monkeypatch):
        gateway = _gateway_home()
        prof = gateway.parent / "profiles" / "lumi"
        prof.mkdir(parents=True, exist_ok=True)
        (gateway.parent / "profiles").mkdir(parents=True, exist_ok=True)
        target = gateway.parent / "profiles" / "research"
        target.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("HERMES_HOME", str(prof))

        from cron.profile_routing import decide_routing

        d = decide_routing({"profile": "research"})
        assert d.action == "delegate"
        assert d.profile == "research"
        assert d.target_home == target.resolve()

    def test_missing_target_means_unreachable(self, monkeypatch):
        gateway = _gateway_home()
        prof = gateway.parent / "profiles" / "lumi"
        prof.mkdir(parents=True, exist_ok=True)
        (gateway.parent / "profiles").mkdir(parents=True, exist_ok=True)
        # research dir is NOT created
        monkeypatch.setenv("HERMES_HOME", str(prof))

        from cron.profile_routing import decide_routing

        d = decide_routing({"profile": "research"})
        assert d.action == "unreachable"
        assert d.profile == "research"
        assert d.target_home is None
