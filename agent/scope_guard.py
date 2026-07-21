"""Scope guard infrastructure for P0-040.

Provides the shared building blocks for scope inheritance, context gate,
and background quiescence enforcement:

* ``ContextGateStop`` - exception raised when observed tokens exceed the
  configured hard limit (SCOPE-003).
* ``MutationScope`` - the scope authority model (read-only /
  exact-write-set / full) used by SCOPE-002 (cron) and SCOPE-004
  (delegate_task scope inheritance).
* ``WEBHOOK_FORBIDDEN_TOOLSETS`` - the set of mutation toolsets stripped
  from the webhook platform toolset (SCOPE-001).
* ``filter_webhook_toolsets()`` - removes forbidden toolsets from a
  webhook toolset list (SCOPE-001 enforcement).
* ``resolve_mutation_scope()`` - parse a raw scope declaration into a
  ``MutationScope`` enum value (SCOPE-002 / SCOPE-004).
* ``child_scope_from_parent()`` - compute a child's effective scope from
  the parent's scope and the child's requested scope, enforcing
  "child can only narrow, never expand" (SCOPE-004).
* ``quiesce_background_activities()`` - await / cancel background tasks
  before sending the final visible response (SCOPE-005).

The module is intentionally policy-only and dependency-free: it never
imports heavy gateway/agent modules at top level so it can be used from
both the cron runner and the conversation loop without circular import
risk.
"""

from __future__ import annotations

import enum
import logging
from typing import Any, Iterable, Optional, Set

logger = logging.getLogger(__name__)


# -- SCOPE-001: Webhook toolset restriction ---------------------------------

# Toolsets that can mutate the filesystem, run commands, modify state, or
# schedule recurring work.  These are stripped from the webhook platform
# toolset so incoming webhooks cannot trigger mutations.
WEBHOOK_FORBIDDEN_TOOLSETS: frozenset[str] = frozenset({
    "file",       # read/write/patch/search - filesystem mutation
    "terminal",   # shell command execution
    "kanban",     # kanban board mutation
    "memory",     # persistent memory mutation
    "cronjob",    # create/modify cron jobs
})


def filter_webhook_toolsets(toolsets: Iterable[str]) -> list[str]:
    """Remove forbidden mutation toolsets from a webhook toolset list.

    Preserves order and deduplicates.  Non-webhook callers are unaffected.
    """
    seen: Set[str] = set()
    result: list[str] = []
    for ts in toolsets:
        name = str(ts).strip()
        if not name or name in seen:
            continue
        if name in WEBHOOK_FORBIDDEN_TOOLSETS:
            logger.info(
                "scope_guard: stripping forbidden toolset '%s' from webhook "
                "platform toolset (SCOPE-001)",
                name,
            )
            continue
        seen.add(name)
        result.append(name)
    return result


# -- SCOPE-002 + SCOPE-004: Mutation scope model ----------------------------


class MutationScope(str, enum.Enum):
    """Authority level for an agent turn, cron job, or delegated child.

    * ``READ_ONLY`` - no mutations allowed (no write-capable toolsets).
    * ``EXACT_WRITE_SET`` - mutations limited to a declared set of paths /
      toolsets.
    * ``FULL`` - unrestricted (default for backward compatibility).
    """

    READ_ONLY = "read-only"
    EXACT_WRITE_SET = "exact-write-set"
    FULL = "full"

    @classmethod
    def parse(cls, raw: Any) -> "MutationScope":
        """Parse a raw scope declaration into a MutationScope.

        Accepts the enum value strings, the enum members, or None
        (defaults to FULL for backward compatibility).
        """
        if raw is None:
            return cls.FULL
        if isinstance(raw, cls):
            return raw
        token = str(raw).strip().lower()
        for member in cls:
            if member.value == token:
                return member
        logger.warning(
            "scope_guard: unrecognized mutation_scope %r, defaulting to FULL",
            raw,
        )
        return cls.FULL

    @property
    def is_read_only(self) -> bool:
        return self is MutationScope.READ_ONLY

    @property
    def allows_full_write(self) -> bool:
        return self is MutationScope.FULL


# Toolsets that perform writes / mutations.  Used to enforce read-only scope.
_WRITE_CAPABLE_TOOLSETS: frozenset[str] = frozenset({
    "file", "terminal", "kanban", "memory", "cronjob", "delegation",
})


def enforce_read_only_toolsets(toolsets: Iterable[str]) -> list[str]:
    """Strip write-capable toolsets, keeping only read-only ones.

    Used when a parent or cron job declares ``read-only`` scope.
    """
    result: list[str] = []
    for ts in toolsets:
        name = str(ts).strip()
        if name and name not in _WRITE_CAPABLE_TOOLSETS:
            result.append(name)
        else:
            logger.debug(
                "scope_guard: stripping write-capable toolset '%s' for "
                "read-only scope",
                name,
            )
    return result


def child_scope_from_parent(
    parent_scope: MutationScope,
    child_requested: Optional[MutationScope] = None,
) -> MutationScope:
    """Compute the child's effective scope.

    The child can only narrow, never expand:
      - If parent is READ_ONLY, child is READ_ONLY regardless of request.
      - If parent is EXACT_WRITE_SET, child can be READ_ONLY or
        EXACT_WRITE_SET (never FULL).
      - If parent is FULL, child can be any scope.
    """
    if parent_scope is MutationScope.READ_ONLY:
        return MutationScope.READ_ONLY
    if parent_scope is MutationScope.EXACT_WRITE_SET:
        if child_requested is None or child_requested is MutationScope.FULL:
            return MutationScope.EXACT_WRITE_SET
        return child_requested  # READ_ONLY or EXACT_WRITE_SET
    # parent is FULL
    return child_requested or MutationScope.FULL


