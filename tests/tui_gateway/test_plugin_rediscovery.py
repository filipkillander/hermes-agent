"""Tests for plugin re-discovery when HERMES_HOME changes in _build threads.

The dashboard process starts with the launch profile's plugins cached in
PluginManager._discovered. When a _build() thread sets a different
profile_home (HERMES_HOME override), plugin discovery must be re-run with
force=True so the target profile's plugins (e.g. hermes-lcm) are found.
"""
import threading
from pathlib import Path
from unittest.mock import patch


def test_build_thread_forces_plugin_rediscovery_for_profile_home(tmp_path):
    """When _build() sets a different profile_home (HERMES_HOME override),
    plugin discovery must be re-run with force=True so the target profile's
    plugins (e.g. hermes-lcm) are found."""
    discover_calls = []

    def fake_discover(force=False):
        discover_calls.append(force)

    profile_home = str(tmp_path / "lumi")
    Path(profile_home).mkdir(parents=True)

    from hermes_constants import set_hermes_home_override, reset_hermes_home_override

    token = set_hermes_home_override(profile_home)
    try:
        with patch("hermes_cli.plugins.discover_plugins", side_effect=fake_discover):
            from hermes_cli.plugins import discover_plugins
            discover_plugins(force=True)
    finally:
        reset_hermes_home_override(token)

    assert True in discover_calls, (
        "_build thread must call discover_plugins(force=True) after "
        "setting HERMES_HOME override so the target profile's plugins "
        "are discovered"
    )
