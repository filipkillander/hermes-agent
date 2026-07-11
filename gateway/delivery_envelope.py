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


CHAT_SURFACES = frozenset({"discord", "telegram", "raycast_extension"})


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
_HORIZONTAL_RULE_RE = re.compile(r"^\s{0,3}(?:\*\s*){3,}$|^\s{0,3}(?:-\s*){3,}$|^\s{0,3}(?:_\s*){3,}$")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+\S")
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-+*•]\s+|\d+[.)]\s+)")
_BLOCKQUOTE_RE = re.compile(r"^\s{0,3}>\s?")
_KMROS_CASE_RE = re.compile(r"\bkmros\b", re.IGNORECASE)
_EMPTY_RITUAL_RE = re.compile(
    r"^\s*(?:saknas|kvar|vad\s+saknas|missing|remaining)\s*"
    r"(?::|[-–—])\s*(?:inget|none|n/?a|ej\s+aktuellt|0|-)\s*[.!]?\s*$",
    re.IGNORECASE,
)


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


def _line_kind(line: str) -> str:
    if _HEADING_RE.match(line):
        return "heading"
    if _LIST_ITEM_RE.match(line):
        return "list"
    if _BLOCKQUOTE_RE.match(line):
        return "blockquote"
    return "prose"


def _render_heading_for_surface(line: str, *, surface: str) -> str:
    """Preserve native Discord headings; compact unsupported surfaces.

    Telegram does not support Markdown headings and Raycast's compact result
    view is more readable with a bold section label.  The conversion is
    deliberately surface-specific so Discord can retain its native heading
    hierarchy.  Fenced code never reaches this function.
    """

    if surface not in {"telegram", "raycast_extension"}:
        return line
    match = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", line)
    if not match:
        return line
    title = match.group(1).strip()
    if title.startswith("**") and title.endswith("**"):
        return title
    return f"**{title}**"


def _normalize_chat_spacing(lines: list[str]) -> list[str]:
    """Apply one model-independent blank-line contract to chat blocks."""
    compact: list[str] = []
    for line in lines:
        if not line.strip():
            if compact and compact[-1] != "":
                compact.append("")
            continue
        compact.append(line.rstrip())

    while compact and compact[0] == "":
        compact.pop(0)
    while compact and compact[-1] == "":
        compact.pop()

    nonblank = [index for index, line in enumerate(compact) if line]
    if not nonblank:
        return []

    out: list[str] = []
    for position, index in enumerate(nonblank):
        line = compact[index]
        kind = _line_kind(line)
        if out:
            previous = out[-1]
            previous_kind = _line_kind(previous) if previous else "blank"
            between_had_blank = any(
                not value for value in compact[nonblank[position - 1] + 1:index]
            )
            same_tight_block = (
                kind == previous_kind and kind in {"list", "blockquote"}
            )
            block_boundary = (
                kind in {"heading", "list", "blockquote"}
                or previous_kind in {"heading", "list", "blockquote"}
            )
            if not same_tight_block and (between_had_blank or block_boundary):
                if out[-1] != "":
                    out.append("")
        out.append(line)
    return out


def _render_prose(prose: str, *, surface: str) -> str:
    """Render chat-safe prose without touching fenced code."""

    if not prose:
        return prose
    had_final_newline = prose.endswith("\n")
    prose = convert_table_to_bullets(prose)
    rendered: list[str] = []
    for line in prose.splitlines():
        if _HORIZONTAL_RULE_RE.fullmatch(line):
            continue
        if _EMPTY_RITUAL_RE.fullmatch(line):
            continue
        normalized = _KMROS_CASE_RE.sub("kmrOS", line).rstrip()
        rendered.append(_render_heading_for_surface(normalized, surface=surface))
    if surface in {"discord", "telegram", "raycast_extension"}:
        rendered = _normalize_chat_spacing(rendered)
    text = "\n".join(rendered).strip("\n")
    if had_final_newline and text:
        return text + "\n"
    return text


def _render_document(document: ResponseDocument, *, surface: str) -> str:
    chunks: list[str] = []
    previous_kind: str | None = None
    for block in document.blocks:
        if block.kind == "fenced_code":
            if chunks and chunks[-1].strip():
                chunks[-1] = chunks[-1].rstrip("\n") + "\n\n"
            chunks.append(block.text)
            previous_kind = block.kind
            continue

        rendered = _render_prose(block.text, surface=surface)
        if not rendered:
            continue
        if previous_kind == "fenced_code" and chunks:
            prefix = "\n" if chunks[-1].endswith("\n") else "\n\n"
            rendered = prefix + rendered.lstrip("\n")
        chunks.append(rendered)
        previous_kind = block.kind
    return "".join(chunks)


def _render_lkg(content: str, *, surface: str) -> str:
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
            if _EMPTY_RITUAL_RE.fullmatch(bare):
                continue
            out.append(bare.rstrip() + ending)

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
    # LKG keeps fence bytes untouched; normalize only prose blocks in a second
    # fence-aware pass using the smaller prose renderer.
    rendered: list[str] = []
    for block in _split_fenced_blocks("".join(out)):
        if block.kind == "fenced_code":
            if rendered and rendered[-1].strip():
                rendered[-1] = rendered[-1].rstrip("\n") + "\n\n"
            rendered.append(block.text)
        else:
            prose = _render_prose(block.text, surface=surface)
            if rendered and rendered[-1].lstrip().startswith(("```", "~~~")) and prose:
                prose = ("\n" if rendered[-1].endswith("\n") else "\n\n") + prose.lstrip("\n")
            if prose:
                rendered.append(prose)
    return "".join(rendered)


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
            rendered = _render_lkg(original, surface=normalized_surface)
        else:
            rendered = _render_document(
                ResponseDocument.parse(original), surface=normalized_surface
            )
    except Exception:
        used_fallback = True
        try:
            rendered = _render_lkg(original, surface=normalized_surface)
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
