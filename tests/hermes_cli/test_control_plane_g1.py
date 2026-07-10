from __future__ import annotations

import hashlib
import subprocess
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
    board_creation_authorized,
    credential_fingerprint,
    delegation_authorized,
    load_runtime_registry,
)


def _registry_file(
    tmp_path: Path,
    *,
    dispatcher: bool = True,
    release_revision: str | None = None,
) -> Path:
    home = tmp_path / "profiles" / "lumi"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text("model: test/model\n", encoding="utf-8")
    registry = tmp_path / "runtime-registry.yaml"
    release_line = f"    release_revision: {release_revision}\n" if release_revision else ""
    registry.write_text(
        f"""schema_version: 2
profiles:
  lumi:
    role: external_gateway
    home: {home}
    service_label: ai.hermes.gateway-lumi
    port: 8642
    dispatcher: {str(dispatcher).lower()}
    allowed_platforms: [telegram]
    required_platforms: [telegram]
    domain: general
    can_delegate_to: [workers]
    can_create_boards: true
{release_line}  coder:
    role: internal_worker
    home: {tmp_path / 'profiles' / 'coder'}
    domain: worker
    can_delegate_to: []
    can_create_boards: false
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


def test_atomic_config_default_sentinel_survives_module_reload(tmp_path):
    """An imported writer must not mistake its old default for an expected hash."""
    import subprocess
    import sys
    import textwrap

    path = tmp_path / "config.yaml"
    path.write_text("model: before\n", encoding="utf-8")

    code = textwrap.dedent(
        f"""
        import importlib
        from pathlib import Path
        import hermes_cli.config as config_module
        old_writer = config_module.atomic_config_write
        importlib.reload(config_module)
        old_writer(Path({str(path)!r}), {{"model": "after"}})
        """
    )
    subprocess.run([sys.executable, "-c", code], check=True)

    assert path.read_text(encoding="utf-8") == "model: after\n"


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
    assert registry.require("lumi").can_create_boards is True
    assert registry.require("lumi").can_delegate_to == ("workers",)
    assert registry.require("coder").domain == "worker"

    duplicate = _registry_file(tmp_path)
    text = duplicate.read_text(encoding="utf-8")
    text += f"""  spark:
    role: external_gateway
    home: {tmp_path / 'profiles' / 'spark'}
    service_label: ai.hermes.gateway-spark
    port: 8642
    domain: smart_home
    can_delegate_to: []
    can_create_boards: false
"""
    duplicate.write_text(text, encoding="utf-8")
    with pytest.raises(RegistryError, match="Port 8642"):
        load_runtime_registry(duplicate)


def test_registry_v2_authority_matrix_is_fail_closed(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    profiles = root / "profiles"
    for name in ("lumi", "igor", "spark", "coder", "review"):
        (profiles / name).mkdir(parents=True, exist_ok=True)
    registry_path = root / "runtime-registry.yaml"
    registry_path.write_text(
        f"""schema_version: 2
profiles:
  lumi:
    role: external_gateway
    home: {profiles / 'lumi'}
    service_label: ai.hermes.gateway-lumi
    port: 8642
    domain: general
    can_delegate_to: [igor, spark, workers]
    can_create_boards: true
  igor:
    role: external_gateway
    home: {profiles / 'igor'}
    service_label: ai.hermes.gateway-igor
    port: 8644
    domain: general
    can_delegate_to: [spark, workers]
    can_create_boards: false
  spark:
    role: external_gateway
    home: {profiles / 'spark'}
    service_label: ai.hermes.gateway-spark
    port: 8643
    domain: smart_home
    can_delegate_to: []
    can_create_boards: false
  coder:
    role: internal_worker
    home: {profiles / 'coder'}
    domain: worker
    can_delegate_to: []
    can_create_boards: false
  review:
    role: internal_worker
    home: {profiles / 'review'}
    domain: worker
    can_delegate_to: []
    can_create_boards: false
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_RUNTIME_REGISTRY", str(registry_path))

    assert board_creation_authorized(profiles / "lumi") is True
    assert board_creation_authorized(profiles / "igor") is False
    assert board_creation_authorized(profiles / "spark") is False
    assert delegation_authorized(profiles / "lumi", "igor") is True
    assert delegation_authorized(profiles / "lumi", "spark") is True
    assert delegation_authorized(profiles / "lumi", "coder") is True
    assert delegation_authorized(profiles / "igor", "spark") is True
    assert delegation_authorized(profiles / "igor", "review") is True
    assert delegation_authorized(profiles / "igor", "lumi") is False
    assert delegation_authorized(profiles / "spark", "workers") is False
    assert delegation_authorized(profiles / "spark", "coder") is False
    assert delegation_authorized(profiles / "lumi", "missing") is False


def test_registry_v2_rejects_unknown_delegation_target(tmp_path):
    path = _registry_file(tmp_path)
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "can_delegate_to: [workers]", "can_delegate_to: [ghost]"
        ),
        encoding="utf-8",
    )
    with pytest.raises(RegistryError, match="unknown profile 'ghost'"):
        load_runtime_registry(path)


