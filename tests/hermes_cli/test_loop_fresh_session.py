"""Fail-closed session bootstrap used by the local kmrOS loop controller."""

from argparse import Namespace
from datetime import datetime
import sys
import types

import pytest


class _FakeSessionDB:
    def __init__(self, existing=()):
        self._existing = set(existing)

    def get_session(self, session_id):
        return {"id": session_id} if session_id in self._existing else None


def test_chat_parser_exposes_fresh_session_and_memory_isolation_flags():
    from hermes_cli._parser import build_top_level_parser

    parser, _subparsers, _chat_parser = build_top_level_parser()
    args = parser.parse_args(
        [
            "chat",
            "--session-id",
            "kmros-loop-20260719-0001",
            "--skip-memory",
            "--controller-isolated",
        ]
    )

    assert args.session_id == "kmros-loop-20260719-0001"
    assert args.skip_memory is True
    assert args.controller_isolated is True


def test_explicit_session_id_is_fresh_and_resume_is_rejected():
    from cli import _resolve_initial_session_id

    session_id, resumed = _resolve_initial_session_id(
        resume=None,
        session_id="kmros-loop-20260719-0001",
        session_db=_FakeSessionDB(),
        session_start=datetime(2026, 7, 19, 12, 0, 0),
    )
    assert session_id == "kmros-loop-20260719-0001"
    assert resumed is False

    with pytest.raises(ValueError, match="cannot be combined"):
        _resolve_initial_session_id(
            resume="old-session",
            session_id="kmros-loop-20260719-0001",
            session_db=_FakeSessionDB(),
            session_start=datetime(2026, 7, 19, 12, 0, 0),
        )


def test_explicit_session_id_rejects_existing_or_unverifiable_state():
    from cli import _resolve_initial_session_id

    kwargs = {
        "resume": None,
        "session_id": "kmros-loop-20260719-0001",
        "session_start": datetime(2026, 7, 19, 12, 0, 0),
    }
    with pytest.raises(ValueError, match="already exists"):
        _resolve_initial_session_id(
            session_db=_FakeSessionDB({"kmros-loop-20260719-0001"}), **kwargs
        )
    with pytest.raises(ValueError, match="freshness cannot be verified"):
        _resolve_initial_session_id(session_db=None, **kwargs)


def test_cmd_chat_forwards_independent_memory_skip(monkeypatch):
    import hermes_cli.main as main_mod
    from hermes_cli._parser import build_top_level_parser

    parser, _subparsers, chat_parser = build_top_level_parser()
    chat_parser.set_defaults(func=main_mod.cmd_chat)
    args = parser.parse_args(
        [
            "chat",
            "--cli",
            "-q",
            "controller prompt",
            "--session-id",
            "kmros-loop-20260719-0001",
            "--skip-memory",
            "--controller-isolated",
            "-t",
            "file",
            "-s",
            "kmros-loop-operator",
        ]
    )
    captured = {}
    fake_cli = types.ModuleType("cli")
    fake_cli.main = lambda **kwargs: captured.update(kwargs)

    monkeypatch.setitem(sys.modules, "cli", fake_cli)
    monkeypatch.setattr(main_mod, "_has_any_provider_configured", lambda: True)
    monkeypatch.setattr(
        main_mod,
        "_termux_should_prefetch_update_check",
        lambda: pytest.fail("isolated controller must not prefetch updates"),
    )
    monkeypatch.setattr(
        main_mod,
        "_sync_bundled_skills_for_startup",
        lambda: pytest.fail("isolated controller must not sync skills"),
    )
    monkeypatch.setattr(main_mod, "_pin_kanban_board_env", lambda: None)

    main_mod.cmd_chat(args)

    assert captured["session_id"] == "kmros-loop-20260719-0001"
    assert captured["skip_memory"] is True
    assert captured["ignore_rules"] is False
    assert captured["controller_isolated"] is True
    assert main_mod.os.environ["HERMES_CONTROLLER_ISOLATED"] == "1"


@pytest.mark.parametrize(
    "argv",
    [
        ["chat", "--controller-isolated"],
        [
            "chat", "--cli", "-q", "prompt", "--session-id",
            "kmros-loop-20260719-0001", "--skip-memory", "--controller-isolated",
            "-s", "kmros-loop-operator",
        ],
        [
            "chat", "--cli", "-q", "prompt", "--session-id",
            "kmros-loop-20260719-0001", "--skip-memory", "--controller-isolated",
            "-t", "file",
        ],
    ],
)
def test_controller_isolated_invocation_fails_closed(argv, monkeypatch):
    import hermes_cli.main as main_mod
    from hermes_cli._parser import build_top_level_parser

    parser, _subparsers, chat_parser = build_top_level_parser()
    chat_parser.set_defaults(func=main_mod.cmd_chat)
    args = parser.parse_args(argv)
    monkeypatch.setattr(main_mod, "_has_any_provider_configured", lambda: True)

    with pytest.raises(SystemExit) as exc:
        main_mod.cmd_chat(args)
    assert exc.value.code == 2


def test_prepare_agent_startup_is_noop_for_controller_isolation(monkeypatch):
    import hermes_cli.main as main_mod

    monkeypatch.setattr(
        main_mod,
        "_apply_safe_mode",
        lambda _args: None,
    )
    main_mod._prepare_agent_startup(Namespace(controller_isolated=True))


@pytest.mark.parametrize("continue_value", [True, "existing session"])
def test_cmd_chat_rejects_fresh_id_with_continue(continue_value):
    import hermes_cli.main as main_mod

    args = Namespace(
        cli=True,
        tui=False,
        safe_mode=False,
        session_id="kmros-loop-20260719-0001",
        skip_memory=True,
        resume=None,
        continue_last=continue_value,
    )
    with pytest.raises(SystemExit) as exc:
        main_mod.cmd_chat(args)
    assert exc.value.code == 2


def test_controller_file_scope_is_exact_and_rejects_symlink_escape(tmp_path, monkeypatch):
    from tools.file_tools import _controller_workspace_scope_error

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "src").mkdir()
    (workspace / "escape").symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("TERMINAL_CWD", str(workspace))
    monkeypatch.setenv("HERMES_CONTROLLER_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv(
        "HERMES_CONTROLLER_ALLOWED_WRITE_PATHS",
        '["src/allowed.py"]',
    )

    assert _controller_workspace_scope_error("src/allowed.py", write=True) is None
    assert "exact write set" in _controller_workspace_scope_error(
        "src/extra.py", write=True
    )
    assert "outside the bound workspace" in _controller_workspace_scope_error(
        "escape/leak.txt", write=True
    )
    assert "outside the bound workspace" in _controller_workspace_scope_error(
        str(outside / "secret.txt")
    )


def test_normal_file_sessions_are_unchanged_without_controller_scope(monkeypatch):
    from tools.file_tools import _controller_workspace_scope_error

    monkeypatch.delenv("HERMES_CONTROLLER_WORKSPACE_ROOT", raising=False)
    monkeypatch.delenv("HERMES_CONTROLLER_ALLOWED_WRITE_PATHS", raising=False)
    assert _controller_workspace_scope_error("/any/normal/path", write=True) is None
