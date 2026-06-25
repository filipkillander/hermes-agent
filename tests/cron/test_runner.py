"""Tests for cron/runner.py — multiprofile job execution.

Lumi gateway delegates job execution to the target profile's HERMES_HOME
through ``run_job_in_profile``. This module is the actual subprocess
workhorse — it picks the right interpreter, sets HERMES_HOME, captures the
result, and writes a structured run-log entry under the gateway's master
store so the scheduler can update last_status/last_error etc.

Contract for ``run_job_in_profile``:
* For ``no_agent=True`` (script) jobs: spawn the script under the target
  HERMES_HOME, capture stdout/stderr/exit_code/duration, return a
  ``RunResult`` and write a structured ``runs/<job_id>/<ts>.json`` record.
* For ``no_agent=False`` (LLM) jobs: same as above but the subprocess
  delegates to ``hermes_cron_executor`` (a thin module the runner imports
  in the child process) so the agent machinery can load skills/plugins
  from the target profile.
* The runner NEVER mutates the gateway's master store directly — that is
  the scheduler's job. The run-log is a *trace*, the master store update
  happens via mark_job_run() back in the gateway process.
* Script path resolution uses ``cron.profile_routing.resolve_script_path``
  so the target profile's scripts/ is preferred over the gateway's.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


def _gateway_home() -> Path:
    return Path(os.environ["HERMES_HOME"]).resolve()


def _hermes_root() -> Path:
    return _gateway_home()


# ---------------------------------------------------------------------------
# RunResult dataclass
# ---------------------------------------------------------------------------


class TestRunResult:
    """RunResult is the structured return value of run_job_in_profile."""

    def test_to_dict_round_trip(self):
        from cron.runner import RunResult

        result = RunResult(
            job_id="abc123",
            profile="research",
            status="ok",
            exit_code=0,
            duration_ms=42,
            output="hello",
            error=None,
            script_path=None,
        )
        d = result.to_dict()
        assert d["job_id"] == "abc123"
        assert d["profile"] == "research"
        assert d["status"] == "ok"
        assert d["exit_code"] == 0
        assert d["duration_ms"] == 42
        assert d["output"] == "hello"
        assert d["error"] is None
        # JSON-serializable so it can be written to disk.
        json.dumps(d)

    def test_from_dict_round_trip(self):
        from cron.runner import RunResult

        src = {
            "job_id": "j1",
            "profile": "minerva",
            "status": "error",
            "exit_code": 2,
            "duration_ms": 123,
            "output": "stderr noise",
            "error": "boom",
            "script_path": "/some/path",
        }
        result = RunResult.from_dict(src)
        assert result.job_id == "j1"
        assert result.profile == "minerva"
        assert result.status == "error"
        assert result.exit_code == 2
        assert result.duration_ms == 123
        assert result.error == "boom"
        assert result.script_path == "/some/path"

    def test_status_is_normalized(self):
        """RunResult.status is constrained to a known vocabulary so the
        scheduler's last_status comparisons stay stable."""
        from cron.runner import RunResult

        for raw, expected in (
            ("ok", "ok"),
            ("error", "error"),
            ("timeout", "timeout"),
            ("unreachable", "unreachable"),
            ("silent", "ok"),  # silent script runs map to ok
            ("garbage", "error"),  # anything unknown → error
        ):
            r = RunResult.from_dict({
                "job_id": "j", "profile": "lumi", "exit_code": 0,
                "duration_ms": 0, "output": "", "error": None,
                "script_path": None, "status": raw,
            })
            assert r.status == expected, f"{raw!r} → {r.status!r}"


# ---------------------------------------------------------------------------
# Script-jobs (no_agent=True)
# ---------------------------------------------------------------------------


