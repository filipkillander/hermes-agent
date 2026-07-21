"""Tests for agent/required_material.py — P0-050 deterministic routing."""

import hashlib
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.required_material import (
    BlockedRequiredMaterial,
    compute_injection_receipt,
    check_persona_fail_closed,
    check_memory_fail_closed,
    enforce_required_skills,
    is_fail_closed_enabled,
    get_required_skills,
    _sha256_hex,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_agent(**p050_overrides):
    """Create a minimal agent-like object with _p050_config."""
    config = {
        "required_skills": [],
        "required_material_fail_closed": False,
    }
    config.update(p050_overrides)
    return SimpleNamespace(_p050_config=config)


# ── BlockedRequiredMaterial ──────────────────────────────────────────────────


class TestBlockedRequiredMaterial:
    def test_basic_message(self):
        e = BlockedRequiredMaterial("SOUL.md")
        assert e.material == "SOUL.md"
        assert e.reason == ""
        assert "BLOCKED_REQUIRED_MATERIAL: SOUL.md" in str(e)

    def test_with_reason(self):
        e = BlockedRequiredMaterial("USER.md", "file missing")
        assert e.material == "USER.md"
        assert e.reason == "file missing"
        assert "file missing" in str(e)

    def test_is_runtime_error(self):
        e = BlockedRequiredMaterial("SOUL.md")
        assert isinstance(e, RuntimeError)

    def test_to_dict(self):
        e = BlockedRequiredMaterial("SOUL.md", "missing")
        d = e.to_dict()
        assert d == {"blocked": True, "material": "SOUL.md", "reason": "missing"}


# ── Config helpers ───────────────────────────────────────────────────────────


class TestConfigHelpers:
    def test_fail_closed_default_off(self):
        agent = _make_agent()
        assert is_fail_closed_enabled(agent) is False

    def test_fail_closed_explicit_true(self):
        agent = _make_agent(required_material_fail_closed=True)
        assert is_fail_closed_enabled(agent) is True

    def test_fail_closed_no_config(self):
        # Agent without _p050_config at all
        agent = SimpleNamespace()
        assert is_fail_closed_enabled(agent) is False

    def test_get_required_skills_empty(self):
        agent = _make_agent()
        assert get_required_skills(agent) == []

    def test_get_required_skills_list(self):
        agent = _make_agent(required_skills=["formatting-harness", "other-skill"])
        assert get_required_skills(agent) == ["formatting-harness", "other-skill"]

    def test_get_required_skills_string(self):
        agent = _make_agent(required_skills="formatting-harness")
        assert get_required_skills(agent) == ["formatting-harness"]

    def test_get_required_skills_none(self):
        agent = _make_agent(required_skills=None)
        assert get_required_skills(agent) == []

    def test_get_required_skills_strips_whitespace(self):
        agent = _make_agent(required_skills=["  formatting-harness  ", ""])
        assert get_required_skills(agent) == ["formatting-harness"]


# ── compute_injection_receipt (ROUTE-001) ────────────────────────────────────


class TestInjectionReceipt:
    def test_receipt_has_all_fields(self):
        receipt = compute_injection_receipt(
            persona="hello",
            user_context="world",
            memory="mem",
            skills="skills",
            stable="stable",
            context="context",
            volatile="volatile",
        )
        assert receipt["version"] == 1
        for key in ["persona_sha256", "user_context_sha256", "memory_sha256",
                     "skills_sha256", "stable_sha256", "context_sha256",
                     "volatile_sha256"]:
            assert len(receipt[key]) == 64  # SHA-256 hex

    def test_receipt_present_flags(self):
        receipt = compute_injection_receipt(
            persona="hello",
            user_context="",
            memory="mem",
            skills="skills",
            stable="stable",
            context="context",
            volatile="volatile",
        )
        assert receipt["persona_present"] is True
        assert receipt["user_context_present"] is False
        assert receipt["memory_present"] is True
        assert receipt["skills_present"] is True

    def test_receipt_all_empty(self):
        receipt = compute_injection_receipt(
            persona="", user_context="", memory="", skills="",
            stable="", context="", volatile="",
        )
        assert receipt["persona_present"] is False
        assert receipt["user_context_present"] is False
        assert receipt["memory_present"] is False
        assert receipt["skills_present"] is False

    def test_receipt_hashes_match_sha256(self):
        receipt = compute_injection_receipt(
            persona="test", user_context="", memory="", skills="",
            stable="", context="", volatile="",
        )
        expected = hashlib.sha256("test".encode("utf-8")).hexdigest()
        assert receipt["persona_sha256"] == expected

    def test_sha256_hex_known_value(self):
        assert _sha256_hex("abc") == hashlib.sha256(b"abc").hexdigest()

    def test_receipt_deterministic(self):
        kwargs = dict(
            persona="p", user_context="u", memory="m", skills="s",
            stable="st", context="ct", volatile="vt",
        )
        r1 = compute_injection_receipt(**kwargs)
        r2 = compute_injection_receipt(**kwargs)
        assert r1 == r2


# ── check_persona_fail_closed (ROUTE-002) ────────────────────────────────────


class TestPersonaFailClosed:
    def test_no_raise_when_content_present(self):
        agent = _make_agent(required_material_fail_closed=True)
        # Should not raise — soul_content is truthy
        check_persona_fail_closed(agent, "I am a persona")

    def test_no_raise_when_fail_closed_off(self):
        agent = _make_agent(required_material_fail_closed=False)
        # Should not raise — fail-closed is off
        check_persona_fail_closed(agent, None)

    def test_raise_when_missing_and_fail_closed(self):
        agent = _make_agent(required_material_fail_closed=True)
        with pytest.raises(BlockedRequiredMaterial) as exc_info:
            check_persona_fail_closed(agent, None)
        assert exc_info.value.material == "SOUL.md"

    def test_raise_when_empty_and_fail_closed(self):
        agent = _make_agent(required_material_fail_closed=True)
        with pytest.raises(BlockedRequiredMaterial):
            check_persona_fail_closed(agent, "")

    def test_no_raise_when_no_config(self):
        agent = SimpleNamespace()  # no _p050_config
        check_persona_fail_closed(agent, None)  # should not raise


# ── check_memory_fail_closed (ROUTE-003) ─────────────────────────────────────


class TestMemoryFailClosed:
    def test_no_raise_when_both_present(self):
        agent = _make_agent(required_material_fail_closed=True)
        check_memory_fail_closed(agent, user_present=True, memory_present=True)

    def test_no_raise_when_fail_closed_off(self):
        agent = _make_agent(required_material_fail_closed=False)
        check_memory_fail_closed(agent, user_present=False, memory_present=False)

    def test_raise_when_user_missing(self):
        agent = _make_agent(required_material_fail_closed=True)
        with pytest.raises(BlockedRequiredMaterial) as exc_info:
            check_memory_fail_closed(agent, user_present=False, memory_present=True)
        assert exc_info.value.material == "USER.md"

    def test_raise_when_memory_missing(self):
        agent = _make_agent(required_material_fail_closed=True)
        with pytest.raises(BlockedRequiredMaterial) as exc_info:
            check_memory_fail_closed(agent, user_present=True, memory_present=False)
        assert exc_info.value.material == "MEMORY.md"

    def test_user_checked_before_memory(self):
        agent = _make_agent(required_material_fail_closed=True)
        with pytest.raises(BlockedRequiredMaterial) as exc_info:
            check_memory_fail_closed(agent, user_present=False, memory_present=False)
        assert exc_info.value.material == "USER.md"  # USER.md reported first


# ── enforce_required_skills (ROUTE-004) ───────────────────────────────────────


class TestEnforceRequiredSkills:
    def test_empty_required_returns_empty(self):
        agent = _make_agent(required_skills=[])
        assert enforce_required_skills(agent) == []

    def test_no_required_returns_empty(self):
        agent = _make_agent()
        assert enforce_required_skills(agent) == []

    def test_fail_open_when_off_and_skill_missing(self):
        agent = _make_agent(
            required_skills=["nonexistent-skill-12345"],
            required_material_fail_closed=False,
        )
        # Should not raise, just log warning
        result = enforce_required_skills(agent)
        assert result == ["nonexistent-skill-12345"]

    def test_raise_when_fail_closed_and_skill_missing(self):
        agent = _make_agent(
            required_skills=["nonexistent-skill-12345"],
            required_material_fail_closed=True,
        )
        with pytest.raises(BlockedRequiredMaterial) as exc_info:
            enforce_required_skills(agent)
        assert "skill:nonexistent-skill-12345" in exc_info.value.material

    def test_present_skill_passes(self, tmp_path):
        # Create a real skill directory with SKILL.md
        import sys
        skills_dir = tmp_path / "skills"
        skill_dir = skills_dir / "test-skill-p050"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: test-skill-p050\ndescription: Test skill\n---\n\nTest body\n"
        )

        agent = _make_agent(
            required_skills=["test-skill-p050"],
            required_material_fail_closed=True,
        )

        with patch("agent.required_material._skill_is_present") as mock_present:
            mock_present.return_value = True
            result = enforce_required_skills(agent)
            assert "test-skill-p050" in result


