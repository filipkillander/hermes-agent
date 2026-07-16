"""Conflict-aware dispatch for shared Kanban workspaces."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    (home / "profiles" / "lumi").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    with kb.connect_closing() as conn:
        yield conn, tmp_path


def _create_dir_task(conn, root: Path, title: str, write_set):
    return kb.create_task(
        conn,
        title=title,
        assignee="lumi",
        workspace_kind="dir",
        workspace_path=str(root),
        write_set=write_set,
    )


def test_overlap_is_deferred_while_disjoint_and_read_only_continue(board):
    conn, tmp_path = board
    repo = tmp_path / "repo"
    repo.mkdir()
    active = _create_dir_task(conn, repo, "active editor", ["src/editor"])
    overlap = _create_dir_task(conn, repo, "same surface", ["src/editor/pages"])
    disjoint = _create_dir_task(conn, repo, "tests only", ["tests"])
    read_only = _create_dir_task(conn, repo, "read only", [])

    assert kb.claim_task(conn, active, claimer="test:active") is not None

    result = kb.dispatch_once(
        conn,
        spawn_fn=lambda *_args, **_kwargs: 123,
        dry_run=True,
    )

    spawned_ids = {task_id for task_id, _assignee, _workspace in result.spawned}
    assert overlap not in spawned_ids
    assert disjoint in spawned_ids
    assert read_only in spawned_ids
    assert any(
        task_id == overlap and blocker == active
        for task_id, blocker, _reason in result.workspace_conflicted
    )
    assert kb.get_task(conn, overlap).status == "ready"


def test_deferred_task_runs_on_next_tick_after_conflict_clears(board):
    conn, tmp_path = board
    repo = tmp_path / "repo"
    repo.mkdir()
    active = _create_dir_task(conn, repo, "active", ["src"])
    waiting = _create_dir_task(conn, repo, "waiting", ["src/app.py"])
    assert kb.claim_task(conn, active, claimer="test:active") is not None

    first = kb.dispatch_once(
        conn, spawn_fn=lambda *_args, **_kwargs: 123, dry_run=True
    )
    assert any(item[0] == waiting for item in first.workspace_conflicted)

    assert kb.complete_task(conn, active, result="done") is True
    second = kb.dispatch_once(
        conn, spawn_fn=lambda *_args, **_kwargs: 123, dry_run=True
    )
    assert waiting in {item[0] for item in second.spawned}
    assert not any(item[0] == waiting for item in second.workspace_conflicted)


def test_legacy_unknown_scope_is_conservative_but_board_stays_live(board):
    conn, tmp_path = board
    repo = tmp_path / "repo"
    other_repo = tmp_path / "other"
    repo.mkdir()
    other_repo.mkdir()
    active = _create_dir_task(conn, repo, "legacy active", None)
    same_repo = _create_dir_task(conn, repo, "same repo", ["docs"])
    other = _create_dir_task(conn, other_repo, "other repo", None)
    assert kb.claim_task(conn, active, claimer="test:active") is not None

    result = kb.dispatch_once(
        conn, spawn_fn=lambda *_args, **_kwargs: 123, dry_run=True
    )
    spawned_ids = {item[0] for item in result.spawned}
    assert same_repo not in spawned_ids
    assert other in spawned_ids


def test_claim_path_enforces_same_conflict_invariant(board):
    conn, tmp_path = board
    repo = tmp_path / "repo"
    repo.mkdir()
    active = _create_dir_task(conn, repo, "active", ["src"])
    waiting = _create_dir_task(conn, repo, "waiting", ["src/file.py"])
    assert kb.claim_task(conn, active, claimer="test:active") is not None

    assert kb.claim_task(conn, waiting, claimer="test:waiting") is None
    assert kb.get_task(conn, waiting).status == "ready"
    rejected = [
        event
        for event in kb.list_events(conn, waiting)
        if event.kind == "claim_rejected"
    ]
    assert rejected[-1].payload["reason"] == "workspace_write_conflict"
    assert rejected[-1].payload["blocking_task_id"] == active


@pytest.mark.parametrize(
    "bad",
    [["/absolute"], ["../escape"], ["src//file.py"], ["src/./file.py"], [""]],
)
def test_write_set_rejects_ambiguous_or_escaping_paths(board, bad):
    conn, tmp_path = board
    with pytest.raises(ValueError, match="write_set"):
        _create_dir_task(conn, tmp_path / "repo", "bad", bad)


def test_migration_column_round_trips_write_set(board):
    conn, tmp_path = board
    task_id = _create_dir_task(conn, tmp_path / "repo", "scoped", ["src", "tests"])
    task = kb.get_task(conn, task_id)
    assert task.write_set == ["src", "tests"]
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
    assert "write_set" in columns


def test_init_migrates_existing_board_without_write_set(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.init_db()
    with kb.connect_closing(db_path) as conn:
        legacy_id = kb.create_task(conn, title="legacy")
        conn.execute("ALTER TABLE tasks DROP COLUMN write_set")
        conn.commit()

    kb.init_db(db_path)

    with kb.connect_closing(db_path) as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
        assert "write_set" in columns
        assert kb.get_task(conn, legacy_id).write_set is None
