from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from gateway.status import evaluate_runtime_readiness
from hermes_cli.config import (
    ConfigTransactionError,
    atomic_config_write,
    read_config_snapshot,
)
from hermes_cli.restart_coordinator import RestartCoordinator, RestartRejected
from hermes_cli.runtime_registry import (
    RegistryError,
    credential_fingerprint,
    load_runtime_registry,
)


def _registry_file(tmp_path: Path, *, dispatcher: bool = True) -> Path:
    home = tmp_path / "profiles" / "lumi"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text("model: test/model\n", encoding="utf-8")
    registry = tmp_path / "runtime-registry.yaml"
    registry.write_text(
        f"""schema_version: 1
profiles:
  lumi:
    role: external_gateway
    home: {home}
    service_label: ai.hermes.gateway-lumi
    port: 8642
    dispatcher: {str(dispatcher).lower()}
    allowed_platforms: [telegram]
    required_platforms: [telegram]
  coder:
    role: internal_worker
    home: {tmp_path / 'profiles' / 'coder'}
""",
        encoding="utf-8",
    )
    return registry


def test_malformed_config_cannot_be_replaced(tmp_path):
    path = tmp_path / "config.yaml"
    original = b"model: [unterminated\nplatforms:\n  telegram: {}\n"
    path.write_bytes(original)
    with pytest.raises(ConfigTransactionError, match="not valid YAML"):
        atomic_config_write(path, {"model": "replacement"})
    assert path.read_bytes() == original


