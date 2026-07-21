"""Deterministic routing: per-turn injection receipts and fail-closed guards.

P0-050 — fixes 7 fail-open gaps in the system-prompt assembly pipeline:

* GAP-001/002: SOUL.md / USER.md missing → silent fallback (no error)
* GAP-003:    Memory stale by design (not re-read mid-session)
* GAP-004:    No skill is "always required" (formatting-harness can vanish)
* GAP-005:    No channel brief injection (out of scope for this module)
* GAP-006/7:  No per-turn receipt, no fail-closed option

This module provides:

* :class:`BlockedRequiredMaterial` — raised when a required material is
  missing and ``required_material_fail_closed`` is ``True`` in config.
* :func:`compute_injection_receipt` — SHA-256 hashes of each prompt tier
  component, stored on ``agent._last_injection_receipt``.
* :func:`enforce_required_skills` — validates ``agent.required_skills`` are
  present and parseable; raises :class:`BlockedRequiredMaterial` if any is
  missing or corrupt.

All fail-closed behaviour is **opt-in** via config.yaml ``agent`` section::

    agent:
      required_skills: [formatting-harness]
      required_material_fail_closed: true

When ``required_material_fail_closed`` is absent or ``False`` (default),
behaviour is unchanged — silent fallback / skip, preserving full backward
compatibility.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class BlockedRequiredMaterial(RuntimeError):
    """Raised when a required material is missing and fail-closed is enabled.

    The ``material`` attribute names what was missing (e.g. ``"SOUL.md"``,
    ``"USER.md"``, ``"skill:formatting-harness"``).  The ``reason`` attribute
    gives a short diagnostic string.
    """

    def __init__(self, material: str, reason: str = ""):
        self.material = material
        self.reason = reason
        msg = f"BLOCKED_REQUIRED_MATERIAL: {material}"
        if reason:
            msg += f" — {reason}"
        super().__init__(msg)

    def to_dict(self) -> Dict[str, str]:
        return {"blocked": True, "material": self.material, "reason": self.reason}


# ── Config helpers ──────────────────────────────────────────────────────────


def _get_agent_config_value(agent: Any, key: str, default: Any = None) -> Any:
    """Read a value from ``agent._p050_config`` (populated at init from config.yaml).

    Falls back to ``default`` when the config dict or key is absent.
    """
    cfg = getattr(agent, "_p050_config", None)
    if not isinstance(cfg, dict):
        return default
    return cfg.get(key, default)


def is_fail_closed_enabled(agent: Any) -> bool:
    """True when ``agent.required_material_fail_closed`` is truthy in config."""
    return bool(_get_agent_config_value(agent, "required_material_fail_closed", False))


def get_required_skills(agent: Any) -> List[str]:
    """Return the list of required skill names from config, or empty list."""
    raw = _get_agent_config_value(agent, "required_skills", [])
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    return [str(s).strip() for s in raw if str(s).strip()]


# ── ROUTE-001: Per-turn injection receipt ──────────────────────────────────


def _sha256_hex(data: str) -> str:
    """Return the hex SHA-256 digest of *data* (UTF-8 encoded)."""
    return hashlib.sha256(data.encode("utf-8", errors="replace")).hexdigest()


def compute_injection_receipt(
    *,
    persona: str,
    user_context: str,
    memory: str,
    skills: str,
    stable: str,
    context: str,
    volatile: str,
) -> Dict[str, Any]:
    """Compute a structured per-turn injection receipt.

    Returns a dict with SHA-256 hashes of each tier component:

    .. code-block:: json

        {
          "version": 1,
          "persona_sha256": "...",
          "user_context_sha256": "...",
          "memory_sha256": "...",
          "skills_sha256": "...",
          "stable_sha256": "...",
          "context_sha256": "...",
          "volatile_sha256": "...",
          "persona_present": true,
          "user_context_present": false,
          "memory_present": true,
          "skills_present": true
        }

    The receipt is lightweight (7 × 64-char hex strings + booleans) and
    stored on ``agent._last_injection_receipt`` by
    :func:`agent.system_prompt.build_system_prompt_parts`.
    """
    return {
        "version": 1,
        "persona_sha256": _sha256_hex(persona),
        "user_context_sha256": _sha256_hex(user_context),
        "memory_sha256": _sha256_hex(memory),
        "skills_sha256": _sha256_hex(skills),
        "stable_sha256": _sha256_hex(stable),
        "context_sha256": _sha256_hex(context),
        "volatile_sha256": _sha256_hex(volatile),
        "persona_present": bool(persona),
        "user_context_present": bool(user_context),
        "memory_present": bool(memory),
        "skills_present": bool(skills),
    }


# ── ROUTE-002: Fail-closed for SOUL.md ──────────────────────────────────────


def check_persona_fail_closed(agent: Any, soul_content: Optional[str]) -> None:
    """Raise :class:`BlockedRequiredMaterial` if SOUL.md is missing and fail-closed.

    Called from ``build_system_prompt_parts`` after attempting to load
    SOUL.md.  When ``soul_content`` is falsy (missing/empty) and
    ``required_material_fail_closed`` is enabled, raises instead of silently
    falling back to :data:`DEFAULT_AGENT_IDENTITY`.

    When fail-closed is off (default), this is a no-op — full backward
    compatibility.
    """
    if soul_content:
        return
    if not is_fail_closed_enabled(agent):
        return
    raise BlockedRequiredMaterial(
        "SOUL.md",
        "persona file missing or empty and required_material_fail_closed=true",
    )


# ── ROUTE-003: Fail-closed for USER.md / MEMORY.md ──────────────────────────


def check_memory_fail_closed(
    agent: Any,
    *,
    user_present: bool,
    memory_present: bool,
) -> None:
    """Raise :class:`BlockedRequiredMaterial` if USER.md/MEMORY.md missing and fail-closed.

    Called after ``MemoryStore.load_from_disk()`` in agent_init.
    When fail-closed is enabled and either file is missing (no entries
    loaded), raises instead of silently skipping.

    The check is per-material: the exception names *which* file is missing.
    If both are missing, USER.md is reported first (persona-adjacent context).
    """
    if not is_fail_closed_enabled(agent):
        return
    if not user_present:
        raise BlockedRequiredMaterial(
            "USER.md",
            "user profile missing and required_material_fail_closed=true",
        )
    if not memory_present:
        raise BlockedRequiredMaterial(
            "MEMORY.md",
            "memory file missing and required_material_fail_closed=true",
        )


# ── ROUTE-004: Required-skills enforcement ──────────────────────────────────


def _skill_is_present(skill_name: str) -> bool:
    """Check whether *skill_name* has a parseable SKILL.md in any skills dir.

    Uses the existing skill discovery utilities to walk all configured
    skills directories (local + external).  A skill is "present" when its
    directory exists and contains a SKILL.md with valid YAML frontmatter
    (at minimum a ``name`` field or a non-empty body).
    """
    from agent.skill_utils import (
        get_all_skills_dirs,
        iter_skill_index_files,
        parse_frontmatter,
    )

    # Plugin-qualified names (namespace:skill) — check the bare part.
    if ":" in skill_name:
        _, skill_name = skill_name.split(":", 1)

    for skills_dir in get_all_skills_dirs():
        if not skills_dir.is_dir():
            continue
        for skill_file in iter_skill_index_files(skills_dir, "SKILL.md"):
            if skill_file.parent.name == skill_name:
                try:
                    raw = skill_file.read_text(encoding="utf-8")
                    frontmatter, body = parse_frontmatter(raw)
                    fm_name = frontmatter.get("name") or skill_file.parent.name
                    if fm_name == skill_name or skill_file.parent.name == skill_name:
                        # Must have at least some content (body or frontmatter)
                        if body.strip() or frontmatter:
                            return True
                except Exception:
                    continue
    return False


def enforce_required_skills(agent: Any) -> List[str]:
    """Validate that all ``agent.required_skills`` are present and parseable.

    Returns the list of validated skill names.  Raises
    :class:`BlockedRequiredMaterial` when any required skill is missing or
    corrupt, regardless of the ``required_material_fail_closed`` setting —
    a required skill being absent is always a blocking condition (the whole
    point of "required" is that it cannot silently disappear).

    However, when ``required_material_fail_closed`` is ``False`` (default),
    the function logs a warning and returns the missing names instead of
    raising, preserving backward compatibility for users who haven't opted
    into fail-closed behaviour.
    """
    required = get_required_skills(agent)
    if not required:
        return []

    missing: List[str] = []
    for skill_name in required:
        if not _skill_is_present(skill_name):
            missing.append(skill_name)

    if not missing:
        return required

    if is_fail_closed_enabled(agent):
        raise BlockedRequiredMaterial(
            f"skill:{missing[0]}",
            f"required skill(s) missing or corrupt: {', '.join(missing)}",
        )

    # Fail-open (default): log warning, don't raise.
    logger.warning(
        "Required skills missing but required_material_fail_closed is off: %s",
        ", ".join(missing),
    )
    return required


__all__ = [
    "BlockedRequiredMaterial",
    "compute_injection_receipt",
    "check_persona_fail_closed",
    "check_memory_fail_closed",
    "enforce_required_skills",
    "is_fail_closed_enabled",
    "get_required_skills",
]
