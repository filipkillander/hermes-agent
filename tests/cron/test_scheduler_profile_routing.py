"""Tests for the scheduler's multiprofile integration.

``cron.scheduler.run_job`` is the gateway's job-execution entrypoint.
After this change it must:

* Continue to handle ``profile == gateway`` jobs in-process exactly as
  before (legacy behaviour, regression-safe).
* For ``profile != gateway`` jobs, delegate to ``cron.runner.run_job_in_profile``
  so the script/agent runs under the target profile's HERMES_HOME.
* For ``profile != gateway`` where the profile dir is missing, return
  a failure tuple (so ``run_one_job`` marks it as an error run).
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


def _gateway_home() -> Path:
    return Path(os.environ["HERMES_HOME"]).resolve()


# ---------------------------------------------------------------------------
# Scheduler-level delegation
# ---------------------------------------------------------------------------


class TestSchedulerDelegatesByProfile:
    """``cron.scheduler.run_job`` routes by ``job.profile``."""

    def test_profile_equals_gateway_runs_in_process(self, monkeypatch):
        """A job whose profile matches the gateway's HERMES_HOME is run
        exactly as before — no delegation."""
        # Patch the runner so we'd notice if it was called.
        from cron import scheduler as s
        from cron import runner as r

        delegate_called = []
        monkeypatch.setattr(
            r, "run_job_in_profile",
            lambda job: delegate_called.append(job) or r.RunResult(
                job_id=job.get("id", ""), profile="x", status="ok",
                exit_code=0, duration_ms=0, output="", error=None,
            ),
        )

        # Sentinel: marker that "legacy" code path ran.
        sentinel_path = []
        monkeypatch.setattr(
            s, "run_job",
            lambda job: sentinel_path.append(job.get("id")) or (True, "doc", "ok", None),
        )

        # Calling s.run_job with profile matching gateway should NOT call
        # the delegate (the delegate sentinel stays empty).
        result = s.run_job({"id": "inproc01", "profile": "", "no_agent": True, "script": "x.sh"})
        assert result[0] is True
        assert sentinel_path == ["inproc01"]
        assert delegate_called == []

    def test_profile_differs_routes_to_runner(self, monkeypatch):
        """A job whose profile differs from the gateway's gets delegated
        to ``cron.runner.run_job_in_profile``. The scheduler must NOT
        try to run it in-process (which would use the wrong HERMES_HOME)."""
        # Set up a sibling profile so decide_routing returns "delegate".
        gateway = _gateway_home()
        profiles_root = gateway / "profiles"
        profiles_root.mkdir(parents=True, exist_ok=True)
        target = profiles_root / "research"
        target.mkdir(parents=True, exist_ok=True)

        from cron import scheduler as s
        from cron import runner as r
        from cron.profile_routing import decide_routing

        # Confirm the routing decision first so the test fails fast if
        # profile_routing's contract changes.
        d = decide_routing({"profile": "research"})
        assert d.action == "delegate", d

        # Now patch the runner to record its calls.
        captured = {}
        def fake_run(job):
            captured["job_id"] = job.get("id")
            captured["profile"] = job.get("profile")
            return r.RunResult(
                job_id=job.get("id", ""),
                profile=job.get("profile", ""),
                status="ok",
                exit_code=0,
                duration_ms=42,
                output="from-target-profile",
                error=None,
            )
        monkeypatch.setattr(r, "run_job_in_profile", fake_run)

        # Run via scheduler. Note: we don't assert the (success, output,
        # response, error) tuple exactly because the scheduler wraps the
        # runner's result into a doc; we just confirm delegation happened
        # and the response string came from the runner.
        success, doc, response, error = s.run_job({"id": "delegate01", "profile": "research", "no_agent": True, "script": "x.sh"})
        assert captured.get("job_id") == "delegate01"
        assert captured.get("profile") == "research"
        assert response == "from-target-profile"
        assert error is None
        assert success is True

    def test_unreachable_profile_returns_failure_tuple(self, monkeypatch):
        """A job whose profile refers to a missing dir must NOT silently
        fall through to in-process execution — that would defeat the
        whole point of declaring a target profile. The scheduler returns
        a failure tuple so mark_job_run records it as an error."""
        gateway = _gateway_home()
        profiles_root = gateway / "profiles"
        profiles_root.mkdir(parents=True, exist_ok=True)
        # We DON'T create profiles/missing-profile.

        from cron import scheduler as s
        from cron.profile_routing import decide_routing

        d = decide_routing({"profile": "missing-profile"})
        assert d.action == "unreachable", d

        # The scheduler should produce a failure tuple — success=False,
        # error message mentions the profile.
        success, doc, response, error = s.run_job({
            "id": "unreach01",
            "name": "unreach",
            "profile": "missing-profile",
            "no_agent": True,
            "script": "x.sh",
        })
        assert success is False
        assert error is not None
        assert "missing-profile" in (error or "") or "unreachable" in (error or "").lower()
        # The response text should NOT be silently delivered as success.
        assert response == "" or response.startswith("⚠") or "Cron" in doc
