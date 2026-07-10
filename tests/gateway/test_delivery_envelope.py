"""Contract tests for the model-independent outbound delivery envelope."""

from __future__ import annotations

import random
import re

import pytest

import gateway.delivery_envelope as envelope_module
from gateway.delivery_envelope import (
    DeliveryMode,
    ResponseDocument,
    build_delivery_envelope,
    prepare_delivery_content,
    prepare_platform_delivery_content,
)
from gateway.config import PlatformConfig


TABLE = "| Name | URL |\n|---|---|\n| Lumi | https://example.test/a?x=1&y=2 |"


@pytest.mark.parametrize("surface", ["discord", "telegram", "raycast_extension"])
def test_golden_chat_rendering(surface):
    source = (
        "# Status\n\n"
        "> quoted prose\n\n"
        "---\n\n"
        f"{TABLE}\n\n"
        "![plot](https://cdn.example.test/plot.png)\n\n"
        "```md\n# literal heading\n---\n| a | b |\n|---|---|\n```"
    )

    rendered = prepare_delivery_content(source, surface=surface)
    prose_before_fence = rendered.split("```md", 1)[0]

    assert rendered.startswith("**Status**")
    assert "> quoted prose" in rendered
    assert "\n---\n" not in prose_before_fence
    assert "**Lumi**" in rendered
    assert "https://example.test/a?x=1&y=2" in rendered
    assert "![plot](https://cdn.example.test/plot.png)" in rendered
    assert "```md\n# literal heading\n---\n| a | b |\n|---|---|\n```" in rendered


@pytest.mark.parametrize(
    "source",
    [
        "plain",
        "## Heading\nBody",
        "> quote\n\n***\nbody",
        TABLE,
        "before\n```python\nprint('https://example.test/a|b')\n```\nafter",
        "before\n~~~sh\nprintf '# | ---'\n~~~\nafter",
        "[label](https://example.test/a_(b)?q=x|y)",
        "![alt](https://cdn.example.test/pic.webp)",
        "`inline | code` and https://example.test/x#anchor",
        "# 日本語\n\nText 🚀",
    ],
)
@pytest.mark.parametrize("surface", ["discord", "telegram"])
def test_property_idempotence(source, surface):
    once = prepare_delivery_content(source, surface=surface)
    twice = prepare_delivery_content(once, surface=surface)
    assert twice == once
    assert once.strip()


def test_response_document_preserves_closed_and_unclosed_fences_exactly():
    source = "before\n```py\na = '|---|'\n```\nafter\n~~~\nunclosed\n"
    document = ResponseDocument.parse(source)
    fenced = [block.text for block in document.blocks if block.kind == "fenced_code"]
    assert fenced == ["```py\na = '|---|'\n```\n", "~~~\nunclosed\n"]


@pytest.mark.parametrize(
    "source, expected_fence",
    [
        (
            "Intro\n```python\n  value = '  keep  '  \n```\n",
            "```python\n  value = '  keep  '  \n```\n",
        ),
        (
            "Intro\n~~~text\n  unclosed | bytes  \n",
            "~~~text\n  unclosed | bytes  \n",
        ),
    ],
)
@pytest.mark.parametrize("mode", ["enforce", "lkg"])
def test_rendered_fenced_segment_is_byte_stable_at_response_end(
    source, expected_fence, mode
):
    rendered = prepare_delivery_content(source, surface="telegram", mode=mode)
    fenced = [
        block.text
        for block in ResponseDocument.parse(rendered).blocks
        if block.kind == "fenced_code"
    ]
    assert fenced == [expected_fence]
    assert rendered.endswith(expected_fence)
    assert prepare_delivery_content(
        rendered, surface="telegram", mode=mode
    ) == rendered


def test_surface_kill_switch_and_lkg_modes_are_independent():
    source = "# Header\n\n---\nBody"
    discord = build_delivery_envelope(source, surface="discord", mode="off")
    telegram = build_delivery_envelope(source, surface="telegram", mode="lkg")
    assert discord.mode is DeliveryMode.OFF and discord.content == source
    assert telegram.mode is DeliveryMode.LKG
    assert telegram.content == "**Header**\n\nBody"


def test_invalid_mode_fails_closed_to_enforcement():
    result = build_delivery_envelope(
        "# Header", surface="telegram",
        mode="typo",
    )
    assert result.mode is DeliveryMode.ENFORCE
    assert result.content == "**Header**"


def test_platform_config_controls_per_surface_mode():
    source = "# Header\n\n---\nBody"
    off = PlatformConfig(extra={"delivery_envelope": "off"})
    lkg = PlatformConfig(extra={"delivery_envelope": "lkg"})
    assert prepare_platform_delivery_content(
        source, surface="discord", config=off
    ) == source
    assert prepare_platform_delivery_content(
        source, surface="telegram", config=lkg
    ) == "**Header**\n\nBody"


def test_primary_failure_uses_lkg_without_content_logging(monkeypatch, caplog):
    def fail(_document):
        raise RuntimeError("secret-content-must-not-be-logged")

    monkeypatch.setattr(envelope_module, "_render_document", fail)
    result = build_delivery_envelope(
        "# secret-content-must-not-be-logged\nBody", surface="discord",
    )
    assert result.used_fallback is True
    assert result.content.startswith("**secret-content-must-not-be-logged**")
    assert "secret-content-must-not-be-logged" not in caplog.text


def test_lkg_preserves_tilde_fenced_table():
    source = f"{TABLE}\n\n~~~md\n{TABLE}\n~~~"
    rendered = prepare_delivery_content(source, surface="telegram", mode="lkg")
    prose, fenced = rendered.split("~~~md", 1)
    assert "|---|" not in prose
    assert TABLE in fenced


@pytest.mark.parametrize("source", ["---", "***\n___", " \n\t"])
def test_never_returns_empty_output(source):
    assert prepare_delivery_content(source, surface="telegram") == "…"


def test_non_chat_surfaces_are_unchanged():
    source = f"# Mail\n\n---\n{TABLE}"
    assert prepare_delivery_content(source, surface="email") == source


def test_chrome_rich_markdown_is_not_compacted_by_delivery_envelope():
    source = f"# Chrome\n\n> quote\n\n{TABLE}"
    assert prepare_delivery_content(source, surface="chrome_extension") == source


def test_fuzz_like_determinism_and_invariants():
    rng = random.Random(20260710)
    atoms = [
        "plain text",
        "# heading",
        "> quote",
        "---",
        TABLE,
        "https://example.test/a?x=1&y=2",
        "![media](https://cdn.example.test/a.png)",
        "```txt\n# keep\n---\n| a | b |\n|---|---|\n```",
        "`inline | code`",
        "emoji 🎛️",
    ]
    for _ in range(100):
        source = "\n\n".join(rng.choice(atoms) for _ in range(rng.randint(1, 12)))
        for surface in ("discord", "telegram"):
            first = prepare_delivery_content(source, surface=surface)
            second = prepare_delivery_content(source, surface=surface)
            assert first == second
            assert prepare_delivery_content(first, surface=surface) == first
            assert first.strip()
            assert "https://example.test/a?x=1&y=2" in first or "https://example.test/a?x=1&y=2" not in source
            assert "![media](https://cdn.example.test/a.png)" in first or "![media]" not in source
            prose = re.sub(r"```[\s\S]*?```", "", first)
            assert not re.search(r"(?m)^\s{0,3}(?:---+|___+|\*\*\*+)\s*$", prose)