def test_registry_identity_revision_excludes_only_release_intent(tmp_path):
    first = _registry_file(tmp_path, release_revision="release-one")
    revision_one = load_runtime_registry(first).revision

    text = first.read_text(encoding="utf-8").replace(
        "release_revision: release-one",
        "release_revision: release-two",
    )
    first.write_text(text, encoding="utf-8")
    revision_two = load_runtime_registry(first).revision
    assert revision_two == revision_one

    first.write_text(
        text.replace("ai.hermes.gateway-lumi", "ai.hermes.gateway-lumi-v2"),
        encoding="utf-8",
    )
    assert load_runtime_registry(first).revision != revision_one


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


def _health_payload(
    profile,
    registry,
    pid,
    config_revision,
    active_agents=0,
    *,
    code_revision=None,
):
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
            "code_revision": (
                profile.release_revision if code_revision is None else code_revision
            ),
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


def test_macos_listener_probe_uses_fixed_lsof_and_parses_all_pids(monkeypatch):
    from hermes_cli import restart_coordinator as module

    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "101\n202\n101\n", "")

    monkeypatch.setattr(module.sys, "platform", "darwin")
    monkeypatch.setattr(module.subprocess, "run", fake_run)

    assert module.listening_pids(8643) == {101, 202}
    argv, kwargs = calls[0]
    assert argv == [
        "/usr/sbin/lsof",
        "-nP",
        "-a",
        "-iTCP:8643",
        "-sTCP:LISTEN",
        "-t",
    ]
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["timeout"] == 10


def test_macos_listener_probe_fails_closed_on_invalid_output(monkeypatch):
    from hermes_cli import restart_coordinator as module

    monkeypatch.setattr(module.sys, "platform", "darwin")
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, "not-a-pid\n", ""
        ),
    )

    with pytest.raises(RestartRejected, match="ownership output is invalid"):
        module.listening_pids(8643)


def test_macos_listener_probe_treats_lsof_no_match_as_empty(monkeypatch):
    from hermes_cli import restart_coordinator as module

    monkeypatch.setattr(module.sys, "platform", "darwin")
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, "", ""),
    )

    assert module.listening_pids(8643) == set()


def test_restart_coordinator_allows_only_preflight_release_transition(tmp_path):
    registry = load_runtime_registry(
        _registry_file(tmp_path, release_revision="release-new")
    )
    profile = registry.require("lumi")
    revision = hashlib.sha256((profile.home / "config.yaml").read_bytes()).hexdigest()
    driver = _Driver()

    def health(url, timeout):
        code_revision = "release-old" if driver.restart_calls == 0 else "release-new"
        return _health_payload(
            profile,
            registry,
            driver.current_pid,
            revision,
            code_revision=code_revision,
        )

    coordinator = RestartCoordinator(
        registry,
        driver=driver,
        state_dir=tmp_path / "state",
        health_probe=health,
        port_probe=lambda port: {driver.current_pid},
        process_probe=lambda pid, profile: True,
        stable_probes=1,
        sleeper=lambda seconds: None,
    )
    result = coordinator.restart("lumi", allow_release_transition=True)
    assert result.old_pid == 101
    assert result.new_pid == 102
    assert driver.restart_calls == 1


def test_restart_coordinator_allows_identified_degraded_gateway_to_heal(tmp_path):
    registry = load_runtime_registry(_registry_file(tmp_path))
    profile = registry.require("lumi")
    revision = hashlib.sha256((profile.home / "config.yaml").read_bytes()).hexdigest()
    driver = _Driver()

    def health(url, timeout):
        payload = _health_payload(
            profile,
            registry,
            driver.current_pid,
            revision,
        )
        if driver.restart_calls == 0:
            payload["platforms"]["telegram"]["state"] = "disconnected"
        return payload

    coordinator = RestartCoordinator(
        registry,
        driver=driver,
        state_dir=tmp_path / "state",
        health_probe=health,
        port_probe=lambda port: {driver.current_pid},
        process_probe=lambda pid, profile: True,
        stable_probes=1,
        sleeper=lambda seconds: None,
    )

    result = coordinator.restart("lumi")
    assert result.new_pid == 102
    assert driver.restart_calls == 1


