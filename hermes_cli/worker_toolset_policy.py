"""Fail-closed toolset boundary shared by delegated and Kanban workers.

Top-level profile agents are intentionally outside this policy.  The protected
set is a code-level floor so a missing or malformed profile config cannot leak
these privileged MCP identities into a child process.  Operators may extend
the floor with ``delegation.blocked_worker_toolsets``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


PROTECTED_WORKER_TOOLSETS = frozenset(
    {
        "mcp-web_filip_staging",
        "mcp-web_studios_staging",
        "mcp-web_media_staging",
    }
)


def _configured_blocked_toolsets(delegation_config: Any) -> set[str]:
    if not isinstance(delegation_config, Mapping):
        return set()
    raw = delegation_config.get("blocked_worker_toolsets", ())
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return set()
    return {item.strip() for item in raw if isinstance(item, str) and item.strip()}


def _toolset_variants(name: str) -> set[str]:
    """Return canonical/raw MCP variants, including a live registry alias."""
    value = str(name or "").strip()
    if not value:
        return set()

    variants = {value}
    if value.startswith("mcp-"):
        variants.add(value[4:])
    else:
        variants.add(f"mcp-{value}")

    # Dynamic MCP registration maps raw server names to mcp-<server>.  The
    # registry lookup covers any future non-standard alias while the prefix
    # fallback keeps this helper safe before registry initialisation.
    try:
        from tools.registry import registry

        target = registry.get_toolset_alias_target(value)
    except Exception:
        target = None
    if target:
        target_value = str(target).strip()
        if target_value:
            variants.add(target_value)
            if target_value.startswith("mcp-"):
                variants.add(target_value[4:])
    return variants


def blocked_worker_toolsets(delegation_config: Any = None) -> set[str]:
    """Return the immutable floor plus any profile-specific additions."""
    configured = _configured_blocked_toolsets(delegation_config)
    blocked: set[str] = set()
    for name in PROTECTED_WORKER_TOOLSETS | configured:
        blocked.update(_toolset_variants(name))
    return blocked


def filter_worker_toolsets(
    toolsets: Iterable[str], delegation_config: Any = None
) -> list[str]:
    """Remove protected toolsets while preserving order and unrelated MCPs."""
    blocked = blocked_worker_toolsets(delegation_config)
    result: list[str] = []
    seen: set[str] = set()
    for toolset in toolsets:
        name = str(toolset or "").strip()
        if not name or name in seen:
            continue
        if _toolset_variants(name) & blocked:
            continue
        result.append(name)
        seen.add(name)
    return result
