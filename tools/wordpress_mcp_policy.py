"""Fail-closed publication gates for KMR WordPress MCP connections.

Easy MCP's Force Draft setting protects create calls, but an update call can
otherwise promote that draft by passing ``status=publish``.  This guard keeps
routine edits to already-published objects useful while making publication of
new surfaces an owner/break-glass action outside the normal agent MCP lane.
"""

from __future__ import annotations

import fnmatch
from typing import Any, Optional


_PROTECTED_SERVER_PATTERNS = (
    "web_filip",
    "web_filip_staging",
    "web_studios",
    "web_studios_staging",
    "web_media",
    "web_media_staging",
)

_CREATE_WITH_STATUS_TOOLS = {
    "wp_create_block",
    "wp_create_cpt_item",
    "wp_create_page",
    "wp_create_post",
}

_UPDATE_WITH_STATUS_TOOLS = {
    "wp_update_block",
    "wp_update_cpt_item",
    "wp_update_page",
    "wp_update_post",
}

_OWNER_GATED_TOOLS = {
    "wp_create_menu",
    "wp_create_menu_item",
    "wp_update_global_styles",
    "wp_update_menu",
    "wp_update_menu_item",
    "wp_update_site_settings",
    "wp_update_template",
}

_SAFE_NON_LIVE_STATUSES = {"draft", "pending"}


def _is_protected_server(server_name: str) -> bool:
    return any(fnmatch.fnmatchcase(server_name, pattern) for pattern in _PROTECTED_SERVER_PATTERNS)


def _requested_status(arguments: Any) -> Optional[str]:
    if not isinstance(arguments, dict) or "status" not in arguments:
        return None
    value = arguments.get("status")
    if value is None:
        return None
    return str(value).strip().lower() or None


def check_wordpress_mcp_call(
    server_name: str,
    tool_name: str,
    arguments: Any,
) -> Optional[dict[str, Any]]:
    """Return a safe policy error payload, or ``None`` when the call is allowed."""
    if not _is_protected_server(server_name):
        return None

    if tool_name in _OWNER_GATED_TOOLS:
        return {
            "error": (
                "Blocked by KMR web policy: menus, templates, global styles and "
                "site settings require Filip-go and the supervisor/break-glass lane."
            ),
            "code": "filip_go_required_high_risk_web_change",
            "server": server_name,
            "tool": tool_name,
        }

    status = _requested_status(arguments)
    if tool_name in _CREATE_WITH_STATUS_TOOLS and status not in (None, *_SAFE_NON_LIVE_STATUSES):
        return {
            "error": (
                "Blocked by KMR web policy: newly created content must remain a draft. "
                "Only Filip may approve publication of a new page or primary surface."
            ),
            "code": "filip_go_required_new_surface",
            "server": server_name,
            "tool": tool_name,
            "requested_status": status,
        }

    if tool_name in _UPDATE_WITH_STATUS_TOOLS and status not in (None, *_SAFE_NON_LIVE_STATUSES):
        return {
            "error": (
                "Blocked by KMR web policy: the normal MCP lane cannot promote content "
                "to a live status. For a routine edit to an existing published page, "
                "omit the status field so WordPress preserves its current status. "
                "Publishing a new surface requires Filip-go and break-glass execution."
            ),
            "code": "filip_go_required_status_transition",
            "server": server_name,
            "tool": tool_name,
            "requested_status": status,
        }

    return None
