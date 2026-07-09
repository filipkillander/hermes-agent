"""Deterministic, local outbound formatting for chat delivery surfaces.

The model/agent produces markdown.  :func:`prepare_delivery_content` is the
last semantic step before a platform adapter renders, chunks, or sends that
markdown.  It deliberately has no network, persistence, or content logging.

The renderer is fail-safe:

* ``enforce`` (default) uses the structured ``ResponseDocument`` renderer;
* ``lkg`` selects the deliberately smaller last-known-good renderer;
* ``off`` is a per-surface kill switch and returns the input unchanged;
* any unexpected renderer failure falls back to LKG, then to the original.

The mode comes from the surface's ``config.yaml`` platform config:
``platforms.<surface>.extra.delivery_envelope``.  It is never read from
``.env`` because behavioral settings are not secrets.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from typing import Any, Iterable

from gateway.platforms.helpers import convert_table_to_bullets


CHAT_SURFACES = frozenset({"discord", "telegram"})


class DeliveryMode(str, Enum):
    ENFORCE = "enforce"
    LKG = "lkg"
    OFF = "off"


@dataclass(frozen=True)
class DocumentBlock:
    """A markdown block whose bytes are either prose or fenced code."""

    kind: str
    text: str


@dataclass(frozen=True)
class ResponseDocument:
    """Minimal semantic document used by the delivery renderer."""

    blocks: tuple[DocumentBlock, ...]

    @classmethod
    def parse(cls, content: str) -> "ResponseDocument":
        return cls(tuple(_split_fenced_blocks(content)))


@dataclass(frozen=True)
class DeliveryEnvelope:
    """Result metadata kept in-process for tests and future observability.

    It intentionally contains no destination, credential, or serialized log
    representation.  Production callers normally consume only ``content``.
    """

    surface: str
    mode: DeliveryMode
    content: str
    used_fallback: bool = False


_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")
_BLOCKQUOTE_RE = re.compile(r"^\s{0,3}>+\s?")
_HORIZONTAL_RULE_RE = re.compile(r"^\s{0,3}(?:\*\s*){3,}$|^\s{0,3}(?:-\s*){3,}$|^\s{0,3}(?:_\s*){3,}$")


def _split_fenced_blocks(content: str) -> Iterable[DocumentBlock]:
    """Split markdown while preserving fenced code byte-for-byte.

    Both backtick and tilde fences are supported, including unclosed fences.
    A closing fence must use the same marker and at least the opening length.
    """

    lines = content.splitlines(keepends=True)
    if not lines:
        yield DocumentBlock("prose", content)
        return

    current: list[str] = []
    in_fence = False
    fence_char = ""
    fence_len = 0

    def flush(kind: str) -> DocumentBlock | None:
        if not current:
            return None
        block = DocumentBlock(kind, "".join(current))
        current.clear()
        return block

    for line in lines:
        match = _FENCE_RE.match(line)
        marker = match.group(1) if match else ""
        if not in_fence and marker:
            block = flush("prose")
            if block:
                yield block
            in_fence = True
            fence_char = marker[0]
            fence_len = len(marker)
            current.append(line)
            continue
        if in_fence:
            current.append(line)
            if marker and marker[0] == fence_char and len(marker) >= fence_len:
                block = flush("fenced_code")
                if block:
                    yield block
                in_fence = False
                fence_char = ""
                fence_len = 0
            continue
        current.append(line)

    block = flush("fenced_code" if in_fence else "prose")
    if block:
        yield block


def _semantic_heading(line: str) -> str:
    match = _HEADING_RE.match(line)
    if not match:
        return line
    title = match.group(1).strip()
    if title.startswith("**") and title.endswith("**"):
        return title
    return f"**{title}**"


def _render_prose(prose: str) -> str:
    """Render chat-safe prose without touching fenced code."""

    if not prose:
        return prose
    had_final_newline = prose.endswith("\n")
    prose = convert_table_to_bullets(prose)
    rendered: list[str] = []
    for line in prose.splitlines():
        if _HORIZONTAL_RULE_RE.fullmatch(line):
            continue
        line = _BLOCKQUOTE_RE.sub("", line)
        rendered.append(_semantic_heading(line).rstrip())
    text = "\n".join(rendered)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if had_final_newline and text:
        return text + "\n"
    return text


def _render_document(document: ResponseDocument) -> str:
    return "".join(
        block.text if block.kind == "fenced_code" else _render_prose(block.text)
        for block in document.blocks
    )


def _render_lkg(content: str) -> str:
    """Small deterministic fallback with the same safety invariants.

    This path intentionally avoids the document dataclasses.  It is separately
    selectable for rollback and is also used if the structured renderer raises.
    """

    out: list[str] = []
    prose: list[str] = []
    in_fence = False
    fence_char = ""
    fence_len = 0
    def flush_prose() -> None:
        if not prose:
            return
        converted = convert_table_to_bullets("".join(prose))
        prose.clear()
        for prose_line in converted.splitlines(keepends=True):
            bare = prose_line.rstrip("\r\n")
            ending = prose_line[len(bare):]
            if _HORIZONTAL_RULE_RE.fullmatch(bare):
                continue
            bare = _BLOCKQUOTE_RE.sub("", bare)
            out.append(_semantic_heading(bare).rstrip() + ending)

    for line in content.splitlines(keepends=True):
        match = _FENCE_RE.match(line)
        marker = match.group(1) if match else ""
        if marker:
            if not in_fence:
                flush_prose()
                in_fence = True
                fence_char = marker[0]
                fence_len = len(marker)
            elif marker[0] == fence_char and len(marker) >= fence_len:
                in_fence = False
            out.append(line)
            continue
        if in_fence:
            out.append(line)
            continue
        prose.append(line)
    flush_prose()
    return "".join(out)


def _configured_mode(mode: DeliveryMode | str | None = None) -> DeliveryMode:
    if isinstance(mode, DeliveryMode):
        return mode
    normalized = str(mode or DeliveryMode.ENFORCE.value).strip().lower()
    aliases = {
        "1": DeliveryMode.ENFORCE,
        "true": DeliveryMode.ENFORCE,
        "on": DeliveryMode.ENFORCE,
        "0": DeliveryMode.OFF,
        "false": DeliveryMode.OFF,
        "disabled": DeliveryMode.OFF,
    }
    if normalized in aliases:
        return aliases[normalized]
    try:
        return DeliveryMode(normalized)
    except ValueError:
        # A typo must fail safe into enforcement, never silently disable it.
        return DeliveryMode.ENFORCE


def build_delivery_envelope(
    content: str | None,
    *,
    surface: str | None,
    mode: DeliveryMode | str | None = None,
) -> DeliveryEnvelope:
    """Build a local delivery envelope without ever returning empty content.

    Non-empty whitespace input becomes a visible safe placeholder so an
    attempted text delivery never reaches a transport as empty. Non-chat
    surfaces and the explicit ``off`` mode are unchanged.
    """

    original = "" if content is None else str(content)
    normalized_surface = (surface or "").strip().lower()
    mode = _configured_mode(mode)
    if normalized_surface not in CHAT_SURFACES or mode is DeliveryMode.OFF or not original:
        return DeliveryEnvelope(normalized_surface, mode, original)

    if not original.strip():
        return DeliveryEnvelope(normalized_surface, mode, "…", used_fallback=True)

    used_fallback = False
    try:
        if mode is DeliveryMode.LKG:
            rendered = _render_lkg(original)
        else:
            rendered = _render_document(ResponseDocument.parse(original))
    except Exception:
        used_fallback = True
        try:
            rendered = _render_lkg(original)
        except Exception:
            rendered = original

    # Removing a rule-only response would create an invalid empty platform
    # send.  Use a visible, platform-safe LKG placeholder instead.
    if not rendered.strip():
        rendered = "…"
        used_fallback = True
    return DeliveryEnvelope(normalized_surface, mode, rendered, used_fallback)


def prepare_delivery_content(
    content: str | None,
    *,
    surface: str | None,
    mode: DeliveryMode | str | None = None,
) -> str:
    """Return the envelope's content for an adapter send/edit path."""

    return build_delivery_envelope(content, surface=surface, mode=mode).content


def delivery_mode_from_platform_config(config: Any) -> DeliveryMode:
    """Resolve ``extra.delivery_envelope`` from a ``PlatformConfig``-like object."""

    extra = getattr(config, "extra", None)
    raw = extra.get("delivery_envelope") if isinstance(extra, dict) else None
    return _configured_mode(raw)


def prepare_platform_delivery_content(
    content: str | None,
    *,
    surface: str,
    config: Any,
) -> str:
    """Prepare content using a surface's ``PlatformConfig`` mode."""

    return prepare_delivery_content(
        content,
        surface=surface,
        mode=delivery_mode_from_platform_config(config),
    )


__all__ = [
    "CHAT_SURFACES",
    "DeliveryEnvelope",
    "DeliveryMode",
    "DocumentBlock",
    "ResponseDocument",
    "build_delivery_envelope",
    "delivery_mode_from_platform_config",
    "prepare_delivery_content",
    "prepare_platform_delivery_content",
]
