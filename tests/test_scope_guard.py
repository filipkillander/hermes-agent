"""Tests for P0-040 scope guard infrastructure.

Covers SCOPE-001 (webhook toolset restriction), SCOPE-002 (cron mutation
scope), SCOPE-003 (context gate), SCOPE-004 (delegate_task scope
inheritance), and SCOPE-005 (background quiescence helpers).
"""

import asyncio
import pytest

from agent.scope_guard import (
    ContextGateStop,
    MutationScope,
    WEBHOOK_FORBIDDEN_TOOLSETS,
    check_context_gate,
    child_scope_from_parent,
    child_write_set_from_parent,
    enforce_read_only_toolsets,
    filter_webhook_toolsets,
    get_hard_token_limit,
    quiesce_background_activities,
    resolve_mutation_scope,
)


# ── SCOPE-001: Webhook toolset restriction ────────────────────────────────

class TestWebhookToolsetRestriction:
    """SCOPE-001: forbidden mutation toolsets are stripped from webhook."""

    def test_forbidden_toolsets_stripped(self):
        toolsets = ["web", "file", "terminal", "skills", "kanban", "memory", "cronjob"]
        filtered = filter_webhook_toolsets(toolsets)
        assert "file" not in filtered
        assert "terminal" not in filtered
        assert "kanban" not in filtered
        assert "memory" not in filtered
        assert "cronjob" not in filtered
        assert "web" in filtered
        assert "skills" in filtered

    def test_no_forbidden_toolsets_unchanged(self):
        toolsets = ["web", "skills", "session_search"]
        assert filter_webhook_toolsets(toolsets) == toolsets

    def test_empty_toolsets(self):
        assert filter_webhook_toolsets([]) == []

    def test_deduplication_preserved(self):
        toolsets = ["web", "web", "file", "skills"]
        filtered = filter_webhook_toolsets(toolsets)
        assert filtered == ["web", "skills"]

    def test_forbidden_set_is_complete(self):
        # Ensure we cover all 5 mutation toolsets from the contract
        assert WEBHOOK_FORBIDDEN_TOOLSETS == frozenset({
            "file", "terminal", "kanban", "memory", "cronjob"
        })


# ── SCOPE-002 + SCOPE-004: Mutation scope model ───────────────────────────

class TestMutationScope:
    """SCOPE-002 / SCOPE-004: MutationScope enum parsing and defaults."""

    def test_parse_read_only(self):
        assert resolve_mutation_scope("read-only") is MutationScope.READ_ONLY
        assert resolve_mutation_scope(MutationScope.READ_ONLY) is MutationScope.READ_ONLY

    def test_parse_exact_write_set(self):
        assert resolve_mutation_scope("exact-write-set") is MutationScope.EXACT_WRITE_SET

    def test_parse_full(self):
        assert resolve_mutation_scope("full") is MutationScope.FULL

    def test_parse_none_defaults_full(self):
        assert resolve_mutation_scope(None) is MutationScope.FULL

    def test_parse_invalid_defaults_full(self):
        assert resolve_mutation_scope("bogus") is MutationScope.FULL

    def test_is_read_only_property(self):
        assert MutationScope.READ_ONLY.is_read_only is True
        assert MutationScope.FULL.is_read_only is False

    def test_allows_full_write_property(self):
        assert MutationScope.FULL.allows_full_write is True
        assert MutationScope.READ_ONLY.allows_full_write is False


class TestEnforceReadOnlyToolsets:
    """SCOPE-002: read-only scope strips write-capable toolsets."""

    def test_strips_write_toolsets(self):
        toolsets = ["web", "file", "terminal", "skills", "kanban", "memory"]
        filtered = enforce_read_only_toolsets(toolsets)
        assert "file" not in filtered
        assert "terminal" not in filtered
        assert "kanban" not in filtered
        assert "memory" not in filtered
        assert "web" in filtered
        assert "skills" in filtered

    def test_already_read_only_unchanged(self):
        toolsets = ["web", "skills", "session_search"]
        assert enforce_read_only_toolsets(toolsets) == toolsets


# ── SCOPE-004: Child scope inheritance ────────────────────────────────────

class TestChildScopeInheritance:
    """SCOPE-004: child scope can only narrow, never expand."""

    def test_read_only_parent_forces_read_only_child(self):
        result = child_scope_from_parent(MutationScope.READ_ONLY, MutationScope.FULL)
        assert result is MutationScope.READ_ONLY

    def test_read_only_parent_no_request(self):
        result = child_scope_from_parent(MutationScope.READ_ONLY, None)
        assert result is MutationScope.READ_ONLY

    def test_exact_write_set_parent_blocks_full_child(self):
        result = child_scope_from_parent(MutationScope.EXACT_WRITE_SET, MutationScope.FULL)
        assert result is MutationScope.EXACT_WRITE_SET

    def test_exact_write_set_parent_allows_read_only_child(self):
        result = child_scope_from_parent(MutationScope.EXACT_WRITE_SET, MutationScope.READ_ONLY)
        assert result is MutationScope.READ_ONLY

    def test_exact_write_set_parent_allows_exact_child(self):
        result = child_scope_from_parent(MutationScope.EXACT_WRITE_SET, MutationScope.EXACT_WRITE_SET)
        assert result is MutationScope.EXACT_WRITE_SET

    def test_full_parent_allows_any_child(self):
        assert child_scope_from_parent(MutationScope.FULL, MutationScope.READ_ONLY) is MutationScope.READ_ONLY
        assert child_scope_from_parent(MutationScope.FULL, MutationScope.EXACT_WRITE_SET) is MutationScope.EXACT_WRITE_SET
        assert child_scope_from_parent(MutationScope.FULL, MutationScope.FULL) is MutationScope.FULL

    def test_full_parent_no_request_defaults_full(self):
        assert child_scope_from_parent(MutationScope.FULL, None) is MutationScope.FULL


