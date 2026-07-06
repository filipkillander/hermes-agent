"""Mechanical chat formatting gate for Discord/Telegram output."""

import textwrap

from gateway.format_gate import apply_chat_format_gate


def test_chat_gate_rewrites_report_markdown_outside_fenced_code():
    text = textwrap.dedent("""\
        ## Verification

        | Check | Result |
        | --- | --- |
        | Sync | ✅ |

        ---
        > quoted report prose
        Saknas: Inget

        ```md
        ## keep heading
        | keep | table |
        | --- | --- |
        > keep quote
        ---
        ```
        """)

    out = apply_chat_format_gate(text, platform="discord")

    assert "**Verification**" in out
    assert "**Sync**" in out
    assert "• Result: ✅" in out
    assert "quoted report prose" in out
    assert "> quoted report prose" not in out
    assert "Saknas: Inget" not in out
    before_fence, fenced_tail = out.split("```md", 1)
    assert "\n---\n" not in before_fence

    fenced = fenced_tail.split("```", 1)[0]
    assert "## keep heading" in fenced
    assert "| keep | table |" in fenced
    assert "> keep quote" in fenced
    assert "---" in fenced


def test_chat_gate_is_idempotent_for_rewritten_text():
    text = "## Done\n\n> shipped\n\n---\n"
    once = apply_chat_format_gate(text, platform="telegram")
    twice = apply_chat_format_gate(once, platform="telegram")

    assert twice == once
    assert once == "**Done**\n\nshipped"


def test_chat_gate_is_platform_scoped():
    text = "## Report\n\n> keep this in files\n\n---\n"

    assert apply_chat_format_gate(text, platform="email") == text
    assert apply_chat_format_gate(text, platform="file") == text



def test_chat_gate_can_remove_only_ritual_markup_to_empty_string():
    assert apply_chat_format_gate("---\n", platform="discord") == ""



def test_chat_gate_rewrites_h1_report_heading_but_preserves_cron_header():
    assert apply_chat_format_gate("# Report\nBody", platform="discord") == "**Report**\nBody"

    cron = "# Daily check\n**Job ID**: abc123\n## Details\nDone"
    out = apply_chat_format_gate(cron, platform="telegram")

    assert out.startswith("# Daily check\n**Job ID**: abc123")
    assert "**Details**" in out


def test_chat_gate_passes_through_for_raycast_platform():
    """Raycast is an external API client surface, not a chat adapter.

    apply_chat_format_gate must return content unchanged for raycast so the
    agent's native markdown is preserved for the Raycast extension to render.
    This is a guardrail regression test — raycast is explicitly out of scope.
    """
    text = "## Report\n\n> keep this\n\n---\n| a | b |\n|---|---|\n| 1 | 2 |"
    assert apply_chat_format_gate(text, platform="raycast") == text


def test_chat_gate_passes_through_for_chrome_platform():
    """Chrome is an extension/tool surface, not a chat adapter.

    apply_chat_format_gate must return content unchanged for chrome so the
    agent's native markdown is preserved for the Chrome extension to render.
    This is a guardrail regression test — chrome is explicitly out of scope.
    """
    text = "## Report\n\n> keep this\n\n---\n| a | b |\n|---|---|\n| 1 | 2 |"
    assert apply_chat_format_gate(text, platform="chrome") == text


def test_chat_gate_passes_through_for_email_platform():
    """Email keeps its native formatting; the chat gate must not touch it.

    This is an explicit guardrail regression test complementing the existing
    email/file pass-through assertions.
    """
    text = "## Report\n\n> keep this\n\n---\n| a | b |\n|---|---|\n| 1 | 2 |"
    assert apply_chat_format_gate(text, platform="email") == text
