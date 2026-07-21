"""SCOPE-002 tests: cron job mutation_scope field and runner enforcement."""
from cron.scheduler import _resolve_cron_enabled_toolsets_with_scope
from agent.scope_guard import MutationScope


class TestCronMutationScope:
    """SCOPE-002: cron jobs must declare and enforce mutation scope."""

    def test_create_job_defaults_full_scope(self):
        """Jobs created without mutation_scope default to 'full'."""
        from cron.jobs import create_job
        job = create_job(
            prompt="test prompt",
            schedule="every 1h",
        )
        assert job["mutation_scope"] == "full"

    def test_create_job_accepts_read_only_scope(self):
        """Jobs can be created with read-only scope."""
        from cron.jobs import create_job
        job = create_job(
            prompt="test prompt",
            schedule="every 1h",
            mutation_scope="read-only",
        )
        assert job["mutation_scope"] == "read-only"

    def test_create_job_accepts_exact_write_set_scope(self):
        """Jobs can be created with exact-write-set scope."""
        from cron.jobs import create_job
        job = create_job(
            prompt="test prompt",
            schedule="every 1h",
            mutation_scope="exact-write-set",
        )
        assert job["mutation_scope"] == "exact-write-set"

    def test_create_job_invalid_scope_defaults_full(self):
        """Invalid scope values default to 'full'."""
        from cron.jobs import create_job
        job = create_job(
            prompt="test prompt",
            schedule="every 1h",
            mutation_scope="bogus-value",
        )
        assert job["mutation_scope"] == "full"

    def test_read_only_scope_strips_write_toolsets(self):
        """Runner strips write-capable toolsets for read-only jobs."""
        job = {
            "id": "test123",
            "mutation_scope": "read-only",
            "enabled_toolsets": ["web", "file", "terminal", "skills", "kanban"],
        }
        cfg = {}
        result = _resolve_cron_enabled_toolsets_with_scope(job, cfg)
        assert "file" not in result
        assert "terminal" not in result
        assert "kanban" not in result
        assert "web" in result
        assert "skills" in result

    def test_full_scope_keeps_all_toolsets(self):
        """Full scope does not strip any toolsets."""
        job = {
            "id": "test456",
            "mutation_scope": "full",
            "enabled_toolsets": ["web", "file", "terminal"],
        }
        cfg = {}
        result = _resolve_cron_enabled_toolsets_with_scope(job, cfg)
        assert "file" in result
        assert "terminal" in result

    def test_no_scope_field_defaults_full(self):
        """Old jobs without mutation_scope field default to full (backward compat)."""
        job = {
            "id": "old789",
            # no mutation_scope field
            "enabled_toolsets": ["web", "file", "terminal"],
        }
        cfg = {}
        result = _resolve_cron_enabled_toolsets_with_scope(job, cfg)
        assert "file" in result
        assert "terminal" in result

    def test_normalize_job_record_defaults_full(self):
        """_normalize_job_record adds mutation_scope='full' to old jobs."""
        from cron.jobs import _normalize_job_record
        old_job = {"id": "legacy", "prompt": "test", "name": "test"}
        normalized = _normalize_job_record(old_job)
        assert normalized["mutation_scope"] == "full"
