"""Multiprofile cron job execution.

This module is the actual subprocess workhorse used by the Lumi gateway
scheduler when a job declares a target ``profile`` that differs from the
gateway's own.

Architecture:
* The gateway scheduler (``cron.scheduler.run_job``) calls
  ``run_job_in_profile(job)`` once ``cron.profile_routing.decide_routing``
  has flagged the job as ``delegate``.
* For ``no_agent=True`` jobs, the runner resolves the script via
  ``cron.profile_routing.resolve_script_path`` (which prefers the target
  profile's scripts/ dir over the gateway's) and spawns it with
  ``HERMES_HOME`` set to the target profile's home — so the script can
  read config/skills/state from the right profile.
* For ``no_agent=False`` (LLM) jobs, the runner currently delegates to the
  target profile's HERMES_HOME by setting the env var and re-invoking
  ``cron.scheduler.run_job`` via a small executor entrypoint; see
  ``_run_agent_subprocess``.
* After every run — success, failure, or unreachable — a structured
  record is written to ``<gateway>/cron/runs/<job_id>/<timestamp>.json``
  so the operator has a forensic trail. The gateway's master
  ``jobs.json`` is NEVER mutated from here; that remains the
  scheduler's job (via ``mark_job_run``).

Why a separate subprocess instead of in-process routing:
* Each Hermes profile may have its own Python venv / provider keys /
  skill set. Setting ``HERMES_HOME`` in the gateway process and calling
  ``run_job`` directly would re-load config.yaml from the wrong place
  and stomp on per-process state (the AIAgent cache, the credential
  pool, MCP servers, the SQLite session store).
* A short-lived subprocess with the right env var gets clean isolation
  for free.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from hermes_constants import get_hermes_home

from cron.profile_routing import (
    ProfileRoutingError,
    ScriptNotFoundError,
    decide_routing,
    get_active_profile_name,
    resolve_profile_home,
    resolve_script_path,
)


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# RunResult
# ---------------------------------------------------------------------------


# Status vocabulary. The scheduler's ``mark_job_run`` treats anything
# other than "ok" as a failure for ``last_status`` purposes, so we
# collapse silent runs to "ok" early here.
_VALID_STATUSES = {"ok", "error", "timeout", "unreachable"}


@dataclass
class RunResult:
    """Structured result of one job execution.

    Returned by ``run_job_in_profile``. JSON-serializable via
    ``to_dict`` / ``from_dict`` so it can cross the subprocess boundary
    cleanly and be persisted to the run-log.
    """

    job_id: str
    profile: str
    status: str
    exit_code: int
    duration_ms: int
    output: str
    error: Optional[str] = None
    script_path: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "RunResult":
        # Tolerate legacy callers passing a richer set of fields.
        allowed = {f for f in cls.__dataclass_fields__.keys()}
        cleaned = {k: v for k, v in (data or {}).items() if k in allowed}
        result = cls(**cleaned)
        result.status = _normalize_status(result.status)
        return result


def _normalize_status(raw: Optional[str]) -> str:
    """Collapse / validate status values to the canonical vocabulary.

    * ``"silent"`` → ``"ok"`` (matches the existing scheduler convention
      where silent runs are still successful).
    * ``None`` / unknown → ``"error"`` (defensive — better to surface a
      bad status than to crash the scheduler tick).
    """
    if raw is None:
        return "error"
    text = str(raw).strip().lower()
    if text == "silent":
        return "ok"
    if text in _VALID_STATUSES:
        return text
    return "error"


# ---------------------------------------------------------------------------
# Run-log writing (structured trace under master store)
# ---------------------------------------------------------------------------


def _runs_root() -> Path:
    """Path to <gateway>/cron/runs/ — the per-job trace directory.

    Always anchored on the *gateway* home (Lumi's master store), never on
    the target profile. Even when a job runs in research, its trace
    lives next to the master jobs.json so the operator has one place to
    look.
    """
    root = get_hermes_home().resolve() / "cron" / "runs"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _write_run_log(result: "RunResult") -> Optional[Path]:
    """Persist the run record to disk. Returns the path written, or None
    if the write failed for any reason (caller already got the result;
    a missing trace is bad but not catastrophic)."""
    try:
        job_dir = _runs_root() / _safe_job_id(result.job_id)
        job_dir.mkdir(parents=True, exist_ok=True)
        # Try to keep permissions tight; never raise on chmod failure.
        try:
            job_dir.chmod(0o700)
        except OSError:
            pass
        ts = (result.started_at or _utc_now_iso()).replace(":", "-")
        path = job_dir / f"{ts}.json"
        path.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return path
    except Exception as exc:  # pragma: no cover - never let trace IO crash a run
        logger.warning(
            "run_job_in_profile: failed to write run log for job %s: %s",
            result.job_id,
            exc,
        )
        return None


def _safe_job_id(job_id: str) -> str:
    """Mirror cron.jobs._job_output_dir's safe-component rule for the
    runs/<job_id>/ path. Re-validated here so this module never trusts
    a hand-edited jobs.json blindly."""
    text = str(job_id or "").strip()
    if not text or text in {".", ".."} or "/" in text or "\\" in text:
        raise ValueError(f"Invalid cron job id for run-log path: {job_id!r}")
    if Path(text).is_absolute() or Path(text).drive:
        raise ValueError(f"Invalid cron job id for run-log path: {job_id!r}")
    return text


def _utc_now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


# ---------------------------------------------------------------------------
# Script execution (no_agent=True)
# ---------------------------------------------------------------------------


def _interpreter_for(path: Path) -> Optional[list]:
    """Pick argv for the script based on its extension. Mirrors the
    rules used by ``cron.scheduler._run_job_script`` but returns a
    bare argv (no subprocess.run call) so we can plug it into both the
    in-process and subprocess paths consistently."""
    suffix = path.suffix.lower()
    if suffix in {".sh", ".bash"}:
        bash = shutil.which("bash") or (
            "/bin/bash" if os.path.isfile("/bin/bash") else None
        )
        if bash is None:
            return None
        return [bash, str(path)]
    # Default to the current Python interpreter — covers .py and any
    # extension-less scripts.
    return [sys.executable, str(path)]


def _run_script_subprocess(
    *,
    argv: list,
    cwd: Path,
    target_home: Path,
    job_id: str,
    script_path: Path,
    timeout_seconds: Optional[int],
) -> "RunResult":
    """Spawn the script under target_home, capture exit/stdout/stderr,
    return a RunResult."""
    started_at = _utc_now_iso()
    t0 = time.monotonic()
    env = os.environ.copy()
    env["HERMES_HOME"] = str(target_home)
    # Provide a hint to the script so it can introspect who ran it.
    env["HERMES_CRON_JOB_ID"] = str(job_id)
    env["HERMES_CRON_PROFILE"] = str(target_home.name)
    # Same env-sanitisation policy as the existing scheduler — no
    # provider secrets leaked into child processes.
    try:
        from tools.environments.local import _sanitize_subprocess_env

        env = _sanitize_subprocess_env(env)
    except Exception:
        pass

    try:
        popen_kwargs = {}
        if sys.platform == "win32":
            from hermes_cli._subprocess_compat import windows_hide_flags

            popen_kwargs["creationflags"] = windows_hide_flags()

        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            cwd=str(cwd),
            env=env,
            **popen_kwargs,
        )
        duration_ms = int((time.monotonic() - t0) * 1000)
        stdout = (result.stdout or "")
        stderr = (result.stderr or "")

        # Redact secrets in both streams before they hit the run-log.
        try:
            from agent.redact import redact_sensitive_text

            stdout = redact_sensitive_text(stdout)
            stderr = redact_sensitive_text(stderr)
        except Exception:
            pass

        if result.returncode == 0:
            # Silent script (no stdout) still counts as ok — matches the
            # existing scheduler convention.
            status = "ok"
        else:
            status = "error"

        output = stdout if status == "ok" else (stdout + ("\n" + stderr if stderr else "")).strip()
        error = None if status == "ok" else (
            f"Script exited with code {result.returncode}" + (f": {stderr.strip()}" if stderr.strip() else "")
        )

        return RunResult(
            job_id=job_id,
            profile=target_home.name,
            status=status,
            exit_code=result.returncode,
            duration_ms=duration_ms,
            output=output.strip() if output else "",
            error=error,
            script_path=str(script_path),
            started_at=started_at,
            finished_at=_utc_now_iso(),
        )
    except subprocess.TimeoutExpired:
        duration_ms = int((time.monotonic() - t0) * 1000)
        return RunResult(
            job_id=job_id,
            profile=target_home.name,
            status="timeout",
            exit_code=-1,
            duration_ms=duration_ms,
            output="",
            error=f"Script timed out after {timeout_seconds}s: {script_path}",
            script_path=str(script_path),
            started_at=started_at,
            finished_at=_utc_now_iso(),
        )
    except FileNotFoundError as exc:
        return RunResult(
            job_id=job_id,
            profile=target_home.name,
            status="error",
            exit_code=127,
            duration_ms=int((time.monotonic() - t0) * 1000),
            output="",
            error=f"Interpreter not found: {exc}",
            script_path=str(script_path),
            started_at=started_at,
            finished_at=_utc_now_iso(),
        )
    except Exception as exc:
        duration_ms = int((time.monotonic() - t0) * 1000)
        return RunResult(
            job_id=job_id,
            profile=target_home.name,
            status="error",
            exit_code=1,
            duration_ms=duration_ms,
            output="",
            error=f"{type(exc).__name__}: {exc}",
            script_path=str(script_path),
            started_at=started_at,
            finished_at=_utc_now_iso(),
        )


def _resolve_timeout_seconds(job: dict) -> Optional[int]:
    """Honor a per-job ``timeout_seconds`` override; fall back to the
    scheduler-wide default. ``None`` means no hard timeout — let the
    script run until it returns."""
    raw = job.get("timeout_seconds")
    if raw is None:
        # Same default as cron.scheduler._DEFAULT_SCRIPT_TIMEOUT (120s).
        try:
            from cron.scheduler import _get_script_timeout

            return int(_get_script_timeout())
        except Exception:
            return 120
    try:
        value = int(raw)
        return value if value > 0 else None
    except (TypeError, ValueError):
        logger.warning("Invalid timeout_seconds=%r for job %s; using default", raw, job.get("id"))
        return 120


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_job_in_profile(job: dict) -> RunResult:
    """Execute ``job`` in the profile it declares and return a structured result.

    Handles both ``no_agent=True`` (script) and ``no_agent=False`` (LLM)
    jobs. For LLM jobs, the run is dispatched to a subprocess that
    re-invokes the scheduler under the target HERMES_HOME; the result
    payload is JSON-serialized over the subprocess boundary and
    deserialized here.

    The function NEVER mutates the master jobs.json store — that is the
    scheduler's job via ``mark_job_run``. It only writes a forensic
    trace to ``<gateway>/cron/runs/<job_id>/<ts>.json``.
    """
    job_id = str(job.get("id") or "").strip()
    if not job_id:
        # No id at all is a programmer error, not a runtime condition.
        # Fail loudly so the caller notices instead of dropping the run.
        raise ValueError("run_job_in_profile: job has no id")

    decision = decide_routing(job)
    profile_name = decision.profile or get_active_profile_name()

    if decision.action == "unreachable":
        result = RunResult(
            job_id=job_id,
            profile=decision.profile,
            status="unreachable",
            exit_code=-1,
            duration_ms=0,
            output="",
            error=decision.reason,
            script_path=None,
            started_at=_utc_now_iso(),
            finished_at=_utc_now_iso(),
        )
        _write_run_log(result)
        return result

    target_home = decision.target_home or get_hermes_home().resolve()

    if job.get("no_agent"):
        return _run_script_job(job, target_home)
    return _run_agent_job(job, target_home)


def _run_script_job(job: dict, target_home: Path) -> RunResult:
    """Script-only job path (no_agent=True)."""
    job_id = str(job.get("id") or "")
    script_ref = job.get("script")
    if not script_ref:
        result = RunResult(
            job_id=job_id,
            profile=target_home.name,
            status="error",
            exit_code=1,
            duration_ms=0,
            output="",
            error="no_agent=True but job has no script",
            script_path=None,
            started_at=_utc_now_iso(),
            finished_at=_utc_now_iso(),
        )
        _write_run_log(result)
        return result

    try:
        script_path = resolve_script_path(
            # Use the declared profile (or active) so the script resolves
            # against the *target* scripts dir, not the gateway's.
            job.get("profile") or get_active_profile_name(),
            str(script_ref),
        )
    except ScriptNotFoundError as exc:
        result = RunResult(
            job_id=job_id,
            profile=target_home.name,
            status="error",
            exit_code=2,
            duration_ms=0,
            output="",
            error=str(exc),
            script_path=None,
            started_at=_utc_now_iso(),
            finished_at=_utc_now_iso(),
        )
        _write_run_log(result)
        return result
    except ProfileRoutingError as exc:
        result = RunResult(
            job_id=job_id,
            profile=target_home.name,
            status="error",
            exit_code=2,
            duration_ms=0,
            output="",
            error=str(exc),
            script_path=None,
            started_at=_utc_now_iso(),
            finished_at=_utc_now_iso(),
        )
        _write_run_log(result)
        return result

    argv = _interpreter_for(script_path)
    if argv is None:
        result = RunResult(
            job_id=job_id,
            profile=target_home.name,
            status="error",
            exit_code=127,
            duration_ms=0,
            output="",
            error=f"Cannot run {script_path.name!r}: no bash on PATH",
            script_path=str(script_path),
            started_at=_utc_now_iso(),
            finished_at=_utc_now_iso(),
        )
        _write_run_log(result)
        return result

    result = _run_script_subprocess(
        argv=argv,
        cwd=script_path.parent,
        target_home=target_home,
        job_id=job_id,
        script_path=script_path,
        timeout_seconds=_resolve_timeout_seconds(job),
    )
    _write_run_log(result)
    return result


def _run_agent_job(job: dict, target_home: Path) -> RunResult:
    """LLM job path (no_agent=False). The agent machinery (AIAgent,
    SessionDB, skill loader, MCP servers, config.yaml) reads
    ``HERMES_HOME`` at import time, so we MUST run the job in a
    subprocess that inherits ``HERMES_HOME=<target_home>`` — otherwise
    the agent would load the gateway's config/skills/state and stomp
    on per-process caches.

    The runner hands the job dict to the child via a small JSON file
    under the master run-log dir, the child runs the agent under the
    target profile, and writes a structured RunResult JSON back to
    that same dir. The runner re-reads the RunResult so the caller
    (gateway scheduler) gets the same shape as the script path.

    The subprocess inherits a sanitized env (provider keys blocked,
    HERMES_HOME / HERMES_CRON_* / run-log dir wired through). The
    runner never mutates the gateway's master ``jobs.json`` — that's
    the scheduler's job via ``mark_job_run``. The subprocess only
    writes the run-log.
    """
    job_id = str(job.get("id") or "").strip()
    if not job_id:
        # Should be caught upstream, but defend again so we never spawn
        # an unkeyed child.
        raise ValueError("_run_agent_job: job has no id")

    # Strip any HERMES_HOME that points at the gateway so the subprocess
    # definitely inherits the target's. Without this, an inherited env
    # would silently route the LLM back to the gateway profile.
    env = os.environ.copy()
    env["HERMES_HOME"] = str(target_home)
    env["HERMES_CRON_JOB_ID"] = job_id
    env["HERMES_CRON_PROFILE"] = str(target_home.name)

    # The run-log dir is always on the gateway's master store (Lumi's
    # HERMES_HOME), regardless of which profile did the work — see
    # ``_runs_root`` for rationale. We drop a small job-payload JSON
    # there so the child can read it without inheriting the full job
    # dict via env (avoids blowing past env-var size limits and keeps
    # prompt text out of `ps`-visible env).
    run_log_dir = _runs_root() / _safe_job_id(job_id)
    run_log_dir.mkdir(parents=True, exist_ok=True)
    try:
        run_log_dir.chmod(0o700)
    except OSError:
        pass
    env["HERMES_CRON_RUN_LOG_DIR"] = str(run_log_dir)
    job_payload_path = run_log_dir / "_job.json"
    try:
        job_payload_path.write_text(
            json.dumps(job, ensure_ascii=False, indent=2)
        )
        try:
            job_payload_path.chmod(0o600)
        except OSError:
            pass
        env["HERMES_CRON_JOB_FILE"] = str(job_payload_path)
    except Exception as exc:
        # If we can't drop the payload, fall back to a /tmp file so the
        # subprocess can still run. Never let IO failure crash a tick.
        logger.warning(
            "_run_agent_job: could not write job payload to %s: %s; "
            "falling back to a tmp file",
            job_payload_path,
            exc,
        )
        import tempfile

        fd, fallback_str = tempfile.mkstemp(
            prefix="hermes_cron_job_", suffix=".json", text=True
        )
        try:
            os.write(fd, json.dumps(job, ensure_ascii=False).encode("utf-8"))
        finally:
            os.close(fd)
        try:
            os.chmod(fallback_str, 0o600)
        except OSError:
            pass
        env["HERMES_CRON_JOB_FILE"] = fallback_str

    # Sanitise the env so provider secrets in the gateway process don't
    # leak verbatim into the child. Same helper the script path uses.
    try:
        from tools.environments.local import _sanitize_subprocess_env

        env = _sanitize_subprocess_env(env)
    except Exception as exc:
        logger.debug("_run_agent_job: _sanitize_subprocess_env unavailable: %s", exc)

    # Re-apply our explicit overrides AFTER sanitisation so a malicious
    # blocklist entry can't clobber HERMES_HOME / HERMES_CRON_*. The
    # _sanitize_subprocess_env helper preserves the keys we set, but
    # defend explicitly so this stays correct if the helper's policy
    # is widened later.
    env["HERMES_HOME"] = str(target_home)
    env["HERMES_CRON_JOB_ID"] = job_id
    env["HERMES_CRON_PROFILE"] = str(target_home.name)
    env["HERMES_CRON_RUN_LOG_DIR"] = str(run_log_dir)
    env["HERMES_CRON_JOB_FILE"] = str(env["HERMES_CRON_JOB_FILE"])

    argv = [
        sys.executable,
        "-m",
        "cron.executor",
        "--job-file",
        str(env["HERMES_CRON_JOB_FILE"]),
        "--run-log-dir",
        str(run_log_dir),
    ]

    started_at = _utc_now_iso()
    t0 = time.monotonic()
    timeout_seconds = _resolve_timeout_seconds(job)
    popen_kwargs: dict = {}
    if sys.platform == "win32":
        try:
            from hermes_cli._subprocess_compat import windows_hide_flags

            popen_kwargs["creationflags"] = windows_hide_flags()
        except Exception:
            pass

    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            cwd=str(target_home),
            env=env,
            **popen_kwargs,
        )
    except subprocess.TimeoutExpired:
        duration_ms = int((time.monotonic() - t0) * 1000)
        run_result = RunResult(
            job_id=job_id,
            profile=target_home.name,
            status="timeout",
            exit_code=-1,
            duration_ms=duration_ms,
            output="",
            error=f"Agent subprocess timed out after {timeout_seconds}s",
            script_path=None,
            started_at=started_at,
            finished_at=_utc_now_iso(),
        )
        _write_run_log(run_result)
        return run_result
    except Exception as exc:
        duration_ms = int((time.monotonic() - t0) * 1000)
        run_result = RunResult(
            job_id=job_id,
            profile=target_home.name,
            status="error",
            exit_code=1,
            duration_ms=duration_ms,
            output="",
            error=f"{type(exc).__name__}: {exc}",
            script_path=None,
            started_at=started_at,
            finished_at=_utc_now_iso(),
        )
        _write_run_log(run_result)
        return run_result

    duration_ms = int((time.monotonic() - t0) * 1000)

    # Redact secrets in both streams before they hit the run-log.
    stdout = (result.stdout or "")
    stderr = (result.stderr or "")
    try:
        from agent.redact import redact_sensitive_text

        stdout = redact_sensitive_text(stdout)
        stderr = redact_sensitive_text(stderr)
    except Exception:
        pass

    # Re-read the canonical RunResult the child wrote. The child is
    # authoritative about status/output/duration because it ran the
    # actual agent. The runner just records the child's exit code as
    # extra context if the child wrote nothing.
    latest = _latest_log(run_log_dir)
    if latest is not None:
        try:
            run_result = RunResult.from_dict(json.loads(latest.read_text()))
            # Prefer the gateway's measured duration (it spans the
            # whole subprocess lifetime) over the child's local clock.
            if run_result.duration_ms in (0, None):
                run_result.duration_ms = duration_ms
            return run_result
        except Exception as exc:
            logger.warning(
                "_run_agent_job: could not parse run log %s: %s",
                latest,
                exc,
            )

    # Child wrote nothing — synthesise a RunResult from the subprocess
    # exit code + captured streams. This is the failure path (the
    # child's contract is to always write a RunResult), so anything we
    # build here is by definition a degraded signal.
    if result.returncode == 0:
        status = "ok"
        error = None
    else:
        status = "error"
        error = (
            f"Agent subprocess exited {result.returncode} without writing "
            f"a run-log entry"
            + (f": {stderr.strip()}" if stderr.strip() else "")
        )
    output = stdout.strip() if status == "ok" else (
        (stdout + ("\n" + stderr if stderr else "")).strip()
    )
    run_result = RunResult(
        job_id=job_id,
        profile=target_home.name,
        status=status,
        exit_code=result.returncode,
        duration_ms=duration_ms,
        output=output,
        error=error,
        script_path=None,
        started_at=started_at,
        finished_at=_utc_now_iso(),
    )
    _write_run_log(run_result)
    return run_result


def _latest_log(log_dir: Path) -> Optional[Path]:
    if not log_dir.is_dir():
        return None
    entries = sorted(log_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return entries[0] if entries else None
