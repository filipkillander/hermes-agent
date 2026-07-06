"""Mechanical formatting gate for chat-platform outbound text.

The gate enforces a small subset of formatting-harness rules mechanically for
Discord/Telegram chat output. It is intentionally pure and platform-scoped so
richer surfaces (mail, files, dashboard, CLI) keep their native formatting.
"""

from __future__ import annotations

import re
from typing import Iterable

from gateway.platforms.helpers import convert_table_to_bullets

CHAT_FORMAT_GATE_PLATFORMS = frozenset({"discord", "telegram"})
CHAT_FORMAT_RULE_IDS = (
    "GFM_TABLE",
    "HORIZONTAL_RULE",
    "BLOCKQUOTE",
    "REPORT_HEADING",
    "EMPTY_RITUAL_SECTION",
)

_HORIZONTAL_RULE_RE = re.compile(r"^\s{0,3}([-*_])(?:\s*\1){2,}\s*$")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_EMPTY_RITUAL_RE = re.compile(
    r"^\s*(?:Saknas|Missing|N/?A|Ej aktuellt|Inget)\s*:\s*(?:Inget|None|N/?A|Ej aktuellt|-)\s*\.?\s*$",
    re.IGNORECASE,
)
_BLOCKQUOTE_RE = re.compile(r"^\s{0,3}>{1,3}\s?")


def _is_chat_platform(platform: str | None) -> bool:
    return (platform or "").lower() in CHAT_FORMAT_GATE_PLATFORMS


def _split_fenced_segments(text: str) -> Iterable[tuple[bool, str]]:
    """Yield ``(is_fenced_code, segment)`` preserving original fence lines."""
    lines = text.splitlines(keepends=True)
    current: list[str] = []
    in_fence = False
    segment_is_fenced = False

    for line in lines:
        stripped = line.lstrip()
        is_fence = stripped.startswith("```")
        if is_fence:
            if current:
                yield segment_is_fenced, "".join(current)
                current = []
            in_fence = not in_fence
            segment_is_fenced = True
            current.append(line)
            if not in_fence:
                yield True, "".join(current)
                current = []
                segment_is_fenced = False
            continue

        if not current:
            segment_is_fenced = in_fence
        current.append(line)

    if current:
        yield segment_is_fenced, "".join(current)


def _rewrite_heading(line: str, *, preserve: bool = False) -> str:
    if preserve:
        return line
    match = _HEADING_RE.match(line)
    if not match:
        return line
    marker, title = match.groups()
    title = title.strip()
    if title.startswith("**") and title.endswith("**"):
        return title
    return f"**{title}**"


def _rewrite_prose_segment(segment: str) -> str:
    if not segment:
        return segment

    # Existing shared table renderer already handles table detection robustly;
    # run it only outside fenced code via the caller's segmentation.
    segment = convert_table_to_bullets(segment)

    had_final_newline = segment.endswith("\n")
    lines = segment.splitlines()
    preserve_cron_title = (
        len(lines) >= 2
        and lines[0].startswith("# ")
        and lines[1].startswith("**Job ID**")
    )
    out: list[str] = []
    for index, line in enumerate(lines):
        if _EMPTY_RITUAL_RE.match(line):
            continue
        if _HORIZONTAL_RULE_RE.match(line):
            continue
        line = _BLOCKQUOTE_RE.sub("", line)
        line = _rewrite_heading(line, preserve=preserve_cron_title and index == 0)
        out.append(line.rstrip())

    rewritten = "\n".join(out)
    # Collapse excessive blank lines introduced by removals, and trim trailing
    # blank space so idempotence is stable.
    rewritten = re.sub(r"\n{3,}", "\n\n", rewritten).strip()
    if had_final_newline and rewritten:
        return rewritten + "\n"
    return rewritten


def apply_chat_format_gate(
    content: str | None,
    *,
    platform: str | None,
    enabled: bool = True,
) -> str | None:
    """Return content rewritten for compact Discord/Telegram chat output.

    The function is a no-op unless ``enabled`` is true and ``platform`` is one of
    ``discord`` or ``telegram``. Fenced code blocks are preserved verbatim.
    """
    if not enabled or not _is_chat_platform(platform) or not content:
        return content

    return "".join(
        segment if is_fenced else _rewrite_prose_segment(segment)
        for is_fenced, segment in _split_fenced_segments(content)
    ).strip()
