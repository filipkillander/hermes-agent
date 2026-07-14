"""Tests for the /api/status profile + gateway topology readout.

Covers the loopback-only ``profiles`` / ``gateway_mode`` / ``gateways`` fields
added to ``/api/status``: profile enumeration, single vs multiplex vs multiple
gateway detection, and per-platform port resolution.
"""

import json
from types import SimpleNamespace

import pytest

from hermes_cli import web_server
from hermes_cli.web_server import (
    _bounded_gateway_status_issues,
    _collect_profile_gateway_topology,
    _probe_registered_profile_health,
    _profile_platform_ports,
)


class _HealthResponse:
    def __init__(self, payload: bytes, *, status: int = 200):
        self._payload = payload
        self.status = status

    def read(self, limit: int = -1) -> bytes:
        return self._payload if limit < 0 else self._payload[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _runtime_health_payload(*, profile: str = "lumi", required=(), platforms=None):
    return {
        "status": "ok",
        "gateway_state": "running",
        "pid": 4242,
        "platforms": platforms or {},
        "runtime_identity": {
            "profile": profile,
            "role": "external_gateway",
            "service_label": "ai.hermes.gateway-lumi",
            "port": 8642,
            "registry_revision": "registry-revision",
            "registry_verified": True,
            "secret_readiness": {"ready": True},
            "required_platforms": list(required),
            "allowed_platforms": list(required),
            "bot_fingerprints": {},
        },
    }


class TestRegisteredProfileHealthProbe:
    def _patch_registry(self, monkeypatch, home, *, required=()):
        import hermes_cli.runtime_registry as registry_mod

        profile = SimpleNamespace(
            name="lumi",
            home=home,
            role="external_gateway",
            health_url="http://127.0.0.1:8642/health/detailed",
            service_label="ai.hermes.gateway-lumi",
            port=8642,
            required_platforms=tuple(required),
        )
        registry = SimpleNamespace(
            revision="registry-revision",
            get=lambda name: profile if name == "lumi" else None,
        )
        monkeypatch.setattr(
            registry_mod, "load_runtime_registry", lambda required=False: registry
        )

    def test_verified_profile_health_is_live(self, tmp_path, monkeypatch):
        self._patch_registry(monkeypatch, tmp_path)
        payload = json.dumps(_runtime_health_payload()).encode()
        monkeypatch.setattr(
            web_server.urllib.request,
            "urlopen",
            lambda request, timeout: _HealthResponse(payload),
        )

        alive, body, issues = _probe_registered_profile_health("lumi", tmp_path)

        assert alive is True
        assert body["pid"] == 4242
        assert issues == []

    def test_readiness_failure_is_reported_without_calling_process_stopped(
        self, tmp_path, monkeypatch
    ):
        self._patch_registry(monkeypatch, tmp_path, required=("discord",))
        payload = json.dumps(
            _runtime_health_payload(
                required=("discord",),
                platforms={"discord": {"state": "disconnected"}},
            )
        ).encode()
        monkeypatch.setattr(
            web_server.urllib.request,
            "urlopen",
            lambda request, timeout: _HealthResponse(payload),
        )

        alive, _body, issues = _probe_registered_profile_health("lumi", tmp_path)

        assert alive is True
        assert issues == ["required_platform_disconnected:discord"]

    def test_wrong_profile_identity_is_rejected(self, tmp_path, monkeypatch):
        self._patch_registry(monkeypatch, tmp_path)
        payload = json.dumps(_runtime_health_payload(profile="igor")).encode()
        monkeypatch.setattr(
            web_server.urllib.request,
            "urlopen",
            lambda request, timeout: _HealthResponse(payload),
        )

        alive, _body, issues = _probe_registered_profile_health("lumi", tmp_path)

        assert alive is False
        assert "profile_mismatch" in issues

    def test_oversized_health_response_is_rejected(self, tmp_path, monkeypatch):
        self._patch_registry(monkeypatch, tmp_path)
        payload = b"{" + b"x" * (web_server._PROFILE_GATEWAY_HEALTH_MAX_BYTES + 1)
        monkeypatch.setattr(
            web_server.urllib.request,
            "urlopen",
            lambda request, timeout: _HealthResponse(payload),
        )

        alive, body, issues = _probe_registered_profile_health("lumi", tmp_path)

        assert alive is False
        assert body is None
        assert issues == ["health_response_too_large"]

    def test_diagnostics_are_bounded_and_code_only(self):
        issues = _bounded_gateway_status_issues(
            [" profile mismatch ", "path /Users/name/secret", *[f"issue-{i}" for i in range(20)]]
        )

        assert len(issues) == web_server._GATEWAY_STATUS_ISSUE_LIMIT
        assert issues[0] == "profile_mismatch"
        assert issues[1] == "path_users_name_secret"


# ---------------------------------------------------------------------------
# _profile_platform_ports
# ---------------------------------------------------------------------------

class TestProfilePlatformPorts:
    def test_no_runtime_platforms_returns_empty(self, tmp_path):
        assert _profile_platform_ports(tmp_path, None) == {}
        assert _profile_platform_ports(tmp_path, {"platforms": {}}) == {}

    def test_non_port_binding_platform_ignored(self, tmp_path):
        runtime = {"platforms": {"telegram": {"state": "connected"}}}
        assert _profile_platform_ports(tmp_path, runtime) == {}

    def test_default_port_when_no_config(self, tmp_path):
        runtime = {"platforms": {"webhook": {"state": "connected"}}}
        assert _profile_platform_ports(tmp_path, runtime) == {"webhook": 8644}

    def test_port_from_config_yaml_top_level(self, tmp_path):
        (tmp_path / "config.yaml").write_text(
            "platforms:\n  webhook:\n    port: 9001\n", encoding="utf-8"
        )
        runtime = {"platforms": {"webhook": {"state": "connected"}}}
        assert _profile_platform_ports(tmp_path, runtime) == {"webhook": 9001}

    def test_port_from_gateway_platforms_block(self, tmp_path):
        (tmp_path / "config.yaml").write_text(
            "gateway:\n  platforms:\n    api_server:\n      port: 9500\n",
            encoding="utf-8",
        )
        runtime = {"platforms": {"api_server": {"state": "connected"}}}
        assert _profile_platform_ports(tmp_path, runtime) == {"api_server": 9500}

    def test_top_level_platforms_wins_over_gateway_block(self, tmp_path):
        (tmp_path / "config.yaml").write_text(
            "gateway:\n  platforms:\n    webhook:\n      port: 1111\n"
            "platforms:\n  webhook:\n    port: 2222\n",
            encoding="utf-8",
        )
        runtime = {"platforms": {"webhook": {"state": "connected"}}}
        assert _profile_platform_ports(tmp_path, runtime) == {"webhook": 2222}

    def test_port_in_extra_block(self, tmp_path):
        (tmp_path / "config.yaml").write_text(
            "platforms:\n  whatsapp_cloud:\n    extra:\n      webhook_port: 8095\n",
            encoding="utf-8",
        )
        runtime = {"platforms": {"whatsapp_cloud": {"state": "connected"}}}
        assert _profile_platform_ports(tmp_path, runtime) == {"whatsapp_cloud": 8095}

    def test_dead_platform_states_excluded(self, tmp_path):
        runtime = {
            "platforms": {
                "webhook": {"state": "fatal"},
                "api_server": {"state": "disconnected"},
                "msgraph_webhook": {"state": "connected"},
            }
        }
        assert _profile_platform_ports(tmp_path, runtime) == {"msgraph_webhook": 8646}

    def test_invalid_port_value_falls_back_to_default(self, tmp_path):
        (tmp_path / "config.yaml").write_text(
            "platforms:\n  webhook:\n    port: notaport\n", encoding="utf-8"
        )
        runtime = {"platforms": {"webhook": {"state": "connected"}}}
        assert _profile_platform_ports(tmp_path, runtime) == {"webhook": 8644}


# ---------------------------------------------------------------------------
# _collect_profile_gateway_topology
# ---------------------------------------------------------------------------

def _patch_topology(monkeypatch, homes, running, runtimes):
    """Patch the topology collector's collaborators.

    ``homes``: list of (name, Path); ``running``: set of profile names with a
    live gateway; ``runtimes``: {name: runtime dict}.
    """
    import hermes_cli.profiles as profiles_mod
    import gateway.status as status_mod

    monkeypatch.setattr(profiles_mod, "profiles_to_serve", lambda multiplex: homes)
    monkeypatch.setattr(
        profiles_mod, "_check_gateway_running",
        lambda home: next(n for n, h in homes if h == home) in running,
    )
    by_path = {home / "gateway_state.json": runtimes.get(name) for name, home in homes}
    monkeypatch.setattr(
        status_mod, "read_runtime_status", lambda path=None: by_path.get(path)
    )


class TestCollectProfileGatewayTopology:
    def test_no_gateways_running(self, tmp_path, monkeypatch):
        homes = [("default", tmp_path / "d"), ("coder", tmp_path / "c")]
        _patch_topology(monkeypatch, homes, running=set(), runtimes={})
        topo = _collect_profile_gateway_topology()
        assert topo["profiles"] == ["default", "coder"]
        assert topo["gateway_mode"] == "none"
        assert topo["gateways"] == []

    def test_single_gateway(self, tmp_path, monkeypatch):
        homes = [("default", tmp_path / "d"), ("coder", tmp_path / "c")]
        _patch_topology(
            monkeypatch, homes, running={"default"},
            runtimes={"default": {"platforms": {}}},
        )
        topo = _collect_profile_gateway_topology()
        assert topo["gateway_mode"] == "single"
        assert [g["profile"] for g in topo["gateways"]] == ["default"]

    def test_multiplex_gateway(self, tmp_path, monkeypatch):
        homes = [("default", tmp_path / "d"), ("coder", tmp_path / "c")]
        _patch_topology(
            monkeypatch, homes, running={"default"},
            runtimes={"default": {
                "platforms": {},
                "served_profiles": ["default", "coder"],
            }},
        )
        topo = _collect_profile_gateway_topology()
        assert topo["gateway_mode"] == "multiplex"
        assert topo["gateways"][0]["served_profiles"] == ["default", "coder"]

    def test_multiple_independent_gateways_with_ports(self, tmp_path, monkeypatch):
        d_home = tmp_path / "d"
        c_home = tmp_path / "c"
        d_home.mkdir()
        c_home.mkdir()
        (c_home / "config.yaml").write_text(
            "platforms:\n  webhook:\n    port: 9644\n", encoding="utf-8"
        )
        homes = [("default", d_home), ("coder", c_home)]
        _patch_topology(
            monkeypatch, homes, running={"default", "coder"},
            runtimes={
                "default": {"platforms": {"webhook": {"state": "connected"}}},
                "coder": {"platforms": {"webhook": {"state": "connected"}}},
            },
        )
        topo = _collect_profile_gateway_topology()
        assert topo["gateway_mode"] == "multiple"
        ports = {g["profile"]: g["ports"] for g in topo["gateways"]}
        assert ports == {"default": {"webhook": 8644}, "coder": {"webhook": 9644}}

    def test_enumeration_failure_degrades_gracefully(self, monkeypatch):
        import hermes_cli.profiles as profiles_mod

        def _boom(multiplex):
            raise RuntimeError("no profiles root")

        monkeypatch.setattr(profiles_mod, "profiles_to_serve", _boom)
        topo = _collect_profile_gateway_topology()
        assert topo == {"profiles": [], "gateway_mode": "unknown", "gateways": []}


# ---------------------------------------------------------------------------
# /api/status wiring
# ---------------------------------------------------------------------------

class TestStatusEndpointTopology:
    @pytest.fixture(autouse=True)
    def _setup_client(self, monkeypatch, _isolate_hermes_home):
        try:
            from starlette.testclient import TestClient
        except ImportError:
            pytest.skip("fastapi/starlette not installed")

        import hermes_state
        from hermes_constants import get_hermes_home
        from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

        monkeypatch.setattr(
            hermes_state, "DEFAULT_DB_PATH", get_hermes_home() / "state.db"
        )
        self.client = TestClient(app)
        self.client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN

    def test_status_includes_full_topology_on_loopback(self, monkeypatch):
        monkeypatch.setattr(
            web_server, "_collect_profile_gateway_topology",
            lambda: {
                "profiles": ["default", "coder"],
                "gateway_mode": "single",
                "gateways": [{"profile": "default", "ports": {}}],
            },
        )
        resp = self.client.get("/api/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["profiles"] == ["default", "coder"]
        assert data["gateway_mode"] == "single"
        # The per-gateway detail (host ports) is loopback-only recon.
        assert data["gateways"] == [{"profile": "default", "ports": {}}]

    def test_profile_names_and_mode_public_when_auth_gated(self, monkeypatch):
        # Profile NAMES + gateway_mode are low-sensitivity product surface: the
        # Hermes Cloud Portal reads /api/status over the network (a gated bind)
        # to render the profile list, so they must survive the auth gate.
        monkeypatch.setattr(
            web_server, "_collect_profile_gateway_topology",
            lambda: {
                "profiles": ["default", "coder"],
                "gateway_mode": "multiplex",
                "gateways": [{"profile": "default", "ports": {"webhook": 8644}}],
            },
        )
        monkeypatch.setattr(web_server.app.state, "auth_required", True, raising=False)
        try:
            resp = self.client.get("/api/status")
            assert resp.status_code == 200
            data = resp.json()
            assert data["profiles"] == ["default", "coder"]
            assert data["gateway_mode"] == "multiplex"
            # But the per-gateway detail (host ports = recon) stays gated,
            # alongside hermes_home / gateway_pid.
            assert "gateways" not in data
            assert "hermes_home" not in data
            assert "gateway_pid" not in data
        finally:
            monkeypatch.setattr(
                web_server.app.state, "auth_required", False, raising=False
            )
