"""SCOPE-001 integration test: _get_platform_tools filters webhook toolsets."""
from hermes_cli.tools_config import _get_platform_tools


class TestWebhookPlatformToolsetFiltering:
    """SCOPE-001: _get_platform_tools strips forbidden toolsets for webhook."""

    def test_webhook_strips_forbidden_toolsets(self):
        """Explicit webhook config with forbidden toolsets gets filtered."""
        config = {
            "platform_toolsets": {
                "webhook": ["web", "file", "terminal", "kanban", "memory", "cronjob", "skills"],
            }
        }
        result = _get_platform_tools(config, "webhook")
        assert "file" not in result
        assert "terminal" not in result
        assert "kanban" not in result
        assert "memory" not in result
        assert "cronjob" not in result
        assert "web" in result
        assert "skills" in result

    def test_non_webhook_platform_unaffected(self):
        """CLI platform keeps its toolsets (file, terminal, etc.)."""
        config = {
            "platform_toolsets": {
                "cli": ["web", "file", "terminal", "skills"],
            },
        }
        result = _get_platform_tools(config, "cli")
        assert "file" in result
        assert "terminal" in result

    def test_webhook_default_toolset_still_filters(self):
        """When webhook has no explicit config, the default hermes-webhook
        composite still gets forbidden toolsets stripped."""
        config = {}
        result = _get_platform_tools(config, "webhook")
        assert "file" not in result
        assert "terminal" not in result
        assert "cronjob" not in result
