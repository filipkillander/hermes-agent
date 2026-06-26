"""Discord adapter import should not leak third-party deprecation warnings."""

import subprocess
import sys


def test_discord_adapter_import_suppresses_audioop_deprecation_warning():
    script = """
import importlib.util
import warnings
try:
    discord_spec = importlib.util.find_spec('discord')
except ValueError:
    discord_spec = None
if discord_spec is None:
    raise SystemExit(0)
with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter('always')
    import plugins.platforms.discord.adapter  # noqa: F401
matches = [
    str(item.message)
    for item in caught
    if issubclass(item.category, DeprecationWarning) and 'audioop' in str(item.message)
]
if matches:
    raise SystemExit('unexpected audioop deprecation warning: ' + repr(matches))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
