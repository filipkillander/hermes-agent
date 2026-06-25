"""Multiprofile cron routing helpers.

Lumi owns the master job store. Each job declares a ``profile`` field that
decides which Hermes profile actually executes the job body — the
scheduler (running in the Lumi/gateway profile) picks between in-process
execution and delegation to a target profile's HERMES_HOME.

This module is intentionally pure logic — no subprocess invocation, no
file locking, no delivery. ``cron/runner.py`` (next to this file) consumes
its outputs and does the actual subprocess work.

Layout rules:
* HERMES_HOME is always the gateway's home (Lumi in production).
* A profile name ``P`` resolves to ``<HERMES_HOME>/profiles/P``.
* ``profile=default`` and ``profile=lumi`` both resolve to HERMES_HOME
  itself — the gateway runs them in-process and never delegates to itself.
* A profile name is a single safe path component: no separators, no
  traversal, no absolute paths. Hand-edited ``jobs.json`` cannot escape
  the profiles/ sandbox through this module.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from hermes_constants import get_hermes_home, get_default_hermes_root


# Sentinel profile names that resolve to HERMES_HOME itself (i.e. the
# gateway profile, no delegation needed).
_LOCAL_PROFILES = frozenset({"", "default", "lumi"})

# A safe profile name is a single path component: alphanumerics, ``_``,
# ``-``, ``.``. No ``/``, no ``\``, no leading dot (avoids ``.`` and
# ``..``), no NUL.
_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")


class ProfileRoutingError(ValueError):
    """Raised when a profile name or script path is unsafe."""


class ScriptNotFoundError(LookupError):
    """Raised when a script cannot be located in the target or gateway dir."""


@dataclass(frozen=True)
class RoutingDecision:
    """The scheduler's per-job routing decision.

    * ``action`` is one of:
        - ``"in_process"``  → run ``run_job`` in the gateway process
        - ``"delegate"``    → spawn a subprocess under ``target_home``
        - ``"unreachable"`` → target profile is unknown; mark + skip
    * ``profile`` is the normalized profile name (empty for in_process).
    * ``target_home`` is the resolved HERMES_HOME for the target profile
      (``None`` for in_process or unreachable).
    * ``reason`` is a short human-readable explanation for logs.
    """

    action: str
    profile: str
    target_home: Optional[Path]
    reason: str


def _hermes_root() -> Path:
    """Return the gateway's Hermes root (parent of profiles/).

    Delegates to ``hermes_constants.get_default_hermes_root`` which
    handles all three layouts the cron scheduler cares about:
      * Standard : HERMES_HOME=~/.hermes           → root=~/.hermes
      * Profile  : HERMES_HOME=~/.hermes/profiles/X → root=~/.hermes
      * Docker   : HERMES_HOME=/opt/data           → root=/opt/data
    """
    return get_default_hermes_root().resolve()


def _validate_profile_name(name: str) -> str:
    """Normalize and validate a profile name. Raises ProfileRoutingError."""
    if name is None:
        return ""
    cleaned = str(name).strip()
    if not cleaned:
        return ""
    if not _PROFILE_NAME_RE.match(cleaned):
        raise ProfileRoutingError(
            f"Invalid profile name {name!r}: must match "
            f"{_PROFILE_NAME_RE.pattern} (single path component, no traversal)"
        )
    return cleaned.lower()


def resolve_profile_home(profile: Optional[str]) -> Path:
    """Return the HERMES_HOME for ``profile``.

    * Empty / None / "default" / "lumi" → the gateway's HERMES_HOME.
    * Other names → ``<hermes_root>/profiles/<name>`` (created if missing).

    Raises ProfileRoutingError for unsafe names.
    """
    name = _validate_profile_name(profile or "")
    if name in _LOCAL_PROFILES:
        return get_hermes_home().resolve()

    root = _hermes_root()
    home = (root / "profiles" / name).resolve()
    # Defence in depth — even after regex validation, refuse to escape
    # the profiles/ subtree if someone changes the regex later.
    try:
        home.relative_to((root / "profiles").resolve())
    except ValueError as exc:
        raise ProfileRoutingError(
            f"Profile home {home} escapes profiles/ sandbox"
        ) from exc
    home.mkdir(parents=True, exist_ok=True)
    return home


def get_active_profile_name() -> str:
    """Return the profile name the current process is running in.

    * HERMES_HOME == <root>/.hermes → "default"
    * HERMES_HOME == <root>/.hermes/profiles/<name> → "<name>"
    * Anything else (Docker / custom HERMES_HOME) → "default"
    """
    home = get_hermes_home().resolve()
    root = _hermes_root()
    try:
        rel = home.relative_to((root / "profiles").resolve())
    except ValueError:
        return "default"
    parts = rel.parts
    if not parts:
        return "default"
    return parts[0]


def resolve_script_path(profile: Optional[str], script_path: str) -> Path:
    """Resolve ``script_path`` against the target profile's scripts/ dir.

    Resolution order:
      1. If ``script_path`` is absolute: validate it lives inside the
         target profile's ``scripts/`` dir and return it resolved.
      2. Else: try ``<target_home>/scripts/<script_path>``. If that file
         exists, return it.
      3. Else: try ``<gateway_home>/scripts/<script_path>``. If that
         exists, return it (legacy fallback so jobs that lived entirely
         in the gateway don't break when re-routed).
      4. Else: raise ``ScriptNotFoundError`` listing both candidates.

    Raises ProfileRoutingError for unsafe absolute paths.
    """
    if not script_path or not str(script_path).strip():
        raise ProfileRoutingError("Empty script path")

    target_home = resolve_profile_home(profile)
    gateway_home = get_hermes_home().resolve()

    raw = Path(str(script_path).strip()).expanduser()

    if raw.is_absolute():
        path = raw.resolve()
        target_scripts = (target_home / "scripts").resolve()
        target_scripts.mkdir(parents=True, exist_ok=True)
        try:
            path.relative_to(target_scripts)
        except ValueError as exc:
            raise ProfileRoutingError(
                f"Absolute script path {path} must live inside "
                f"the target profile's scripts dir ({target_scripts})"
            ) from exc
        if not path.is_file():
            raise ScriptNotFoundError(
                f"Script not found: {path} (target={target_scripts})"
            )
        return path

    # Relative: try target first, then gateway.
    target_candidate = (target_home / "scripts" / raw).resolve()
    gateway_candidate = (gateway_home / "scripts" / raw).resolve()

    if target_candidate.is_file():
        return target_candidate
    if gateway_candidate.is_file():
        return gateway_candidate

    raise ScriptNotFoundError(
        f"Script not found: {script_path!r} "
        f"(target={target_candidate}, gateway={gateway_candidate})"
    )


def decide_routing(job: dict) -> RoutingDecision:
    """Decide how ``job`` should be executed.

    See ``RoutingDecision`` for the action vocabulary.
    """
    raw_profile = job.get("profile") if job else None
    profile = _validate_profile_name(str(raw_profile) if raw_profile is not None else "")
    active = get_active_profile_name()

    if profile in _LOCAL_PROFILES or profile == active:
        # Preserve the input intent in `profile`: an empty input profile
        # stays empty so logs read "profile=unset" rather than the
        # misleadingly-precise "profile=default".
        return RoutingDecision(
            action="in_process",
            profile=profile,
            target_home=None,
            reason=(
                f"profile={profile or 'unset'} matches gateway "
                f"(active={active}); running in-process"
            ),
        )

    target = (_hermes_root() / "profiles" / profile).resolve()
    if not target.is_dir():
        return RoutingDecision(
            action="unreachable",
            profile=profile,
            target_home=None,
            reason=(
                f"profile={profile!r} has no profile directory "
                f"({target}); mark job and skip"
            ),
        )

    return RoutingDecision(
        action="delegate",
        profile=profile,
        target_home=target.resolve(),
        reason=(
            f"profile={profile!r} differs from gateway "
            f"(active={active}); delegating to {target}"
        ),
    )


# ---------------------------------------------------------------------------
# CLI summary helpers
# ---------------------------------------------------------------------------


def build_job_summary(job: dict) -> dict:
    """Return a CLI-friendly dict for one job.

    Fields:
    * job_id, name — from the job record
    * profile — normalized profile name (empty string if unset)
    * unreachable — True iff profile is set, non-local, and the profile
      directory is missing on this host
    * last_status, last_error, last_run_at, last_delivery_error — copy
      through so the CLI can show recent health
    * next_run_at, schedule_display — scheduling state
    * no_agent, script — execution mode marker
    """
    profile = _validate_profile_name(str(job.get("profile") or "")) if job else ""
    unreachable = False
    if profile and profile not in _LOCAL_PROFILES:
        target = (_hermes_root() / "profiles" / profile).resolve()
        unreachable = not target.is_dir()

    return {
        "job_id": str(job.get("id") or ""),
        "name": str(job.get("name") or job.get("id") or ""),
        "profile": profile,
        "unreachable": unreachable,
        "last_status": job.get("last_status") if job else None,
        "last_error": job.get("last_error") if job else None,
        "last_run_at": job.get("last_run_at") if job else None,
        "last_delivery_error": job.get("last_delivery_error") if job else None,
        "next_run_at": job.get("next_run_at") if job else None,
        "schedule_display": (
            job.get("schedule_display")
            or (job.get("schedule") or {}).get("display")
            or (job.get("schedule") or {}).get("expr")
            if job
            else None
        ),
        "no_agent": bool(job.get("no_agent")) if job else False,
        "script": job.get("script") if job else None,
        "enabled": bool(job.get("enabled", True)) if job else True,
    }


def count_jobs_by_profile(jobs) -> dict:
    """Return ``{profile_name: count}`` over a list of job dicts.

    Empty / missing ``profile`` buckets together as ``""`` so the CLI
    can show "no profile set" vs a per-profile breakdown.
    """
    counts: dict = {}
    for job in jobs or []:
        if not job:
            key = ""
        else:
            raw = job.get("profile")
            key = _validate_profile_name(str(raw)) if raw else ""
        counts[key] = counts.get(key, 0) + 1
    return counts