def test_config_compare_and_swap_rejects_stale_writer(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("model: first\n", encoding="utf-8")
    snapshot = read_config_snapshot(path)
    path.write_text("model: second\nplatforms: {}\n", encoding="utf-8")
    with pytest.raises(ConfigTransactionError, match="changed since it was read"):
        atomic_config_write(path, {"model": "stale"}, expected_base_hash=snapshot.content_hash)
    assert "second" in path.read_text(encoding="utf-8")


def test_config_candidate_schema_fails_before_publish(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("model: valid\n", encoding="utf-8")
    with pytest.raises(ConfigTransactionError, match="schema validation failed"):
        atomic_config_write(path, {"custom_providers": {"name": "wrong-shape"}})
    assert path.read_text(encoding="utf-8") == "model: valid\n"


def test_tui_parse_failure_cannot_collapse_config(tmp_path, monkeypatch):
    from tui_gateway import server

    path = tmp_path / "config.yaml"
    original = "model: [broken\nplatforms:\n  discord: {}\n"
    path.write_text(original, encoding="utf-8")
    monkeypatch.setattr(server, "_hermes_home", str(tmp_path))
    monkeypatch.setattr(server, "_cfg_cache", None)
    monkeypatch.setattr(server, "_cfg_mtime", None)
    monkeypatch.setattr(server, "_cfg_path", None)
    monkeypatch.setattr(server, "_cfg_base_hash", None)
    monkeypatch.setattr(server, "_cfg_load_error", None)
    assert server._load_cfg() == {}
    with pytest.raises(RuntimeError, match="last read failed"):
        server._save_cfg({"display": {"skin": "compact"}})
    assert path.read_text(encoding="utf-8") == original


def test_registry_validates_unique_ownership_and_secure_worker_defaults(tmp_path):
    registry = load_runtime_registry(_registry_file(tmp_path))
    assert registry.require("lumi").dispatcher is True
    assert registry.require("coder").dispatcher is False
    assert registry.require("coder").allowed_platforms == ()

    duplicate = _registry_file(tmp_path)
    text = duplicate.read_text(encoding="utf-8")
    text += f"""  spark:
    role: external_gateway
    home: {tmp_path / 'profiles' / 'spark'}
    service_label: ai.hermes.gateway-spark
    port: 8642
"""
    duplicate.write_text(text, encoding="utf-8")
    with pytest.raises(RegistryError, match="Port 8642"):
        load_runtime_registry(duplicate)


def test_readiness_rejects_wrong_profile_even_when_running():
    payload = {
        "gateway_state": "running",
        "runtime_identity": {
            "profile": "ops",
            "role": "external_gateway",
            "service_label": "ai.hermes.gateway-ops",
            "port": 8642,
            "registry_revision": "r1",
            "registry_verified": True,
            "secret_readiness": {"state": "ready", "ready": True},
            "required_platforms": [],
        },
        "platforms": {},
    }
    ready, failures = evaluate_runtime_readiness(
        payload,
        expected_profile="lumi",
        expected_service_label="ai.hermes.gateway-lumi",
        expected_port=8642,
        expected_registry_revision="r1",
    )
    assert ready is False
    assert {"profile_mismatch", "service_label_mismatch"}.issubset(failures)


class _Driver:
    def __init__(self):
        self.current_pid = 101
        self.restart_calls = 0

    def pid(self, label):
        return self.current_pid

    def restart(self, label):
        self.restart_calls += 1
        self.current_pid += 1


def _health_payload(profile, registry, pid, config_revision, active_agents=0):
    return {
        "status": "ok",
        "gateway_state": "running",
        "pid": pid,
        "active_agents": active_agents,
        "platforms": {"telegram": {"state": "connected"}},
        "runtime_identity": {
            "profile": profile.name,
            "role": profile.role,
            "home": str(profile.home),
            "service_label": profile.service_label,
            "port": profile.port,
            "registry_revision": registry.revision,
            "registry_verified": True,
            "secret_readiness": {"state": "ready", "ready": True},
            "required_platforms": list(profile.required_platforms),
            "allowed_platforms": list(profile.allowed_platforms),
            "config_revision": config_revision,
            "code_revision": profile.release_revision,
        },
    }


def test_readiness_fails_closed_when_external_secrets_are_not_ready():
    payload = {
        "gateway_state": "running",
        "runtime_identity": {
            "profile": "lumi",
            "role": "external_gateway",
            "service_label": "ai.hermes.gateway-lumi",
            "port": 8642,
            "registry_revision": "r1",
            "registry_verified": True,
            "secret_readiness": {
                "state": "degraded",
                "ready": False,
                "failed_sources": 1,
            },
            "required_platforms": [],
            "allowed_platforms": [],
        },
        "platforms": {},
    }
    ready, failures = evaluate_runtime_readiness(payload)
    assert ready is False
    assert "secret_sources_not_ready" in failures


def test_keyed_bot_fingerprint_and_readiness_mismatch(tmp_path):
    key_path = tmp_path / "fingerprint.key"
    key_path.write_bytes(b"k" * 32)
    key_path.chmod(0o600)
    expected = credential_fingerprint("bot-token-a", key_path=key_path)
    other = credential_fingerprint("bot-token-b", key_path=key_path)
    assert expected and expected.startswith("hmac-sha256:")
    assert expected != other
    assert "bot-token" not in expected

    payload = {
        "gateway_state": "running",
        "runtime_identity": {
            "profile": "lumi",
            "role": "external_gateway",
            "registry_verified": True,
            "secret_readiness": {"state": "ready", "ready": True},
            "required_platforms": ["telegram"],
            "allowed_platforms": ["telegram"],
            "bot_fingerprints": {"telegram": expected},
        },
        "platforms": {
            "telegram": {
                "state": "connected",
                "credential_fingerprint": other,
            }
        },
    }
    ready, failures = evaluate_runtime_readiness(payload)
    assert ready is False
    assert "bot_fingerprint_mismatch:telegram" in failures

    payload["platforms"]["telegram"]["credential_fingerprint"] = expected
    ready, failures = evaluate_runtime_readiness(payload)
    assert ready is True
    assert failures == []


def test_restart_coordinator_proves_identity_and_stability(tmp_path):
    registry = load_runtime_registry(_registry_file(tmp_path))
    profile = registry.require("lumi")
    revision = hashlib.sha256((profile.home / "config.yaml").read_bytes()).hexdigest()
    driver = _Driver()

    coordinator = RestartCoordinator(
        registry,
        driver=driver,
        state_dir=tmp_path / "state",
        health_probe=lambda url, timeout: _health_payload(profile, registry, driver.current_pid, revision),
        port_probe=lambda port: {driver.current_pid},
        process_probe=lambda pid, profile: True,
        stable_probes=3,
        sleeper=lambda seconds: None,
    )
    result = coordinator.restart("lumi")
    assert result.old_pid == 101
    assert result.new_pid == 102
    assert result.stable_probes == 3
    assert driver.restart_calls == 1


def test_restart_coordinator_never_kills_unknown_port_owner(tmp_path):
    registry = load_runtime_registry(_registry_file(tmp_path))
    driver = _Driver()
    coordinator = RestartCoordinator(
        registry,
        driver=driver,
        state_dir=tmp_path / "state",
        health_probe=lambda url, timeout: None,
        port_probe=lambda port: {99999},
        process_probe=lambda pid, profile: True,
    )
    with pytest.raises(RestartRejected, match="unknown PID"):
        coordinator.restart("lumi")
    assert driver.restart_calls == 0


def test_restart_coordinator_rejects_drifted_service_command(tmp_path):
    registry = load_runtime_registry(_registry_file(tmp_path))
    driver = _Driver()
    coordinator = RestartCoordinator(
        registry,
        driver=driver,
        state_dir=tmp_path / "state",
        health_probe=lambda url, timeout: None,
        port_probe=lambda port: set(),
        process_probe=lambda pid, profile: False,
    )
    with pytest.raises(RestartRejected, match="not the registered profile command"):
        coordinator.restart("lumi")
    assert driver.restart_calls == 0


def test_restart_coordinator_defers_active_gateway(tmp_path):
    registry = load_runtime_registry(_registry_file(tmp_path))
    profile = registry.require("lumi")
    revision = hashlib.sha256((profile.home / "config.yaml").read_bytes()).hexdigest()
    driver = _Driver()
    coordinator = RestartCoordinator(
        registry,
        driver=driver,
        state_dir=tmp_path / "state",
        health_probe=lambda url, timeout: _health_payload(profile, registry, driver.current_pid, revision, 1),
        port_probe=lambda port: {driver.current_pid},
        process_probe=lambda pid, profile: True,
    )
    with pytest.raises(RestartRejected, match="active agents"):
        coordinator.restart("lumi")
    assert driver.restart_calls == 0


def test_restart_coordinator_opens_circuit_after_bounded_attempts(tmp_path):
    registry = load_runtime_registry(_registry_file(tmp_path))
    profile = registry.require("lumi")
    revision = hashlib.sha256((profile.home / "config.yaml").read_bytes()).hexdigest()
    driver = _Driver()
    coordinator = RestartCoordinator(
        registry,
        driver=driver,
        state_dir=tmp_path / "state",
        health_probe=lambda url, timeout: _health_payload(profile, registry, driver.current_pid, revision),
        port_probe=lambda port: {driver.current_pid},
        process_probe=lambda pid, profile: True,
        stable_probes=1,
        sleeper=lambda seconds: None,
        clock=lambda: 1000.0,
        max_attempts=2,
    )
    coordinator.restart("lumi")
    coordinator.restart("lumi")
    with pytest.raises(RestartRejected, match="Circuit open"):
        coordinator.restart("lumi")
    assert driver.restart_calls == 2


def test_restart_coordinator_uses_bounded_failure_backoff(tmp_path):
    registry = load_runtime_registry(_registry_file(tmp_path))
    profile = registry.require("lumi")
    revision = hashlib.sha256((profile.home / "config.yaml").read_bytes()).hexdigest()
    driver = _Driver()
    calls = {"health": 0}
    sleeps = []

    def health(url, timeout):
        calls["health"] += 1
        # Preflight succeeds; first two postflight probes fail; third succeeds.
        if calls["health"] in {2, 3}:
            return None
        return _health_payload(profile, registry, driver.current_pid, revision)

    coordinator = RestartCoordinator(
        registry,
        driver=driver,
        state_dir=tmp_path / "state",
        health_probe=health,
        port_probe=lambda port: {driver.current_pid},
        process_probe=lambda pid, profile: True,
        stable_probes=1,
        sleeper=sleeps.append,
        probe_interval=2.0,
        max_probe_interval=3.0,
    )
    coordinator.restart("lumi")
    assert sleeps == [2.0, 3.0]