class TestRunScriptJob:
    """run_job_in_profile for no_agent=True jobs runs the script under
    the target HERMES_HOME."""

    def test_in_process_when_profile_matches_gateway(self, monkeypatch):
        """profile=current → no subprocess, run via _run_job_script
        equivalent inline."""
        # Script lives in the gateway scripts/ dir.
        gateway_scripts = _gateway_home() / "scripts"
        gateway_scripts.mkdir(parents=True, exist_ok=True)
        script = gateway_scripts / "echo_ok.sh"
        script.write_text("#!/bin/bash\necho gateway-run\n")
        script.chmod(0o755)

        from cron.runner import run_job_in_profile

        job = {
            "id": "abc123def456",
            "name": "test",
            "no_agent": True,
            "script": "echo_ok.sh",
            "profile": "",
        }
        result = run_job_in_profile(job)

        assert result.status == "ok"
        assert result.exit_code == 0
        assert "gateway-run" in result.output
        # No profile declared → runner reports the active profile
        # (which in tests is the conftest tempdir's name).
        assert result.profile  # non-empty — gateway profile name recorded

    def test_resolves_script_in_target_profile(self, monkeypatch):
        """When the script lives in the target profile's scripts/ dir,
        it's preferred over the gateway's copy."""
        target_scripts = _hermes_root() / "profiles" / "research" / "scripts"
        target_scripts.mkdir(parents=True, exist_ok=True)
        target_script = target_scripts / "route.sh"
        target_script.write_text("#!/bin/bash\necho target-run\n")
        target_script.chmod(0o755)

        # Also put a copy in the gateway scripts/ to confirm target wins.
        gateway_scripts = _gateway_home() / "scripts"
        gateway_scripts.mkdir(parents=True, exist_ok=True)
        (gateway_scripts / "route.sh").write_text("#!/bin/bash\necho gateway-run\n")
        (gateway_scripts / "route.sh").chmod(0o755)

        from cron.runner import run_job_in_profile

        job = {
            "id": "xyz789xyz789",
            "name": "route-test",
            "no_agent": True,
            "script": "route.sh",
            "profile": "research",
        }
        result = run_job_in_profile(job)

        assert result.status == "ok"
        assert "target-run" in result.output
        assert "gateway-run" not in result.output
        assert result.profile == "research"
        # The actual path used should be inside the target profile.
        assert str(target_script.resolve()) in str(result.script_path or "")

    def test_subprocess_sees_target_hermes_home(self, monkeypatch):
        """The script subprocess inherits HERMES_HOME=<target_home> so
        any code it spawns that consults Hermes config / skills / state
        reads the target profile's data — not the gateway's."""
        target_scripts = _hermes_root() / "profiles" / "research" / "scripts"
        target_scripts.mkdir(parents=True, exist_ok=True)
        script = target_scripts / "probe_home.sh"
        script.write_text("#!/bin/bash\necho HH=$HERMES_HOME\n")
        script.chmod(0o755)

        target_home = (_hermes_root() / "profiles" / "research").resolve()
        # Sentinel env var set in the script: we'll override HERMES_HOME
        # in the subprocess but we can also confirm it survived.
        monkeypatch.setenv("_TEST_HARNESS_HOME_SENTINEL", str(_gateway_home()))

        from cron.runner import run_job_in_profile

        job = {
            "id": "hhprobe00001",
            "name": "probe",
            "no_agent": True,
            "script": "probe_home.sh",
            "profile": "research",
        }
        result = run_job_in_profile(job)

        assert result.status == "ok", result.error
        # The script printed "HH=<value>"; assert it equals the target.
        assert f"HH={target_home}" in result.output, result.output

    def test_unreachable_profile_returns_unreachable(self):
        """If the profile dir doesn't exist, return RunResult(status='unreachable')
        without crashing — the scheduler can record this and skip."""
        from cron.runner import run_job_in_profile

        job = {
            "id": "nope00000001",
            "name": "missing",
            "no_agent": True,
            "script": "anything.sh",
            "profile": "this-profile-does-not-exist",
        }
        result = run_job_in_profile(job)

        assert result.status == "unreachable"
        assert result.exit_code != 0
        assert result.error is not None
        assert "this-profile-does-not-exist" in (result.error or "") or "unreachable" in (result.error or "").lower()

    def test_failing_script_returns_error_status(self):
        """A script that exits non-zero returns status='error', not crash."""
        gateway_scripts = _gateway_home() / "scripts"
        gateway_scripts.mkdir(parents=True, exist_ok=True)
        script = gateway_scripts / "fails.sh"
        script.write_text("#!/bin/bash\necho failure 1>&2; exit 7\n")
        script.chmod(0o755)

        from cron.runner import run_job_in_profile

        job = {
            "id": "fail00000001",
            "name": "fails",
            "no_agent": True,
            "script": "fails.sh",
            "profile": "",
        }
        result = run_job_in_profile(job)

        assert result.status == "error"
        assert result.exit_code == 7

    def test_silent_script_returns_ok(self):
        """A script that exits 0 with empty stdout is status='ok' with
        empty output — matches the existing scheduler silent-run path."""
        gateway_scripts = _gateway_home() / "scripts"
        gateway_scripts.mkdir(parents=True, exist_ok=True)
        script = gateway_scripts / "silent.sh"
        script.write_text("#!/bin/bash\nexit 0\n")
        script.chmod(0o755)

        from cron.runner import run_job_in_profile

        job = {
            "id": "silent000001",
            "name": "silent",
            "no_agent": True,
            "script": "silent.sh",
            "profile": "",
        }
        result = run_job_in_profile(job)

        assert result.status == "ok"
        assert result.exit_code == 0
        assert result.output == ""