# ── Integration: build_system_prompt_parts receipt ──────────────────────────


class TestSystemPromptReceiptIntegration:
    """Verify build_system_prompt_parts stores a receipt on the agent."""

    def test_receipt_stored_on_agent(self, monkeypatch):
        """build_system_prompt_parts should set agent._last_injection_receipt."""
        from agent.system_prompt import build_system_prompt_parts

        agent = SimpleNamespace(
            load_soul_identity=False,
            skip_context_files=False,
            valid_tool_names=[],
            _task_completion_guidance=False,
            _tool_use_enforcement=False,
            _environment_probe=False,
            _kanban_worker_guidance="",
            _memory_store=None,
            _memory_manager=None,
            model="",
            provider="",
            platform="",
            pass_session_id=False,
            session_id="",
            _p050_config={"required_skills": [], "required_material_fail_closed": False},
        )

        with (
            patch("run_agent.load_soul_md", return_value=""),
            patch("run_agent.build_nous_subscription_prompt", return_value=""),
            patch("run_agent.build_environment_hints", return_value=""),
            patch("run_agent.build_context_files_prompt", return_value=""),
        ):
            parts = build_system_prompt_parts(agent)

        # Receipt should be stored
        assert hasattr(agent, "_last_injection_receipt")
        assert agent._last_injection_receipt is not None
        receipt = agent._last_injection_receipt
        assert receipt["version"] == 1
        # Persona was empty (load_soul_md returned ""), so DEFAULT_AGENT_IDENTITY
        # was used as fallback
        assert receipt["persona_present"] is True  # DEFAULT_AGENT_IDENTITY is truthy
        assert receipt["user_context_present"] is False  # no memory store
        assert receipt["memory_present"] is False
        # Receipt should have valid SHA-256 hashes
        assert len(receipt["stable_sha256"]) == 64
        assert len(receipt["context_sha256"]) == 64
        assert len(receipt["volatile_sha256"]) == 64

    def test_receipt_persona_fail_closed_raises(self, monkeypatch):
        """When fail_closed is on and SOUL.md is empty, should raise."""
        from agent.system_prompt import build_system_prompt_parts

        agent = SimpleNamespace(
            load_soul_identity=False,
            skip_context_files=False,
            valid_tool_names=[],
            _task_completion_guidance=False,
            _tool_use_enforcement=False,
            _environment_probe=False,
            _kanban_worker_guidance="",
            _memory_store=None,
            _memory_manager=None,
            model="",
            provider="",
            platform="",
            pass_session_id=False,
            session_id="",
            _p050_config={"required_skills": [], "required_material_fail_closed": True},
        )

        with (
            patch("run_agent.load_soul_md", return_value=""),
            patch("run_agent.build_nous_subscription_prompt", return_value=""),
            patch("run_agent.build_environment_hints", return_value=""),
            patch("run_agent.build_context_files_prompt", return_value=""),
        ):
            with pytest.raises(BlockedRequiredMaterial) as exc_info:
                build_system_prompt_parts(agent)
            assert exc_info.value.material == "SOUL.md"