class TestChildWriteSetInheritance:
    """SCOPE-004: child write_set must be a subset of parent's."""

    def test_child_subset_of_parent(self):
        parent = {"/tmp/a", "/tmp/b"}
        child = {"/tmp/a"}
        result = child_write_set_from_parent(parent, child)
        assert result == {"/tmp/a"}

    def test_child_disjoint_from_parent_yields_empty(self):
        parent = {"/tmp/a"}
        child = {"/tmp/x"}
        result = child_write_set_from_parent(parent, child)
        assert result == set()

    def test_child_none_inherits_parent(self):
        parent = {"/tmp/a", "/tmp/b"}
        result = child_write_set_from_parent(parent, None)
        assert result == {"/tmp/a", "/tmp/b"}

    def test_parent_none_child_used_as_is(self):
        child = {"/tmp/x"}
        result = child_write_set_from_parent(None, child)
        assert result == {"/tmp/x"}

    def test_both_none_yields_none(self):
        assert child_write_set_from_parent(None, None) is None


# ── SCOPE-003: Context gate ───────────────────────────────────────────────

class TestContextGate:
    """SCOPE-003: hard token limit check at turn start."""

    def test_no_config_no_gate(self):
        # No config = no gate (backward compatible)
        check_context_gate(999999999, config=None)

    def test_no_hard_limit_no_gate(self):
        config = {"agent": {}}
        check_context_gate(999999999, config=config)

    def test_under_limit_no_raise(self):
        config = {"agent": {"hard_token_limit": 100000}}
        check_context_gate(50000, config=config)

    def test_over_limit_raises(self):
        config = {"agent": {"hard_token_limit": 100000}}
        with pytest.raises(ContextGateStop) as exc_info:
            check_context_gate(150000, config=config)
        assert exc_info.value.observed_tokens == 150000
        assert exc_info.value.hard_limit == 100000

    def test_at_limit_no_raise(self):
        config = {"agent": {"hard_token_limit": 100000}}
        # At the limit (equal) should NOT raise — only over
        check_context_gate(100000, config=config)

    def test_limit_below_1000_clamped(self):
        config = {"agent": {"hard_token_limit": 100}}
        assert get_hard_token_limit(config) == 1000

    def test_invalid_limit_ignored(self):
        config = {"agent": {"hard_token_limit": "not-a-number"}}
        assert get_hard_token_limit(config) is None

    def test_gate_covers_all_turn_types(self):
        """The gate should apply to main, /goal, internal, and background turns.
        This is a contract-level test — the actual coverage is in the
        conversation loop wiring.  Here we just verify the check function
        raises regardless of context.
        """
        config = {"agent": {"hard_token_limit": 10000}}
        with pytest.raises(ContextGateStop):
            check_context_gate(50000, config=config)


# ── SCOPE-005: Background quiescence ──────────────────────────────────────

class TestBackgroundQuiescence:
    """SCOPE-005: background activities are awaited/cancelled before final response."""

    @pytest.mark.asyncio
    async def test_no_tasks_no_op(self):
        await quiesce_background_activities([])

    @pytest.mark.asyncio
    async def test_completed_tasks_no_op(self):
        loop = asyncio.get_event_loop()
        task = loop.create_task(asyncio.sleep(0))
        await task
        await quiesce_background_activities([task])

    @pytest.mark.asyncio
    async def test_pending_task_awaited(self):
        loop = asyncio.get_event_loop()
        completed = loop.create_task(asyncio.sleep(0.01))
        await quiesce_background_activities([completed], timeout=1.0)
        assert completed.done()

    @pytest.mark.asyncio
    async def test_straggler_task_cancelled(self):
        loop = asyncio.get_event_loop()
        straggler = loop.create_task(asyncio.sleep(100))
        await quiesce_background_activities([straggler], timeout=0.05, cancel=True)
        assert straggler.cancelled()

    @pytest.mark.asyncio
    async def test_straggler_not_cancelled_when_cancel_false(self):
        loop = asyncio.get_event_loop()
        straggler = loop.create_task(asyncio.sleep(100))
        await quiesce_background_activities([straggler], timeout=0.05, cancel=False)
        # Task should still be running (not cancelled)
        assert not straggler.done()
        straggler.cancel()  # cleanup