# ---------------------------------------------------------------------------
# Run-log writing
# ---------------------------------------------------------------------------


class TestRunLogWriting:
    """The runner writes a structured runs/<job_id>/<ts>.json per run."""

    def test_writes_run_log_under_gateway_store(self, monkeypatch):
        """Every successful run produces a JSON record under
        <gateway>/cron/runs/<job_id>/<timestamp>.json."""
        gateway_scripts = _gateway_home() / "scripts"
        gateway_scripts.mkdir(parents=True, exist_ok=True)
        script = gateway_scripts / "log_test.sh"
        script.write_text("#!/bin/bash\necho logged\n")
        script.chmod(0o755)

        from cron.runner import run_job_in_profile

        job = {
            "id": "logtest00001",
            "name": "log-test",
            "no_agent": True,
            "script": "log_test.sh",
            "profile": "",
        }
        result = run_job_in_profile(job)

        runs_dir = _gateway_home() / "cron" / "runs" / job["id"]
        assert runs_dir.is_dir(), f"runs dir missing: {runs_dir}"
        entries = list(runs_dir.glob("*.json"))
        assert len(entries) == 1, f"expected 1 log entry, got {entries}"
        record = json.loads(entries[0].read_text())
        assert record["job_id"] == job["id"]
        assert record["status"] == "ok"
        assert record["exit_code"] == 0
        assert "logged" in record["output"]

    def test_failed_run_also_writes_log(self):
        """Failed runs produce a log entry too — failures are first-class
        signal for the operator, not silent garbage to be dropped."""
        gateway_scripts = _gateway_home() / "scripts"
        gateway_scripts.mkdir(parents=True, exist_ok=True)
        script = gateway_scripts / "boom.sh"
        script.write_text("#!/bin/bash\nexit 9\n")
        script.chmod(0o755)

        from cron.runner import run_job_in_profile

        job = {
            "id": "boom00000001",
            "name": "boom",
            "no_agent": True,
            "script": "boom.sh",
            "profile": "",
        }
        run_job_in_profile(job)

        runs_dir = _gateway_home() / "cron" / "runs" / job["id"]
        entries = list(runs_dir.glob("*.json"))
        assert len(entries) == 1
        record = json.loads(entries[0].read_text())
        assert record["status"] == "error"
        assert record["exit_code"] == 9

    def test_run_log_includes_profile_and_script_path(self):
        """The log entry must record which profile + script actually ran
        so postmortem tooling can correlate failures to the right env."""
        gateway_scripts = _gateway_home() / "scripts"
        gateway_scripts.mkdir(parents=True, exist_ok=True)
        script = gateway_scripts / "trace.sh"
        script.write_text("#!/bin/bash\necho trace\n")
        script.chmod(0o755)

        from cron.runner import run_job_in_profile

        job = {
            "id": "trace0000001",
            "name": "trace",
            "no_agent": True,
            "script": "trace.sh",
            "profile": "",
        }
        run_job_in_profile(job)

        runs_dir = _gateway_home() / "cron" / "runs" / job["id"]
        record = json.loads(list(runs_dir.glob("*.json"))[0].read_text())
        assert "profile" in record
        assert "script_path" in record
        assert record["script_path"]  # non-empty


