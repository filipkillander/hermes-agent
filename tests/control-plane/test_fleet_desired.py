"""Candidate tests for the kmros fleet-desired contract (Phase A, read-only).

These tests verify the contract can be read, validates against its JSON Schema,
that ``retired`` is a first-class scheduled status, that stale data is not green,
and that no secret VALUES leak into the contract (only names/identifiers).

Run (from worktree root):
    python -m pytest tests/control-plane/test_fleet_desired.py -o 'addopts=' -q
"""

from __future__ import annotations

import copy
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

pytest.importorskip("jsonschema")
from jsonschema import Draft202012Validator  # noqa: E402

# ── Paths ────────────────────────────────────────────────────────────────────
WORKTREE = Path(__file__).resolve().parents[2]
CONTRACT = WORKTREE / "control-plane" / "fleet-desired.yaml"
SCHEMA = WORKTREE / "control-plane" / "fleet-desired.schema.json"


# ── Fixtures ──────────────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def schema():
    return json.loads(SCHEMA.read_text())


@pytest.fixture(scope="module")
def validator(schema):
    return Draft202012Validator(schema)


@pytest.fixture(scope="module")
def contract():
    return yaml.safe_load(CONTRACT.read_text())


# Heuristic patterns that indicate a leaked secret VALUE rather than a name.
# We scan scalar strings for these and fail if any match. Identifiers/names in
# the contract are short slugs (DISCORD_BOT_TOKEN, discord-bot-token) that do
# NOT match these patterns.
_SECRET_VALUE_PATTERNS = [
    # Long high-entropy run of PURE ALPHANUMERICS (>= 32 chars, no separators).
    # Real tokens (hex PATs, base62 keys) and the injected guard (x*40) match;
    # readable slugs/paths/dated-IDs (kmros-decision-..., knowledge/.../...) do
    # not, because their separators break the run into <32-char segments.
    re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9]{32,}(?![A-Za-z0-9])"),
    # Discord/Telegram/Slack bearer prefixes.
    re.compile(r"(?i)(bot\s+)?token\s*[:=]\s*\S+"),
    re.compile(r"(?i)(discord|telegram|slack|feishu)\s+token\s*[:=]"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._-]+"),
    # AWS / generic secret key shapes.
    re.compile(r"(?i)AKIA[0-9A-Z]{16}"),
    re.compile(r"(?i)(secret|password|api[_-]?key)\s*[:=]\s*\S+"),
]


