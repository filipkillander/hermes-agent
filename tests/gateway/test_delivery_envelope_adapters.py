"""Adapter-boundary proof for legacy, rich, send, edit, and draft paths."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.discord.adapter import DiscordAdapter
from plugins.platforms.telegram.adapter import TelegramAdapter
from tools import send_message_tool


SOURCE = "# Status\n\n---\n| A | B |\n|---|---|\n| one | two |"


def _discord_adapter():
    return object.__new__(DiscordAdapter)


@pytest.fixture()
def telegram_adapter():
    return TelegramAdapter(PlatformConfig(enabled=True, token="not-a-real-token"))


def test_discord_legacy_formatter_is_envelope_boundary():
    rendered = _discord_adapter().format_message(SOURCE)
    assert rendered.startswith("**Status**")
    assert "\n---\n" not in rendered
    assert "|---|" not in rendered


def test_telegram_legacy_formatter_is_envelope_boundary(telegram_adapter):
    rendered = telegram_adapter.format_message(SOURCE)
    assert rendered.startswith("*Status*")
    assert "\\-\\-\\-" not in rendered
    assert "|---|" not in rendered


def test_telegram_rich_payload_cannot_bypass_envelope(telegram_adapter):
    payload = telegram_adapter._rich_message_payload(SOURCE)
    rendered = payload["markdown"]
    assert rendered.startswith("**Status**")
    assert "\n---\n" not in rendered
    assert "|---|" not in rendered


def test_telegram_rich_payload_honors_surface_kill_switch():
    adapter = TelegramAdapter(
        PlatformConfig(
            enabled=True,
            token="not-a-real-token",
            extra={"delivery_envelope": "off"},
        )
    )
    rendered = adapter._rich_message_payload(SOURCE)["markdown"]
    assert rendered.startswith("# Status")
    assert "|---|" in rendered


@pytest.mark.asyncio
async def test_telegram_interim_native_draft_is_enveloped(telegram_adapter):
    bot = MagicMock()
    bot.send_message_draft = AsyncMock(return_value=True)
    telegram_adapter._bot = bot
    telegram_adapter._rich_messages_enabled = False
    telegram_adapter._metadata_thread_id = MagicMock(return_value=None)

    result = await telegram_adapter.send_draft("123", 7, SOURCE)

    assert result.success
    sent = bot.send_message_draft.await_args.kwargs["text"]
    assert sent.startswith("*Status*")
    assert "|---|" not in sent


@pytest.mark.asyncio
async def test_discord_send_formats_before_chunk_and_transport(monkeypatch):
    adapter = _discord_adapter()
    channel = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=1)))
    client = MagicMock()
    client.get_channel.return_value = channel
    adapter._client = client
    adapter._reply_to_mode = "off"
    adapter._nonconversational_messages = MagicMock()
    adapter._last_self_message_id = {}
    monkeypatch.setattr(adapter, "truncate_message", lambda content, _limit: [content])
    adapter.MAX_MESSAGE_LENGTH = 2000

    result = await adapter.send("123", SOURCE)

    assert result.success
    sent = channel.send.await_args.kwargs["content"]
    assert sent.startswith("**Status**")
    assert "|---|" not in sent


@pytest.mark.asyncio
async def test_discord_interim_edit_is_enveloped():
    adapter = _discord_adapter()
    message = SimpleNamespace(edit=AsyncMock())
    channel = SimpleNamespace(fetch_message=AsyncMock(return_value=message))
    client = MagicMock()
    client.get_channel.return_value = channel
    adapter._client = client
    adapter._last_overflow_preview = {}
    adapter.MAX_MESSAGE_LENGTH = 2000

    result = await adapter.edit_message("123", "5", SOURCE, finalize=False)

    assert result.success
    delivered = message.edit.await_args.kwargs["content"]
    assert delivered.startswith("**Status**")
    assert "|---|" not in delivered


@pytest.mark.asyncio
async def test_telegram_interim_edit_is_enveloped(telegram_adapter):
    bot = MagicMock()
    bot.edit_message_text = AsyncMock(return_value=True)
    telegram_adapter._bot = bot
    telegram_adapter._last_overflow_preview = {}

    result = await telegram_adapter.edit_message(
        "123", "5", SOURCE, finalize=False
    )

    assert result.success
    delivered = bot.edit_message_text.await_args.kwargs["text"]
    assert delivered.startswith("**Status**")
    assert "|---|" not in delivered


@pytest.mark.asyncio
async def test_no_agent_telegram_send_message_is_enveloped(monkeypatch):
    standalone = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(send_message_tool, "_send_telegram", standalone)
    from gateway.config import Platform

    result = await send_message_tool._send_to_platform(
        Platform.TELEGRAM,
        SimpleNamespace(token="not-a-real-token", extra={}),
        "123",
        SOURCE,
    )

    assert result["success"]
    delivered = standalone.await_args.args[2]
    assert delivered.startswith("**Status**")
    assert "|---|" not in delivered


@pytest.mark.asyncio
async def test_no_agent_discord_send_message_is_enveloped(monkeypatch):
    standalone = AsyncMock(return_value={"success": True})
    entry = SimpleNamespace(max_message_length=2000, standalone_sender_fn=standalone)
    from gateway.config import Platform
    from gateway.platform_registry import platform_registry

    monkeypatch.setattr(platform_registry, "get", lambda _name: entry)
    result = await send_message_tool._send_to_platform(
        Platform.DISCORD,
        SimpleNamespace(token="not-a-real-token", extra={}),
        "123",
        SOURCE,
    )

    assert result["success"]
    delivered = standalone.await_args.args[2]
    assert delivered.startswith("**Status**")
    assert "|---|" not in delivered


def test_discord_slash_prompt_helper_is_enveloped():
    adapter = _discord_adapter()
    adapter.MAX_MESSAGE_LENGTH = 2000
    rendered = adapter._self_contained_prompt_content("**Confirm**", SOURCE)
    assert "|---|" not in rendered
    assert "\n---\n" not in rendered