def child_write_set_from_parent(
    parent_write_set: Optional[set[str]],
    child_write_set: Optional[set[str]],
) -> Optional[set[str]]:
    """Intersect the child's requested write_set with the parent's.

    The child's write_set can only be a subset of the parent's.  If the
    parent has no write_set constraint (None = unrestricted), the child's
    requested set is used as-is.  If the child requests None, it inherits
    the parent's set.
    """
    if parent_write_set is None:
        return child_write_set
    if child_write_set is None:
        return set(parent_write_set)
    return set(child_write_set) & set(parent_write_set)


# -- SCOPE-003: Context gate -----------------------------------------------


class ContextGateStop(Exception):
    """Raised when observed tokens exceed the configured hard limit.

    This is a controlled stop - the agent should not continue the turn.
    The exception carries the observed token count and the hard limit for
    diagnostic logging.
    """

    def __init__(self, observed_tokens: int, hard_limit: int, message: str = ""):
        self.observed_tokens = observed_tokens
        self.hard_limit = hard_limit
        super().__init__(
            message
            or f"Context gate: observed {observed_tokens:,} tokens exceed "
            f"hard limit {hard_limit:,}. Turn stopped."
        )


def get_hard_token_limit(config: Optional[dict] = None) -> Optional[int]:
    """Resolve the hard token limit from config.

    Reads ``agent.hard_token_limit`` from config.yaml.  Returns None when
    unset (no gate).  When set, the limit is clamped to a minimum of 1000
    to prevent misconfiguration crashes.
    """
    if config is None:
        return None
    agent_cfg = config.get("agent") if isinstance(config, dict) else None
    if not isinstance(agent_cfg, dict):
        return None
    raw = agent_cfg.get("hard_token_limit")
    if raw is None:
        return None
    try:
        limit = int(raw)
    except (TypeError, ValueError):
        logger.warning("scope_guard: invalid hard_token_limit %r, ignoring", raw)
        return None
    if limit < 1000:
        logger.warning(
            "scope_guard: hard_token_limit %d < 1000, clamping to 1000", limit
        )
        return 1000
    return limit


def check_context_gate(
    observed_tokens: int,
    config: Optional[dict] = None,
) -> None:
    """Raise ContextGateStop if observed_tokens exceed the hard limit.

    Called at turn start (SCOPE-003).  When the hard limit is not
    configured, this is a no-op (backward compatible).
    """
    hard_limit = get_hard_token_limit(config)
    if hard_limit is None:
        return
    if observed_tokens > hard_limit:
        logger.warning(
            "scope_guard: context gate triggered - %d tokens > %d hard limit",
            observed_tokens,
            hard_limit,
        )
        raise ContextGateStop(observed_tokens, hard_limit)


# -- SCOPE-005: Background quiescence --------------------------------------

async def quiesce_background_activities(
    background_tasks: Iterable[Any],
    *,
    timeout: float = 10.0,
    cancel: bool = True,
) -> None:
    """Await or cancel background activities before sending the final response.

    Called before the final visible response is delivered (SCOPE-005).
    Ensures no mutation may begin after the final response.

    Args:
        background_tasks: iterable of asyncio.Task / future objects.
        timeout: max seconds to wait for tasks to complete naturally.
        cancel: if True (default), cancel tasks that don't finish within
            timeout.  If False, just await with timeout and log stragglers.
    """
    import asyncio

    tasks = [t for t in background_tasks if t is not None]
    if not tasks:
        return

    pending = [t for t in tasks if not t.done()]
    if not pending:
        return

    logger.info(
        "scope_guard: quiescing %d background activities before final "
        "response (SCOPE-005, timeout=%.1fs, cancel=%s)",
        len(pending), timeout, cancel,
    )

    # Use asyncio.wait (NOT wait_for+gather) so that a timeout does NOT
    # cancel the tasks - we control cancellation explicitly below.
    done, stragglers = await asyncio.wait(pending, timeout=timeout)
    if not stragglers:
        logger.info("scope_guard: all background activities completed")
        return

    stragglers = list(stragglers)
    if cancel:
        logger.warning(
            "scope_guard: %d background activities did not finish in "
            "%.1fs - cancelling (SCOPE-005)",
            len(stragglers), timeout,
        )
        for t in stragglers:
            if not t.done():
                t.cancel()
        # Give cancelled tasks a brief grace period
        try:
            await asyncio.wait_for(
                asyncio.gather(*stragglers, return_exceptions=True),
                timeout=2.0,
            )
        except asyncio.TimeoutError:
            logger.error(
                "scope_guard: %d tasks still running after cancel - "
                "proceeding with final response",
                len([t for t in stragglers if not t.done()]),
            )
    else:
        logger.warning(
            "scope_guard: %d background activities did not finish in "
            "%.1fs - proceeding (cancel=False)",
            len(stragglers), timeout,
        )


__all__ = [
    "ContextGateStop",
    "MutationScope",
    "WEBHOOK_FORBIDDEN_TOOLSETS",
    "filter_webhook_toolsets",
    "enforce_read_only_toolsets",
    "resolve_mutation_scope",
    "child_scope_from_parent",
    "child_write_set_from_parent",
    "get_hard_token_limit",
    "check_context_gate",
    "quiesce_background_activities",
]


# Alias for consistency with the contract naming
resolve_mutation_scope = MutationScope.parse
