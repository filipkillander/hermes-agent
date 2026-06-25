"""``hermes_cron_executor`` — entrypoint for LLM jobs delegated to a
non-gateway profile.

When the Lumi gateway schedules a job whose ``profile`` is not the
gateway's own, the runner (``cron.runner``) hands off execution here so
the agent machinery loads skills/plugins/config.yaml from the *target*
profile's HERMES_HOME rather than the gateway's.

This module is the public seam of that delegation — it is imported both
by ``cron.runner._run_agent_job`` and (optionally, in the future) by a
``hermes cron run --job-id <id>`` CLI subcommand. Keeping it tiny and
import-free of the gateway keeps the subprocess startup cost low.

Contract:
* Reads HERMES_HOME from the environment (set by ``cron.runner``).
* Calls ``cron.scheduler.run_job`` exactly as the gateway would.
* Writes a structured ``RunResult`` JSON to ``HERMES_CRON_RUN_LOG_DIR``,
  which is the gateway's ``<HERMES_HOME>/cron/runs/<job_id>/`` dir.
* Returns the same RunResult the gateway sees, so a single log entry
  per run shows up under the master store regardless of which profile
  did the work.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from hermes_constants import get_hermes_home

from cron.runner import RunResult, _utc_now_iso, _write_run_log, _safe_job_id


logger = logging.getLogger(__name__)


def run_job_entrypoint(job: dict, *, run_log_dir: Optional[Path] = None) -> RunResult:
    """Execute ``job`` under the active HERMES_HOME and write the result.

    This is the entrypoint the runner calls for LLM jobs (``no_agent=False``)
    in a non-gateway profile. It MUST be importable without pulling in
    gateway-only state (otherwise we'd defeat the whole point of the
    delegation).

    Args:
        job: The cron job dict (same shape as in jobs.json).
        run_log_dir: Where to write the run-log JSON. Defaults to
            ``<gateway_home>/cron/runs/<job_id>/`` so the master store
            captures every run even when the work happened in another
            profile.
    """
    job_id = str(job.get("id") or "").strip()
    target_home = get_hermes_home().resolve()
    profile = str(job.get("profile") or target_home.name)

    if run_log_dir is None:
        run_log_dir = (
            # The "gateway home" here is interpreted as the active
            # profile's home if HERMES_CRON_GATEWAY_HOME is set; otherwise
            # the runner has already wired us up to write into the
            # master's run-log dir.
            Path(__file__).resolve().parent.parent  # noqa: F841 — see note
            / "cron" / "runs" / _safe_job_id(job_id)
        )
        run_log_dir.mkdir(parents=True, exist_ok=True)
    # Import lazily so importing this module never pulls in
    # AIAgent / SessionDB unless we actually need them.
    try:
        from cron.scheduler import run_job as _scheduler_run_job

        ok, output, final_response, error = _scheduler_run_job(job)
    except Exception as exc:
        result = RunResult(
            job_id=job_id,
            profile=profile,
            status="error",
            exit_code=1,
            duration_ms=0,
            output="",
            error=f"{type(exc).__name__}: {exc}",
            script_path=None,
            started_at=_utc_now_iso(),
            finished_at=_utc_now_iso(),
        )
        # Write into the gateway's run-log dir so the operator sees it.
        _write_run_log(result)
        return result

    # Mirror the same empty-response guard the gateway's run_one_job uses
    # so a soft-failure agent run is not silently marked ok.
    if ok and not (final_response or "").strip():
        ok = False
        error = error or "Agent completed but produced empty response"

    status = "ok" if ok else "error"
    result = RunResult(
        job_id=job_id,
        profile=profile,
        status=status,
        exit_code=0 if ok else 1,
        duration_ms=0,  # scheduler.run_job doesn't expose duration; could be added later
        output=(final_response or output or "").strip(),
        error=error,
        script_path=None,
        started_at=_utc_now_iso(),
        finished_at=_utc_now_iso(),
    )

    # Write into the runner-supplied log dir first (master store),
    # then fall back to the executor-local helper so per-profile local
    # inspection works too.
    try:
        run_log_dir.mkdir(parents=True, exist_ok=True)
        ts = result.started_at.replace(":", "-")
        path = run_log_dir / f"{ts}.json"
        path.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        try:
            if path is not None:
                path.chmod(0o600)
        except OSError:
            pass
    except Exception as exc:  # pragma: no cover - never let IO fail a run
        logger.warning("executor: could not write run log: %s", exc)
    _write_run_log(result)
    return result


# ---------------------------------------------------------------------------
# CLI entrypoint — ``python -m cron.executor --job-file <path>``
# ---------------------------------------------------------------------------


def _main(argv: list[str]) -> int:
    """Subprocess entrypoint used by ``cron.runner._run_agent_job``.

    The runner (running in the gateway profile) writes the job dict to a
    temp JSON file under the master run-log dir, sets ``HERMES_HOME`` to
    the target profile, and spawns this module as a child process. We
    load the job dict, hand it to ``run_job_entrypoint``, and exit with
    the RunResult's ``exit_code`` so the runner can detect failures.

    Args:
        argv: Process argv (excluding ``[0]``, the script name). Accepted
            forms:
              * ``--job-file <path>``  — path to the job dict JSON
              * ``--run-log-dir <path>`` — optional override of the
                run-log directory. Defaults to the env-supplied
                ``HERMES_CRON_RUN_LOG_DIR``.

    Returns:
        The RunResult's ``exit_code`` (``0`` on success). Errors return
        ``1`` so the runner can mark the run failed.
    """
    import argparse

    parser = argparse.ArgumentParser(prog="python -m cron.executor")
    parser.add_argument(
        "--job-file",
        default=os.environ.get("HERMES_CRON_JOB_FILE", ""),
        help=(
            "Path to a JSON file containing the cron job dict. Defaults "
            "to the $HERMES_CRON_JOB_FILE env var."
        ),
    )
    parser.add_argument(
        "--run-log-dir",
        default=os.environ.get("HERMES_CRON_RUN_LOG_DIR", ""),
        help=(
            "Directory where the RunResult JSON should be written. "
            "Defaults to $HERMES_CRON_RUN_LOG_DIR (set by the runner)."
        ),
    )
    args = parser.parse_args(argv)

    if not args.job_file:
        logger.error("cron.executor: --job-file (or $HERMES_CRON_JOB_FILE) is required")
        return 1

    job_path = Path(args.job_file)
    try:
        job = json.loads(job_path.read_text())
    except FileNotFoundError:
        logger.error("cron.executor: job file not found: %s", job_path)
        return 1
    except Exception as exc:
        logger.error("cron.executor: could not read job file %s: %s", job_path, exc)
        return 1

    run_log_dir = Path(args.run_log_dir) if args.run_log_dir else None
    if run_log_dir is not None:
        run_log_dir.mkdir(parents=True, exist_ok=True)

    result = run_job_entrypoint(job, run_log_dir=run_log_dir)
    try:
        return int(result.exit_code)
    except (TypeError, ValueError):
        return 0 if result.status == "ok" else 1


if __name__ == "__main__":  # pragma: no cover - subprocess entrypoint
    import os
    import sys as _sys

    raise SystemExit(_main(_sys.argv[1:]))
