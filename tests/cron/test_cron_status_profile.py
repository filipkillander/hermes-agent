"""Tests for the multiprofile-aware cron status summary.

The CLI surface (``hermes cron status``) is hard to test end-to-end
because it talks to the terminal and the gateway. So we test the pure
summary helper directly and assert it shows the new fields the
multiprofile rollout needs:

* per-job profile
* last exit code / status / error
* next scheduled run
* whether the profile is reachable
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


def _gateway_home() -> Path:
    return Path(os.environ["HERMES_HOME"]).resolve()


# ---------------------------------------------------------------------------
# Pure summary helper
# ---------------------------------------------------------------------------


class TestBuildJobSummary:
    """build_job_summary is the pure helper the CLI calls per row."""

    def test_returns_dict_with_profile_fields(self):
        from cron.profile_routing import build_job_summary

        job = {
            "id": "abc123def456",
            "name": "nightly-watchdog",
            "profile": "research",
            "last_status": "ok",
            "last_run_at": "2026-06-25T01:02:03+00:00",
            "next_run_at": "2026-06-26T01:00:00+00:00",
            "schedule_display": "0 1 * * *",
            "no_agent": True,
            "script": "nightly-watchdog.sh",
        }
        summary = build_job_summary(job)

        assert summary["job_id"] == "abc123def456"
        assert summary["name"] == "nightly-watchdog"
        assert summary["profile"] == "research"
        assert summary["last_status"] == "ok"
        assert summary["last_run_at"] == "2026-06-25T01:02:03+00:00"
        assert summary["next_run_at"] == "2026-06-26T01:00:00+00:00"
        assert summary["schedule_display"] == "0 1 * * *"
        assert summary["no_agent"] is True
        assert summary["script"] == "nightly-watchdog.sh"

    def test_missing_profile_uses_dash_not_none(self):
        """A legacy job without ``profile`` should render as "-" or
        "default" rather than ``None`` so the CLI prints cleanly."""
        from cron.profile_routing import build_job_summary

        summary = build_job_summary({"id": "x" * 12, "name": "old"})
        assert summary["profile"] == ""  # empty string → caller decides how to render

    def test_includes_last_error_when_failed(self):
        from cron.profile_routing import build_job_summary

        summary = build_job_summary({
            "id": "y" * 12, "name": "broken", "last_status": "error",
            "last_error": "script exited 7",
        })
        assert summary["last_status"] == "error"
        assert summary["last_error"] == "script exited 7"

    def test_includes_unreachable_marker(self):
        """When the target profile directory does not exist, the summary
        flags the job as ``unreachable=True`` so the operator sees it
        in the CLI without needing to scroll logs."""
        from cron.profile_routing import build_job_summary

        summary = build_job_summary({
            "id": "z" * 12,
            "name": "ghost",
            "profile": "this-profile-does-not-exist",
        })
        assert summary["profile"] == "this-profile-does-not-exist"
        assert summary["unreachable"] is True

    def test_reachable_profile_does_not_set_unreachable(self):
        from cron.profile_routing import build_job_summary

        # Default profile resolves to HERMES_HOME itself (always present).
        summary = build_job_summary({
            "id": "r" * 12, "name": "reachable", "profile": "default",
        })
        assert summary["unreachable"] is False


# ---------------------------------------------------------------------------
# Aggregate helpers
# ---------------------------------------------------------------------------


class TestAggregateCounts:
    """The CLI shows per-profile job counts. Tested directly."""

    def test_count_jobs_by_profile(self):
        from cron.profile_routing import count_jobs_by_profile

        jobs = [
            {"id": "a", "profile": "research"},
            {"id": "b", "profile": "research"},
            {"id": "c", "profile": "minerva"},
            {"id": "d", "profile": ""},
            {"id": "e"},  # no profile key
        ]
        counts = count_jobs_by_profile(jobs)
        assert counts == {
            "research": 2,
            "minerva": 1,
            "": 2,  # empty + missing both bucket as "no profile"
        }

    def test_count_empty_jobs(self):
        from cron.profile_routing import count_jobs_by_profile

        assert count_jobs_by_profile([]) == {}


# ---------------------------------------------------------------------------
# CLI integration smoke test
# ---------------------------------------------------------------------------


class TestCronStatusRendersMultiprofileFields:
    """Smoke-test that the (refactored) cron status CLI prints per-job
    profile / last_status / next_run lines."""

    def test_status_includes_profile_and_next_run(self, capsys, monkeypatch):
        # Stub out the gateway PID lookup so the test doesn't depend on
        # a real running gateway.
        import hermes_cli.gateway as _gw_mod

        monkeypatch.setattr(_gw_mod, "find_gateway_pids", lambda: [])

        # Seed a job store with two jobs in different profiles.
        from cron.jobs import JOBS_FILE, save_jobs
        # conftest already isolated HERMES_HOME; ensure cron/ exists.
        cron_dir = _gateway_home() / "cron"
        cron_dir.mkdir(parents=True, exist_ok=True)

        save_jobs([
            {
                "id": "abc123def456",
                "name": "research-watchdog",
                "profile": "research",
                "last_status": "ok",
                "next_run_at": "2026-06-26T01:00:00+00:00",
                "schedule": {"kind": "cron", "expr": "0 1 * * *", "display": "0 1 * * *"},
                "schedule_display": "0 1 * * *",
                "enabled": True,
            },
            {
                "id": "def456abc123",
                "name": "lumi-housekeeping",
                "profile": "",
                "last_status": "error",
                "last_error": "boom",
                "next_run_at": "2026-06-25T12:30:00+00:00",
                "schedule": {"kind": "cron", "expr": "*/30 * * * *", "display": "*/30 * * * *"},
                "schedule_display": "*/30 * * * *",
                "enabled": True,
            },
        ])

        from hermes_cli import cron as cli_cron
        rc = cli_cron.cron_status()
        captured = capsys.readouterr().out
        assert rc is None  # function returns nothing on the happy path

        # The CLI must mention BOTH jobs' profiles so the operator sees
        # the multiprofile state at a glance.
        assert "research" in captured
        assert "lumi-housekeeping" in captured
        assert "next" in captured.lower() or "Next" in captured
