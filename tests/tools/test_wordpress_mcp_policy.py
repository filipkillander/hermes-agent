import json

import pytest

from tools.wordpress_mcp_policy import check_wordpress_mcp_call


@pytest.mark.parametrize(
    "server",
    ["web_filip_staging", "web_studios_staging", "web_media_staging", "web_media"],
)
def test_protected_kmr_servers_block_new_surface_publication(server):
    blocked = check_wordpress_mcp_call(server, "wp_create_page", {"title": "Ny", "status": "publish"})

    assert blocked is not None
    assert blocked["code"] == "filip_go_required_new_surface"
    assert blocked["requested_status"] == "publish"


def test_new_page_without_status_stays_available_for_force_draft_lane():
    assert check_wordpress_mcp_call(
        "web_media_staging", "wp_create_page", {"title": "Ny"}
    ) is None


@pytest.mark.parametrize("status", [None, "", "draft", "pending"])
def test_non_live_statuses_are_allowed(status):
    args = {"page_id": 123, "content": "Utkast"}
    if status is not None:
        args["status"] = status

    assert check_wordpress_mcp_call("web_filip_staging", "wp_update_page", args) is None


@pytest.mark.parametrize("status", ["publish", "future", "private", "PUBLISH"])
def test_update_cannot_transition_content_to_live_status(status):
    blocked = check_wordpress_mcp_call(
        "web_studios_staging",
        "wp_update_page",
        {"page_id": 123, "content": "Text", "status": status},
    )

    assert blocked is not None
    assert blocked["code"] == "filip_go_required_status_transition"


def test_existing_published_page_can_be_updated_without_status_field():
    assert check_wordpress_mcp_call(
        "web_media_staging",
        "wp_update_page",
        {"page_id": 123, "content": "Korrigerad text"},
    ) is None


@pytest.mark.parametrize(
    "tool",
    ["wp_create_menu", "wp_create_menu_item", "wp_update_menu", "wp_update_template"],
)
def test_high_risk_web_tools_require_owner_lane(tool):
    blocked = check_wordpress_mcp_call("web_media_staging", tool, {})

    assert blocked is not None
    assert blocked["code"] == "filip_go_required_high_risk_web_change"


def test_unrelated_mcp_server_is_untouched():
    assert check_wordpress_mcp_call(
        "client_crm", "wp_update_page", {"status": "publish"}
    ) is None


def test_mcp_handler_enforces_policy_before_connection(monkeypatch):
    from tools import mcp_tool

    monkeypatch.setattr(mcp_tool, "_servers", {})
    handler = mcp_tool._make_tool_handler("web_media_staging", "wp_update_page", 5)

    result = json.loads(handler({"page_id": 123, "status": "publish"}))

    assert result["code"] == "filip_go_required_status_transition"
    assert result["server"] == "web_media_staging"
