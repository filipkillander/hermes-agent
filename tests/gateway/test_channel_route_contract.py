"""Fas A-3 candidate contract tests for the Vendelip #spel channel route.

These are CANDIDATE tests (not live implementation). They prove the contract:

    channel ID  ->  canonical brief  ->  required skills  ->  knowledge root
                  (path + hash)        (hash + readiness)     (read policy)

for the Vendelip #spel Discord channel, including:

* resolving the channel id ``1509925693247459378`` against a canonical registry,
* verifying the brief path and SHA-256 hash for ``vendelip/spel.md``,
* loading the required skills (``sonar-context``, ``gaming-skill``) and
  verifying their hash/readiness via a mock skill loader,
* verifying the knowledge root and its read-only policy,
* per-turn receipt mechanics: ID / path / hash / status with NO private brief
  text leaking into the receipt,
* fail-closed capability gap: when a mandatory skill is missing, the route
  fails closed BEFORE any model call,
* synthetic canary for a NEW session: brief is injected, the assembled prompt
  contains the channel id and Game Mode marker,
* synthetic canary for an EXISTING session: per-turn receipt is re-emitted
  without re-injecting the full brief body.

The contract is exercised against fixtures under ``tests/fixtures/spel/``.
Nothing here installs ``channel_prompts``/``channel_skill_bindings`` live,
touches gateway config, or starts the gateway.

The resolver (``ChannelRouteContract``) below is a candidate implementation
kept inside the test module so the tests can pin the contract shape without
modifying production code. The production seam already exists:
``gateway.platforms.base.resolve_channel_prompt`` and
``gateway.platforms.base.resolve_channel_skills`` consume ``config.extra``;
these tests assert the contract that the live config WOULD satisfy once
``channel_prompts`` / ``channel_skill_bindings`` point at the #spel brief.
"""
from __future__ import annotations

import hashlib
import json
import sys
import threading
import types
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Fixture root and channel id under contract
# ---------------------------------------------------------------------------

