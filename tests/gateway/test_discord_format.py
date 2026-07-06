"""Discord format_message: tables converted to bullet groups."""

import types
import sys


def _make_discord_adapter():
    """Construct a DiscordAdapter with discord.py stubbed out."""
    fake_discord = types.ModuleType("discord")
    fake_discord.Intents = type("Intents", (), {"default": classmethod(lambda cls: cls())})
    fake_discord.Message = object
    fake_ext = types.ModuleType("discord.ext")
    fake_commands = types.ModuleType("discord.ext.commands")
    fake_ext.commands = fake_commands
    fake_discord.ext = fake_ext
    sys.modules.setdefault("discord", fake_discord)
    sys.modules.setdefault("discord.ext", fake_ext)
    sys.modules.setdefault("discord.ext.commands", fake_commands)

    from plugins.platforms.discord.adapter import DiscordAdapter
    adapter = object.__new__(DiscordAdapter)
    return adapter


class TestDiscordFormatMessage:

    def test_table_converted_to_bullets(self):
        adapter = _make_discord_adapter()
        text = (
            "Results:\n\n"
            "| Name | Score |\n"
            "|------|-------|\n"
            "| Alice | 95   |\n"
            "| Bob   | 80   |\n"
            "\nDone."
        )
        out = adapter.format_message(text)
        assert "**Alice**" in out
        assert "• Score: 95" in out
        assert "**Bob**" in out
        assert "• Score: 80" in out
        assert out.startswith("Results:")
        assert out.rstrip().endswith("Done.")
        assert "|---" not in out

    def test_plain_text_unchanged(self):
        adapter = _make_discord_adapter()
        text = "Hello world, no tables here."
        assert adapter.format_message(text) == text

    def test_code_block_table_unchanged(self):
        adapter = _make_discord_adapter()
        text = "```\n| a | b |\n|---|---|\n| 1 | 2 |\n```"
        assert adapter.format_message(text) == text

    def test_empty_string(self):
        adapter = _make_discord_adapter()
        assert adapter.format_message("") == ""

    def test_format_gate_rewrites_chat_report_markdown(self):
        adapter = _make_discord_adapter()
        text = "## Status\n\n> report quote\n\n---\nSaknas: Inget"

        out = adapter.format_message(text)

        assert out == "**Status**\n\nreport quote"


    def test_format_gate_lint_only_config_does_not_rewrite(self):
        from gateway.config import PlatformConfig

        adapter = _make_discord_adapter()
        adapter.config = PlatformConfig(
            enabled=True,
            token="***",
            extra={"format_gate": "lint-only"},
        )

        assert adapter.format_message("## Status") == "## Status"

    def test_format_gate_zero_config_disables_rewrite(self):
        from gateway.config import PlatformConfig

        adapter = _make_discord_adapter()
        adapter.config = PlatformConfig(
            enabled=True,
            token="***",
            extra={"format_gate": 0},
        )

        assert adapter.format_message("## Status\n\n---") == "## Status\n\n---"

    def test_format_gate_can_be_disabled_without_disabling_existing_table_rewrite(self):
        from gateway.config import PlatformConfig

        adapter = _make_discord_adapter()
        adapter.config = PlatformConfig(
            enabled=True,
            token="***",
            extra={"format_gate": False},
        )
        text = "## Status\n\n> report quote\n\n---"

        assert adapter.format_message(text) == text