def _walk_scalars(node, path="root"):
    """Yield (path, scalar) for every string/int scalar in a nested structure."""
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _walk_scalars(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk_scalars(v, f"{path}[{i}]")
    elif isinstance(node, str):
        yield path, node
    # ints/bools/None are not secret-shaped; skip.


# ── Tests ────────────────────────────────────────────────────────────────────
class TestContractLoads:
    def test_contract_file_exists(self):
        assert CONTRACT.is_file(), f"missing contract at {CONTRACT}"

    def test_schema_file_exists(self):
        assert SCHEMA.is_file(), f"missing schema at {SCHEMA}"

    def test_contract_parses_as_yaml(self, contract):
        assert isinstance(contract, dict)
        assert contract.get("schema", {}).get("kind") == "fleet-desired"

    def test_schema_parses_as_json(self, schema):
        assert isinstance(schema, dict)
        assert schema.get("title", "").startswith("kmros fleet-desired")


class TestValidContractValidates:
    def test_shipped_contract_validates_against_schema(self, validator, contract):
        errors = list(validator.iter_errors(contract))
        assert not errors, _format_errors(errors)

    def test_contract_has_all_required_top_level_keys(self, contract):
        required = [
            "schema", "profiles", "source", "rollout", "channels",
            "scheduled", "recovery", "owner_controlled", "updater",
            "retention", "secrets", "freshness",
        ]
        missing = [k for k in required if k not in contract]
        assert not missing, f"missing top-level keys: {missing}"

    def test_all_three_profiles_present(self, contract):
        assert set(contract["profiles"]) >= {"lumi", "igor", "spark"}

    def test_example_channel_present(self, contract):
        assert "1509925693247459378" in contract["channels"]
        ch = contract["channels"]["1509925693247459378"]
        assert ch["canonical_brief"]
        assert ch["required_skills"]
        assert ch["knowledge_root"]


class TestInvalidContractRejected:
    def test_missing_required_key_fails(self, validator, contract):
        bad = copy.deepcopy(contract)
        del bad["schema"]["owner_decision_id"]
        errors = list(validator.iter_errors(bad))
        assert errors, "expected validation failure for missing owner_decision_id"

    def test_wrong_type_fails(self, validator, contract):
        bad = copy.deepcopy(contract)
        bad["profiles"]["lumi"]["port"] = "not-a-port"
        errors = list(validator.iter_errors(bad))
        assert errors, "expected validation failure for string port"

    def test_additional_property_fails(self, validator, contract):
        bad = copy.deepcopy(contract)
        bad["rogue_top_level_key"] = "should not be allowed"
        errors = list(validator.iter_errors(bad))
        assert errors, "expected validation failure for additionalProperties"

    def test_bad_kind_const_fails(self, validator, contract):
        bad = copy.deepcopy(contract)
        bad["schema"]["kind"] = "not-fleet-desired"
        errors = list(validator.iter_errors(bad))
        assert errors, "expected validation failure for wrong kind"

    def test_port_out_of_range_fails(self, validator, contract):
        bad = copy.deepcopy(contract)
        bad["profiles"]["igor"]["port"] = 99999
        errors = list(validator.iter_errors(bad))
        assert errors, "expected validation failure for port > 65535"


class TestRetiredStatus:
    def test_retired_is_valid_status(self, validator, contract):
        """A scheduled entry with status=retired must validate."""
        entry = contract["scheduled"]["launchagents"]["nightly-restart"]
        assert entry["status"] == "retired"

    def test_retired_entry_validates_against_schema(self, validator, contract):
        # Isolate just the scheduled sub-tree shape by building a minimal valid
        # contract around a retired entry.
        minimal = copy.deepcopy(contract)
        minimal["scheduled"]["cron"]["retired-job"] = {
            "status": "retired",
            "reason": "intentionally retired",
        }
        errors = list(validator.iter_errors(minimal))
        assert not errors, _format_errors(errors)

    def test_unknown_status_fails(self, validator, contract):
        bad = copy.deepcopy(contract)
        bad["scheduled"]["launchagents"]["nightly-restart"]["status"] = "zombie"
        errors = list(validator.iter_errors(bad))
        assert errors, "expected validation failure for unknown status"


class TestStaleDataNotGreen:
    """A stale contract (old timestamp) must NOT be classified as OK."""

    def _freshness_ok(self, contract_obj) -> bool:
        last = datetime.fromisoformat(
            contract_obj["freshness"]["last_affirmed_at"].replace("Z", "+00:00")
        )
        max_age = timedelta(hours=contract_obj["freshness"]["max_age_hours"])
        age = datetime.now(timezone.utc) - last
        return age <= max_age

    def test_fresh_contract_is_ok(self, contract):
        # The shipped contract's last_affirmed_at is recent relative to max_age.
        assert self._freshness_ok(contract) is True

    def test_stale_contract_is_not_ok(self, contract):
        stale = copy.deepcopy(contract)
        stale["freshness"]["last_affirmed_at"] = (
            datetime.now(timezone.utc) - timedelta(days=30)
        ).isoformat()
        assert self._freshness_ok(stale) is False

    def test_stale_contract_still_validates_schema(self, validator, contract):
        """Schema validation is about SHAPE, not freshness. A stale contract
        still passes schema validation; the STALENESS check is a separate gate
        that must be applied on top. This test documents that boundary so no
        one mistakes 'validates' for 'fresh'."""
        stale = copy.deepcopy(contract)
        stale["freshness"]["last_affirmed_at"] = (
            datetime.now(timezone.utc) - timedelta(days=30)
        ).isoformat()
        errors = list(validator.iter_errors(stale))
        assert not errors, _format_errors(errors)
        # And it is NOT fresh:
        assert not self._freshness_ok(stale)


class TestNoSecretValues:
    """Scan the contract for token-like secret VALUES. Only names/identifiers
    are allowed; a leaked value must fail the contract."""

    def test_no_secret_values_in_contract(self, contract):
        offenders = []
        for path, scalar in _walk_scalars(contract):
            for pat in _SECRET_VALUE_PATTERNS:
                m = pat.search(scalar)
                if m:
                    offenders.append((path, m.group(0)[:40]))
        assert not offenders, (
            f"potential secret VALUES found in contract (only names allowed): {offenders}"
        )

    def test_secrets_section_has_names_only(self, contract):
        for entry in contract.get("secrets", []):
            assert "name" in entry and "identifier" in entry
            # identifiers are short UPPER_SNAKE slugs, not secret blobs:
            assert re.fullmatch(r"[A-Z][A-Z0-9_]*", entry["identifier"]), (
                f"identifier looks like a value, not a name: {entry['identifier']}"
            )

    def test_known_secret_name_present(self, contract):
        names = {e["name"] for e in contract["secrets"]}
        assert "discord-bot-token" in names

    def test_secret_value_would_be_caught(self, contract):
        """Guard: if someone adds a real token VALUE, this test catches it.
        We inject a fake one and confirm the scanner flags it."""
        poisoned = copy.deepcopy(contract)
        poisoned["secrets"].append({
            "name": "leaked",
            "identifier": "x" * 40,  # 40-char blob -> matches the blob pattern
        })
        offenders = []
        for path, scalar in _walk_scalars(poisoned):
            for pat in _SECRET_VALUE_PATTERNS:
                if pat.search(scalar):
                    offenders.append(path)
        assert offenders, "scanner failed to catch an injected secret-shaped value"


# ── Helpers ──────────────────────────────────────────────────────────────────
def _format_errors(errors) -> str:
    import json
    lines = ["validation errors:"]
    for e in errors[:10]:
        loc = "/".join(str(p) for p in e.absolute_path) or "<root>"
        lines.append(f"  at {loc}: {e.message[:200]}")
    if len(errors) > 10:
        lines.append(f"  ... and {len(errors) - 10} more")
    return "\n".join(lines)


# need json imported for fixtures (kept here to avoid top-level noise in a test module)
import json  # noqa: E402