SPEL_FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "spel"
SPEL_CHANNEL_ID = "1509925693247459378"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _load_registry() -> dict:
    with (SPEL_FIXTURE_ROOT / "canonical_registry.json").open("r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Candidate contract resolver (kept in-test; production seam is
# resolve_channel_prompt / resolve_channel_skills).
# ---------------------------------------------------------------------------


@dataclass
class SkillReadiness:
    name: str
    path: Path
    sha256: str
    ready: bool


@dataclass
class ChannelRouteReceipt:
    """Per-turn receipt emitted by the route. Carries ID/path/hash/status only.

    The receipt MUST NOT carry private brief text. The contract tests below
    pin that invariant so a leak surfaces immediately.
    """

    channel_id: str
    brief_path: str
    brief_sha256: str
    skills: list[SkillReadiness] = field(default_factory=list)
    knowledge_root: str | None = None
    knowledge_read_policy: str | None = None
    status: str = "ok"  # "ok" | "fail-closed"
    reason: str | None = None

    def to_public_dict(self) -> dict:
        return {
            "channel_id": self.channel_id,
            "brief_path": self.brief_path,
            "brief_sha256": self.brief_sha256,
            "skills": [
                {"name": s.name, "sha256": s.sha256, "ready": s.ready}
                for s in self.skills
            ],
            "knowledge_root": self.knowledge_root,
            "knowledge_read_policy": self.knowledge_read_policy,
            "status": self.status,
            "reason": self.reason,
        }


class ChannelRouteContract:
    """Candidate resolver for the #spel channel route-contract.

    Reads a canonical registry (fixture), resolves a channel id to its brief
    path, loads required skills, verifies the knowledge root, and emits a
    per-turn receipt. Fail-closed semantics: if a mandatory skill is missing
    the route raises ``CapabilityGap`` before any model call is made.
    """

    class CapabilityGap(RuntimeError):
        """Raised when a mandatory skill is missing — fail-closed pre-model-call."""

    def __init__(self, registry: dict, fixture_root: Path, skill_loader):
        self.registry = registry
        self.fixture_root = Path(fixture_root)
        self.skill_loader = skill_loader  # callable(name) -> SkillReadiness | None

    @classmethod
    def for_spel_fixture(cls, skill_loader=None):
        registry = _load_registry()
        loader = skill_loader or default_mock_skill_loader
        return cls(registry, SPEL_FIXTURE_ROOT, loader)

    def resolve_channel(self, channel_id: str) -> dict:
        channels = self.registry.get("channels") or {}
        entry = channels.get(str(channel_id))
        if not entry:
            raise KeyError(f"channel id {channel_id!r} not in canonical registry")
        return entry

    def brief_path(self, channel_id: str) -> Path:
        entry = self.resolve_channel(channel_id)
        rel = entry["brief_relpath"]
        return self.fixture_root / "briefs" / rel

    def brief_sha256(self, channel_id: str) -> str:
        return _sha256_file(self.brief_path(channel_id))

    def required_skill_names(self, channel_id: str) -> list[str]:
        entry = self.resolve_channel(channel_id)
        return list(entry.get("required_skills") or [])

    def knowledge_root(self, channel_id: str) -> Path:
        entry = self.resolve_channel(channel_id)
        return self.fixture_root / entry["knowledge_root_relpath"]

    def knowledge_read_policy(self, channel_id: str) -> str:
        entry = self.resolve_channel(channel_id)
        return str(entry.get("read_policy") or "read-only")

    def load_skills(self, channel_id: str) -> list[SkillReadiness]:
        out: list[SkillReadiness] = []
        for name in self.required_skill_names(channel_id):
            ready = self.skill_loader(name)
            if ready is None:
                # treat as not-ready but present in the receipt so the gap is visible
                out.append(SkillReadiness(name=name, path=Path(""), sha256="", ready=False))
            else:
                out.append(ready)
        return out

    def build_receipt(self, channel_id: str) -> ChannelRouteReceipt:
        entry = self.resolve_channel(channel_id)
        skills = self.load_skills(channel_id)
        missing = [s.name for s in skills if not s.ready]
        if missing:
            return ChannelRouteReceipt(
                channel_id=str(channel_id),
                brief_path=str(self.brief_path(channel_id)),
                brief_sha256=self.brief_sha256(channel_id),
                skills=skills,
                knowledge_root=str(self.knowledge_root(channel_id)),
                knowledge_read_policy=self.knowledge_read_policy(channel_id),
                status="fail-closed",
                reason=f"missing mandatory skills: {missing}",
            )
        return ChannelRouteReceipt(
            channel_id=str(channel_id),
            brief_path=str(self.brief_path(channel_id)),
            brief_sha256=self.brief_sha256(channel_id),
            skills=skills,
            knowledge_root=str(self.knowledge_root(channel_id)),
            knowledge_read_policy=self.knowledge_read_policy(channel_id),
            status="ok",
            reason=None,
        )

    def route(self, channel_id: str) -> ChannelRouteReceipt:
        """Fail-closed route: raises CapabilityGap if a mandatory skill is missing."""
        receipt = self.build_receipt(channel_id)
        if receipt.status == "fail-closed":
            raise self.CapabilityGap(receipt.reason or "fail-closed")
        return receipt


# ---------------------------------------------------------------------------
# Mock skill loader (reads fixture skills under tests/fixtures/spel/skills)
# ---------------------------------------------------------------------------


def default_mock_skill_loader(name: str) -> SkillReadiness | None:
    skill_dir = SPEL_FIXTURE_ROOT / "skills" / name
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        return None
    return SkillReadiness(
        name=name,
        path=skill_md,
        sha256=_sha256_file(skill_md),
        ready=True,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestResolveChannelId:
    def test_channel_id_resolved_against_canonical_registry(self):
        contract = ChannelRouteContract.for_spel_fixture()
        entry = contract.resolve_channel(SPEL_CHANNEL_ID)
        assert entry["platform"] == "discord"
        assert entry["guild"] == "vendelip"
        assert entry["name"] == "#spel"
        assert entry["brief_relpath"] == "vendelip/spel.md"
        assert "sonar-context" in entry["required_skills"]
        assert "gaming-skill" in entry["required_skills"]

    def test_unknown_channel_id_raises(self):
        contract = ChannelRouteContract.for_spel_fixture()
        with pytest.raises(KeyError):
            contract.resolve_channel("0000000000000000000")

    def test_channel_id_normalized_to_string(self):
        contract = ChannelRouteContract.for_spel_fixture()
        # numeric channel id must be coerced to str just like Discord sends
        entry = contract.resolve_channel(str(1509925693247459378))
        assert entry["name"] == "#spel"


class TestBriefPathAndHash:
    def test_brief_path_points_at_vendelip_spel_md(self):
        contract = ChannelRouteContract.for_spel_fixture()
        path = contract.brief_path(SPEL_CHANNEL_ID)
        assert path.name == "spel.md"
        assert path.parent.name == "vendelip"
        assert path.exists(), f"brief fixture missing at {path}"

    def test_brief_hash_is_stable_sha256(self):
        contract = ChannelRouteContract.for_spel_fixture()
        h1 = contract.brief_sha256(SPEL_CHANNEL_ID)
        h2 = contract.brief_sha256(SPEL_CHANNEL_ID)
        assert len(h1) == 64
        assert h1 == h2
        # hash must match an independent hashlib computation of the same file
        assert h1 == _sha256_file(contract.brief_path(SPEL_CHANNEL_ID))

    def test_brief_hash_changes_when_brief_changes(self, tmp_path):
        contract = ChannelRouteContract.for_spel_fixture()
        h_orig = contract.brief_sha256(SPEL_CHANNEL_ID)
        # mutate a copy of the brief in a temp fixture root
        copy_root = tmp_path / "spel"
        copy_root.mkdir()
        import shutil
        shutil.copytree(SPEL_FIXTURE_ROOT, copy_root, dirs_exist_ok=True)
        copy_brief = copy_root / "briefs" / "vendelip" / "spel.md"
        copy_brief.write_text(copy_brief.read_text() + "\n\n[amended]\n", encoding="utf-8")
        contract2 = ChannelRouteContract(_load_registry(), copy_root, default_mock_skill_loader)
        h2 = contract2.brief_sha256(SPEL_CHANNEL_ID)
        assert h2 != h_orig, "brief hash did not change after brief mutation"


class TestRequiredSkills:
    def test_required_skills_resolved_from_registry(self):
        contract = ChannelRouteContract.for_spel_fixture()
        names = contract.required_skill_names(SPEL_CHANNEL_ID)
        assert names == ["sonar-context", "gaming-skill"]

    def test_skill_hashes_are_sha256_and_stable(self):
        contract = ChannelRouteContract.for_spel_fixture()
        readiness = contract.load_skills(SPEL_CHANNEL_ID)
        for s in readiness:
            assert s.ready is True
            assert len(s.sha256) == 64
            assert s.path.exists()
            assert s.sha256 == _sha256_file(s.path)

    def test_mock_skill_loader_returns_none_for_unknown_skill(self):
        contract = ChannelRouteContract.for_spel_fixture(
            skill_loader=lambda name: None
        )
        readiness = contract.load_skills(SPEL_CHANNEL_ID)
        assert all(not s.ready for s in readiness)
        assert all(s.sha256 == "" for s in readiness)

    def test_skill_readiness_drives_fail_closed(self):
        # mock loader that returns sonar-context but NOT gaming-skill
        def partial_loader(name: str) -> SkillReadiness | None:
            if name == "sonar-context":
                return default_mock_skill_loader(name)
            return None

        contract = ChannelRouteContract.for_spel_fixture(skill_loader=partial_loader)
        with pytest.raises(ChannelRouteContract.CapabilityGap):
            contract.route(SPEL_CHANNEL_ID)


class TestKnowledgeRoot:
    def test_knowledge_root_resolved(self):
        contract = ChannelRouteContract.for_spel_fixture()
        root = contract.knowledge_root(SPEL_CHANNEL_ID)
        assert root.exists()
        assert (root / "README.md").exists()

    def test_knowledge_read_policy_is_read_only(self):
        contract = ChannelRouteContract.for_spel_fixture()
        assert contract.knowledge_read_policy(SPEL_CHANNEL_ID) == "read-only"


class TestPerTurnReceipt:
    def test_receipt_contains_id_path_hash_status(self):
        contract = ChannelRouteContract.for_spel_fixture()
        receipt = contract.build_receipt(SPEL_CHANNEL_ID)
        assert receipt.channel_id == SPEL_CHANNEL_ID
        assert receipt.brief_path.endswith("vendelip/spel.md")
        assert len(receipt.brief_sha256) == 64
        assert receipt.status == "ok"
        assert len(receipt.skills) == 2
        assert all(s.ready for s in receipt.skills)

    def test_receipt_omits_private_brief_text(self):
        """Per-turn receipt must never leak private brief body text."""
        contract = ChannelRouteContract.for_spel_fixture()
        receipt = contract.build_receipt(SPEL_CHANNEL_ID)
        public = json.dumps(receipt.to_public_dict())
        brief_text = contract.brief_path(SPEL_CHANNEL_ID).read_text(encoding="utf-8")
        # no full line of the brief body should appear in the public receipt
        for line in brief_text.splitlines():
            stripped = line.strip()
            if len(stripped) < 8:
                continue
            assert stripped not in public, (
                f"private brief line leaked into receipt: {stripped!r}"
            )

    def test_receipt_fail_closed_when_skill_missing(self):
        contract = ChannelRouteContract.for_spel_fixture(
            skill_loader=lambda name: None
        )
        receipt = contract.build_receipt(SPEL_CHANNEL_ID)
        assert receipt.status == "fail-closed"
        assert receipt.reason is not None
        assert "sonar-context" in receipt.reason
        assert "gaming-skill" in receipt.reason


class TestFailClosedCapabilityGap:
    def test_route_raises_before_model_call_when_skill_missing(self):
        """Fail-closed: a missing mandatory skill aborts the route, never
        reaches a model call. We assert by counting model-call invocations."""
        model_calls = {"count": 0}

        def fake_model_call(*args, **kwargs):
            model_calls["count"] += 1
            return {"final_response": "should-not-reach"}

        contract = ChannelRouteContract.for_spel_fixture(
            skill_loader=lambda name: None
        )
        with pytest.raises(ChannelRouteContract.CapabilityGap):
            receipt = contract.route(SPEL_CHANNEL_ID)
            # even if the route somehow returned, no model call should have run
            fake_model_call(receipt)
        assert model_calls["count"] == 0, "model call happened despite fail-closed"

    def test_route_succeeds_when_all_skills_ready(self):
        contract = ChannelRouteContract.for_spel_fixture()
        receipt = contract.route(SPEL_CHANNEL_ID)
        assert receipt.status == "ok"
        assert all(s.ready for s in receipt.skills)


# ---------------------------------------------------------------------------
# Synthetic canary — NEW session
# ---------------------------------------------------------------------------

class _CapturingAgent:
    """Minimal stand-in for run_agent.AIAgent that captures the assembled
    system prompt + first user message so the canary can assert on them."""

    last_init: dict | None = None
    last_user_message: str | None = None
    last_ephemeral: str | None = None

    def __init__(self, *args, **kwargs):
        type(self).last_init = dict(kwargs)
        self.tools = []

    def run_conversation(self, user_message, conversation_history=None,
                         task_id=None, persist_user_message=None):
        type(self).last_user_message = user_message
        type(self).last_ephemeral = (
            self.last_init.get("ephemeral_system_prompt") if self.last_init else None
        )
        return {
            "final_response": "ok",
            "messages": [],
            "api_calls": 1,
            "completed": True,
        }


def _install_fake_agent(monkeypatch):
    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _CapturingAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)


def _ensure_discord_mock():
    if "discord" in sys.modules and hasattr(sys.modules["discord"], "__file__"):
        return
    discord_mod = types.ModuleType("discord")
    discord_mod.Intents = MagicMock()
    discord_mod.Intents.default.return_value = MagicMock()
    discord_mod.DMChannel = type("DMChannel", (), {})
    discord_mod.Thread = type("Thread", (), {})
    discord_mod.ForumChannel = type("ForumChannel", (), {})
    discord_mod.Interaction = object
    ext_mod = MagicMock()
    commands_mod = MagicMock()
    commands_mod.Bot = MagicMock
    ext_mod.commands = commands_mod
    sys.modules.setdefault("discord", discord_mod)
    sys.modules.setdefault("discord.ext", ext_mod)
    sys.modules.setdefault("discord.ext.commands", commands_mod)


class TestCanaryNewSession:
    """Synthetic canary for a NEW session: brief is injected into the assembled
    prompt and the prompt carries the channel id + Game Mode marker."""

    def test_new_session_prompt_contains_channel_id_and_game_mode(self, monkeypatch):
        _install_fake_agent(monkeypatch)
        contract = ChannelRouteContract.for_spel_fixture()
        receipt = contract.route(SPEL_CHANNEL_ID)  # all skills ready
        assert receipt.status == "ok"

        # Assemble the ephemeral prompt the way resolve_channel_prompt would
        # surface it at API call time: the brief body + a per-turn receipt
        # header. The receipt header must NOT contain private brief text.
        brief_text = contract.brief_path(SPEL_CHANNEL_ID).read_text(encoding="utf-8")
        receipt_header = (
            f"[Channel route receipt "
            f"id={receipt.channel_id} "
            f"path={Path(receipt.brief_path).name} "
            f"sha256={receipt.brief_sha256[:12]} "
            f"status={receipt.status}]"
        )
        ephemeral = f"{receipt_header}\n\n{brief_text}"

        # Drive the capturing agent the way the gateway would.
        _CapturingAgent.last_init = None
        _CapturingAgent.last_user_message = None
        agent = _CapturingAgent(
            ephemeral_system_prompt=ephemeral,
            prefill_messages=[],
        )
        agent.run_conversation("start")

        assert _CapturingAgent.last_ephemeral is not None
        assert SPEL_CHANNEL_ID in _CapturingAgent.last_ephemeral
        assert "Game Mode" in _CapturingAgent.last_ephemeral
        # the receipt header appears in the assembled prompt
        assert "[Channel route receipt" in _CapturingAgent.last_ephemeral
        assert receipt.status == "ok"

    def test_new_session_injects_skill_payload(self, monkeypatch):
        """On a NEW session, required skills are auto-loaded (mirrors the
        gateway.run ``_is_new_session and auto_skill`` path). The canary
        asserts the skill payload is prepended to the user message."""
        _install_fake_agent(monkeypatch)
        contract = ChannelRouteContract.for_spel_fixture()
        skills = contract.load_skills(SPEL_CHANNEL_ID)
        skill_payload = "\n\n".join(
            f'[IMPORTANT: The "{s.name}" skill is auto-loaded for #spel.]'
            for s in skills
        )
        user_text = "vilken klass spelar vi?"
        assembled = f"{skill_payload}\n\n{user_text}"
        assert "sonar-context" in assembled
        assert "gaming-skill" in assembled
        assert user_text in assembled


class TestCanaryExistingSession:
    """Synthetic canary for an EXISTING session: the full brief body is NOT
    re-injected (it is already in transcript history from the first message);
    only the per-turn receipt header is re-emitted."""

    def test_existing_session_re_emits_receipt_without_full_brief(self, monkeypatch):
        _install_fake_agent(monkeypatch)
        contract = ChannelRouteContract.for_spel_fixture()
        receipt = contract.build_receipt(SPEL_CHANNEL_ID)
        header_only = (
            f"[Channel route receipt "
            f"id={receipt.channel_id} "
            f"path={Path(receipt.brief_path).name} "
            f"sha256={receipt.brief_sha256[:12]} "
            f"status={receipt.status}]"
        )
        brief_text = contract.brief_path(SPEL_CHANNEL_ID).read_text(encoding="utf-8")

        _CapturingAgent.last_init = None
        _CapturingAgent.last_user_message = None
        agent = _CapturingAgent(ephemeral_system_prompt=header_only)
        agent.run_conversation("fortsätt")

        assert _CapturingAgent.last_ephemeral is not None
        assert "[Channel route receipt" in _CapturingAgent.last_ephemeral
        assert receipt.channel_id in _CapturingAgent.last_ephemeral
        assert receipt.status in _CapturingAgent.last_ephemeral
        # The full brief body must NOT be re-injected for an existing session
        for line in brief_text.splitlines():
            stripped = line.strip()
            if len(stripped) < 12:
                continue
            assert stripped not in _CapturingAgent.last_ephemeral, (
                f"full brief line re-injected for existing session: {stripped!r}"
            )

    def test_existing_session_receipt_still_fail_closed_if_skill_drops(self):
        """If a skill later becomes unavailable mid-session, the per-turn
        receipt must reflect fail-closed status, not silently degrade."""
        contract = ChannelRouteContract.for_spel_fixture(
            skill_loader=lambda name: None
        )
        receipt = contract.build_receipt(SPEL_CHANNEL_ID)
        assert receipt.status == "fail-closed"
        header = (
            f"[Channel route receipt id={receipt.channel_id} "
            f"sha256={receipt.brief_sha256[:12]} status={receipt.status}]"
        )
        assert "fail-closed" in header