# ---------------------------------------------------------------------------
# Agent-job delegation (no_agent=False)
# ---------------------------------------------------------------------------


def _ensure_target_profile(name: str) -> Path:
    """Create ``<hermes_root>/profiles/<name>`` so ``decide_routing``
    returns ``"delegate"``. Returns the resolved target home."""
    target = (_hermes_root() / "profiles" / name).resolve()
    target.mkdir(parents=True, exist_ok=True)
    return target


class TestRunAgentJobDelegation:
    """``_run_agent_job`` must execute the LLM job in a *subprocess* under
    the target profile's HERMES_HOME — never in the gateway's own
    process. Otherwise the agent would load config / skills / state from
    the gateway profile, which is exactly the bug this commit fixes.
    """

    def test_agent_job_does_not_call_entrypoint_in_process(self, monkeypatch):
        """The runner must NOT import+call ``cron.executor.run_job_entrypoint``
        in the gateway process for delegated agent jobs. It must spawn a
        subprocess instead."""
        target_home = _ensure_target_profile("research")

        from cron import runner as r

        # Sentinel: if the runner in-process-calls the entrypoint, this
        # mock will fire and the test will fail.
        in_process_calls = []

        def forbidden_in_process(*args, **kwargs):
            in_process_calls.append((args, kwargs))
            raise AssertionError(
                "run_job_entrypoint must NOT be called in-process for "
                "delegated agent jobs — the runner should spawn a subprocess."
            )

        # Patch the in-process entrypoint call site AND the subprocess.run
        # call site. We capture subprocess.run so the test can assert the
        # subprocess was given the right env, then we simulate a successful
        # executor by writing a RunResult JSON to the run-log dir.
        captured = {}

        def fake_subprocess_run(argv, **kwargs):
            captured["argv"] = argv
            captured["kwargs"] = kwargs
            # Simulate the child writing a RunResult into the run-log dir.
            env = kwargs.get("env") or {}
            run_log_dir = Path(env.get("HERMES_CRON_RUN_LOG_DIR", ""))
            run_log_dir.mkdir(parents=True, exist_ok=True)
            ts = "2099-01-01T00-00-00Z"
            record = {
                "job_id": "subproc0001",
                "profile": "research",
                "status": "ok",
                "exit_code": 0,
                "duration_ms": 17,
                "output": "from-child",
                "error": None,
                "script_path": None,
                "started_at": "2099-01-01T00:00:00Z",
                "finished_at": "2099-01-01T00:00:01Z",
            }
            (run_log_dir / f"{ts}.json").write_text(json.dumps(record))
            # Return a fake CompletedProcess so the runner can read .returncode
            cp = MagicMock()
            cp.returncode = 0
            cp.stdout = ""
            cp.stderr = ""
            return cp

        monkeypatch.setattr(
            "cron.executor.run_job_entrypoint", forbidden_in_process
        )
        monkeypatch.setattr(r.subprocess, "run", fake_subprocess_run)

        job = {
            "id": "subproc0001",
            "name": "subproc-test",
            "no_agent": False,
            "profile": "research",
            "prompt": "echo hi from target profile",
        }
        result = r.run_job_in_profile(job)

        # The in-process call must NOT have happened.
        assert in_process_calls == [], (
            "runner._run_agent_job called run_job_entrypoint in-process; "
            "the agent would have run with the gateway's HERMES_HOME."
        )
        # Subprocess was called.
        assert "argv" in captured, "subprocess.run was not invoked"
        argv = captured["argv"]
        # argv must point at the current Python interpreter and reference
        # the hermes_cron_executor entrypoint.
        assert argv[0] == sys.executable
        argv_blob = " ".join(str(a) for a in argv)
        assert "hermes_cron_executor" in argv_blob or "cron.executor" in argv_blob, (
            f"subprocess argv does not look like the executor entrypoint: {argv!r}"
        )
        # Env must have HERMES_HOME pointing at the target profile and
        # HERMES_CRON_PROFILE set to the target profile name.
        env = captured["kwargs"].get("env") or {}
        assert env.get("HERMES_HOME") == str(target_home), (
            f"HERMES_HOME={env.get('HERMES_HOME')!r} did not match target "
            f"profile home {target_home}"
        )
        assert env.get("HERMES_CRON_PROFILE") == "research"
        assert env.get("HERMES_CRON_JOB_ID") == "subproc0001"
        # The runner must NOT mutate master jobs.json from the gateway
        # process — that's the scheduler's job. The subprocess only writes
        # the run-log; the gateway just reads it.
        assert result.status == "ok"
        assert result.profile == "research"

    def test_agent_job_subprocess_sees_sanitized_env(self, monkeypatch):
        """Provider secrets in the gateway's environment must NOT leak
        into the agent-job subprocess env verbatim. The runner must
        reuse ``_sanitize_subprocess_env`` so a leaked key in the
        gateway process can't be reused against the target profile's
        provider config."""
        _ensure_target_profile("research")

        from cron import runner as r

        # Plant a "secret" in the gateway process env. _sanitize_subprocess_env
        # should drop it (it matches the provider env-var blocklist).
        monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-from-gateway")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-anthropic-leak")

        captured = {}

        def fake_subprocess_run(argv, **kwargs):
            captured["argv"] = argv
            captured["env"] = kwargs.get("env") or {}
            cp = MagicMock()
            cp.returncode = 0
            cp.stdout = ""
            cp.stderr = ""
            # Write a synthetic run-log so the runner can deserialize it.
            env = captured["env"]
            run_log_dir = Path(env.get("HERMES_CRON_RUN_LOG_DIR", ""))
            run_log_dir.mkdir(parents=True, exist_ok=True)
            (run_log_dir / "2099-01-01T00-00-00Z.json").write_text(
                json.dumps({
                    "job_id": "sanitize001", "profile": "research",
                    "status": "ok", "exit_code": 0, "duration_ms": 0,
                    "output": "", "error": None, "script_path": None,
                    "started_at": "2099-01-01T00:00:00Z",
                    "finished_at": "2099-01-01T00:00:00Z",
                })
            )
            return cp

        monkeypatch.setattr(r.subprocess, "run", fake_subprocess_run)

        job = {
            "id": "sanitize001",
            "name": "sanitize",
            "no_agent": False,
            "profile": "research",
            "prompt": "hi",
        }
        r.run_job_in_profile(job)

        env = captured["env"]
        # Sanitizer must have stripped provider keys.
        assert "OPENAI_API_KEY" not in env, (
            "provider key leaked into agent-job subprocess env"
        )
        assert "ANTHROPIC_API_KEY" not in env, (
            "provider key leaked into agent-job subprocess env"
        )
        # But the cron profile markers we set explicitly must survive.
        assert env.get("HERMES_CRON_PROFILE") == "research"
        assert env.get("HERMES_CRON_JOB_ID") == "sanitize001"

    def test_agent_job_in_process_failure_keeps_run_log(self, monkeypatch):
        """If the subprocess exits non-zero, the runner still produces a
        structured RunResult and the run-log is written under the
        gateway store so the operator has a trace. Mirrors the contract
        for script jobs."""
        _ensure_target_profile("research")

        from cron import runner as r

        def fake_subprocess_run(argv, **kwargs):
            cp = MagicMock()
            cp.returncode = 2
            cp.stdout = "stdout-from-child"
            cp.stderr = "boom in target profile"
            return cp

        monkeypatch.setattr(r.subprocess, "run", fake_subprocess_run)

        job = {
            "id": "agentfail01",
            "name": "agent-fail",
            "no_agent": False,
            "profile": "research",
            "prompt": "fail",
        }
        result = r.run_job_in_profile(job)

        # Subprocess failure is surfaced as status="error" with the
        # child's exit code preserved.
        assert result.status == "error"
        assert result.exit_code == 2
        assert result.profile == "research"
        # The child stdout/stderr must be captured into the run-log so
        # the operator can see what happened. Exclude the runner's own
        # _job.json payload file (it's not a run-log entry).
        runs_dir = _gateway_home() / "cron" / "runs" / job["id"]
        assert runs_dir.is_dir()
        records = [p for p in runs_dir.glob("*.json") if not p.name.startswith("_")]
        assert len(records) == 1
        body = json.loads(records[0].read_text())
        assert body["status"] == "error"
        assert body["exit_code"] == 2