def test_restart_coordinator_rejects_wrong_identity_even_when_degraded(tmp_path):
    registry = load_runtime_registry(_registry_file(tmp_path))
    profile = registry.require("lumi")
    revision = hashlib.sha256((profile.home / "config.yaml").read_bytes()).hexdigest()
    driver = _Driver()
    payload = _health_payload(profile, registry, driver.current_pid, revision)
    payload["platforms"]["telegram"]["state"] = "disconnected"
    payload["runtime_identity"]["profile"] = "spark"

    coordinator = RestartCoordinator(
        registry,
        driver=driver,
        state_dir=tmp_path / "state",
        health_probe=lambda url, timeout: payload,
        port_probe=lambda port: {driver.current_pid},
        process_probe=lambda pid, profile: True,
    )

    with pytest.raises(RestartRejected, match="profile_mismatch"):
        coordinator.restart("lumi")
    assert driver.restart_calls == 0


def test_release_transition_is_opt_in(tmp_path):
    registry = load_runtime_registry(
        _registry_file(tmp_path, release_revision="release-new")
    )
    profile = registry.require("lumi")
    revision = hashlib.sha256((profile.home / "config.yaml").read_bytes()).hexdigest()
    driver = _Driver()
    coordinator = RestartCoordinator(
        registry,
        driver=driver,
        state_dir=tmp_path / "state",
        health_probe=lambda url, timeout: _health_payload(
            profile,
            registry,
            driver.current_pid,
            revision,
            code_revision="release-old",
        ),
        port_probe=lambda port: {driver.current_pid},
        process_probe=lambda pid, profile: True,
    )

    with pytest.raises(RestartRejected, match="code_revision_mismatch"):
        coordinator.restart("lumi")
    assert driver.restart_calls == 0


@pytest.mark.parametrize(
    ("field", "wrong_value", "failure"),
    [
        ("profile", "ops", "profile_mismatch"),
        ("registry_revision", "wrong-registry", "registry_revision_mismatch"),
        ("config_revision", "wrong-config", "config_revision_mismatch"),
    ],
)
def test_release_transition_keeps_other_preflight_identity_checks(
    tmp_path, field, wrong_value, failure
):
    registry = load_runtime_registry(
        _registry_file(tmp_path, release_revision="release-new")
    )
    profile = registry.require("lumi")
    revision = hashlib.sha256((profile.home / "config.yaml").read_bytes()).hexdigest()
    driver = _Driver()
    payload = _health_payload(
        profile,
        registry,
        driver.current_pid,
        revision,
        code_revision="release-old",
    )
    payload["runtime_identity"][field] = wrong_value
    coordinator = RestartCoordinator(
        registry,
        driver=driver,
        state_dir=tmp_path / "state",
        health_probe=lambda url, timeout: payload,
        port_probe=lambda port: {driver.current_pid},
        process_probe=lambda pid, profile: True,
    )

    with pytest.raises(RestartRejected, match=failure):
        coordinator.restart("lumi", allow_release_transition=True)
    assert driver.restart_calls == 0


def test_release_transition_postflight_enforces_desired_revision(tmp_path):
    registry = load_runtime_registry(
        _registry_file(tmp_path, release_revision="release-new")
    )
    profile = registry.require("lumi")
    revision = hashlib.sha256((profile.home / "config.yaml").read_bytes()).hexdigest()
    driver = _Driver()
    now = [0.0]

    def sleep(seconds):
        now[0] += seconds

    coordinator = RestartCoordinator(
        registry,
        driver=driver,
        state_dir=tmp_path / "state",
        health_probe=lambda url, timeout: _health_payload(
            profile,
            registry,
            driver.current_pid,
            revision,
            code_revision="release-old",
        ),
        port_probe=lambda port: {driver.current_pid},
        process_probe=lambda pid, profile: True,
        clock=lambda: now[0],
        sleeper=sleep,
        stable_probes=1,
        postflight_timeout=1,
    )

    with pytest.raises(RestartRejected, match="code_revision_mismatch"):
        coordinator.restart("lumi", allow_release_transition=True)
    assert driver.restart_calls == 1


def test_restart_coordinator_cli_forwards_release_transition_flag(monkeypatch, tmp_path):
    from hermes_cli import restart_coordinator as module

    calls = {}
    registry = object()

    class FakeCoordinator:
        def __init__(self, received_registry):
            assert received_registry is registry

        def restart(self, profile, *, allow_release_transition):
            calls.update(
                profile=profile,
                allow_release_transition=allow_release_transition,
            )
            return module.RestartResult(
                profile=profile,
                old_pid=1,
                new_pid=2,
                config_revision="config-revision",
                stable_probes=6,
            )

    monkeypatch.setattr(module, "load_runtime_registry", lambda *args, **kwargs: registry)
    monkeypatch.setattr(module, "RestartCoordinator", FakeCoordinator)

    assert module.main(["lumi", "--allow-release-transition"]) == 0
    assert calls == {
        "profile": "lumi",
        "allow_release_transition": True,
    }


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
