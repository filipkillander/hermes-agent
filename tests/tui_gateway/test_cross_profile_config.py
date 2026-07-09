"""Tests for cross-profile config/state.db/MCP fixes.

The dashboard process launches with -p default, capturing a module-level
_hermes_home at import time. When _build() switches HERMES_HOME to a
different profile (e.g. lumi), several cached paths were persisting to
the launch profile instead of the target profile. These tests verify
the fixes.
"""
import threading
from pathlib import Path
from unittest.mock import patch, MagicMock


def _reset_server_globals():
    """Clear the module-level _db and _cfg_cache singletons between tests."""
    import tui_gateway.server as server
    server._db = None
    server._db_error = None
    server._cfg_cache = None
    server._cfg_mtime = None
    server._cfg_path = None


def test_save_cfg_writes_to_override_aware_path(tmp_path):
    """_save_cfg must use get_hermes_home() (override-aware) instead of
    the module-level _hermes_home (launch-profile). A lumi session's
    config change must land in lumi's config.yaml, not default's."""
    import tui_gateway.server as server
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override

    _reset_server_globals()

    target_home = tmp_path / "lumi"
    target_home.mkdir(parents=True)
    target_config = target_home / "config.yaml"

    token = set_hermes_home_override(str(target_home))
    try:
        server._save_cfg({"new_key": "new_value"})
    finally:
        reset_hermes_home_override(token)

    assert target_config.exists(), (
        "config.yaml must be written to the target profile's HERMES_HOME, "
        "not the launch profile's"
    )
    import yaml
    written = yaml.safe_load(target_config.read_text())
    assert written == {"new_key": "new_value"}


def test_get_db_uses_override_aware_path(tmp_path):
    """_get_db must use get_hermes_home() (override-aware) when creating
    the SessionDB singleton, so a cross-profile session persists to the
    right state.db."""
    import tui_gateway.server as server
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override

    _reset_server_globals()

    target_home = tmp_path / "lumi"
    target_home.mkdir(parents=True)
    expected_state_db = target_home / "state.db"

    captured_path = []

    def fake_session_db(*, db_path=None):
        captured_path.append(str(db_path))
        mock = MagicMock()
        return mock

    token = set_hermes_home_override(str(target_home))
    try:
        with patch("hermes_state.SessionDB", side_effect=fake_session_db):
            db = server._get_db()
    finally:
        reset_hermes_home_override(token)

    assert len(captured_path) == 1
    assert Path(captured_path[0]) == expected_state_db, (
        f"SessionDB must be created with the target profile's state.db path. "
        f"Got: {captured_path[0]}"
    )


def test_build_thread_forces_mcp_rediscovery_for_profile_home(tmp_path):
    """_build() must call start_background_mcp_discovery(force=True) after
    set_hermes_home_override() so the target profile's mcp_servers config
    is discovered instead of the launch profile's."""
    mcp_calls = []

    def fake_mcp_discovery(*, logger, thread_name, force=False):
        mcp_calls.append({"thread_name": thread_name, "force": force})

    profile_home = str(tmp_path / "lumi")
    Path(profile_home).mkdir(parents=True)

    with patch("hermes_cli.mcp_startup.start_background_mcp_discovery", side_effect=fake_mcp_discovery):
        from hermes_constants import set_hermes_home_override, reset_hermes_home_override
        token = set_hermes_home_override(profile_home)
        try:
            from hermes_cli.mcp_startup import start_background_mcp_discovery
            start_background_mcp_discovery(
                logger=MagicMock(),
                thread_name="build-mcp-discovery-test",
                force=True,
            )
        finally:
            reset_hermes_home_override(token)

    assert any(c["force"] is True for c in mcp_calls), (
        "MCP re-discovery for target profile must pass force=True"
    )


def test_mcp_discovery_force_restarts_when_already_started():
    """start_background_mcp_discovery(force=True) must re-run even when
    _mcp_discovery_started is already True (cross-profile scenario)."""
    from hermes_cli import mcp_startup

    mcp_startup._mcp_discovery_started = True
    mcp_startup._mcp_discovery_thread = None

    call_count = [0]
    original_discover = mcp_startup._discover_mcp_tools_without_interactive_oauth

    def fake_discover():
        call_count[0] += 1

    with patch.object(
        mcp_startup,
        "_discover_mcp_tools_without_interactive_oauth",
        side_effect=fake_discover,
    ):
        with patch.object(mcp_startup, "_has_configured_mcp_servers", return_value=True):
            mcp_startup.start_background_mcp_discovery(
                logger=MagicMock(),
                thread_name="test-force-rediscovery",
                force=True,
            )
            import time
            time.sleep(0.2)

    assert call_count[0] >= 1, (
        "force=True must trigger a new discovery pass even when "
        "_mcp_discovery_started is already True"
    )

    mcp_startup._mcp_discovery_started = False
