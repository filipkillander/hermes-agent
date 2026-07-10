from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient


REPO = Path(__file__).resolve().parents[2]


def _authority_root(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "root"
    profiles = root / "profiles"
    for name in ("lumi", "igor", "spark", "coder", "review"):
        (profiles / name).mkdir(parents=True)
        (profiles / name / "config.yaml").write_text("model: test\n", encoding="utf-8")
    registry = root / "runtime-registry.yaml"
    registry.write_text(
        f"""schema_version: 2
profiles:
  lumi:
    role: external_gateway
    home: {profiles / 'lumi'}
    service_label: ai.hermes.gateway-lumi
    port: 8642
    domain: general
    can_delegate_to: [igor, spark, workers]
    can_create_boards: true
  igor:
    role: external_gateway
    home: {profiles / 'igor'}
    service_label: ai.hermes.gateway-igor
    port: 8644
    domain: general
    can_delegate_to: [spark, workers]
    can_create_boards: false
  spark:
    role: external_gateway
    home: {profiles / 'spark'}
    service_label: ai.hermes.gateway-spark
    port: 8643
    domain: smart_home
    can_delegate_to: []
    can_create_boards: false
  coder:
    role: internal_worker
    home: {profiles / 'coder'}
    domain: worker
    can_delegate_to: []
    can_create_boards: false
  review:
    role: internal_worker
    home: {profiles / 'review'}
    domain: worker
    can_delegate_to: []
    can_create_boards: false
""",
        encoding="utf-8",
    )
    return root, registry


def _env(root: Path, registry: Path, profile: str) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": str(REPO),
            "HERMES_HOME": str(root / "profiles" / profile),
            "HERMES_PROFILE": profile,
            "HERMES_RUNTIME_REGISTRY": str(registry),
        }
    )
    return env


def test_cli_denies_igor_board_create_before_filesystem_write(tmp_path):
    root, registry = _authority_root(tmp_path)
    result = subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "kanban", "boards", "create", "forbidden"],
        cwd=REPO,
        env=_env(root, registry, "igor"),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert "denies board management" in result.stderr
    assert not (root / "kanban" / "boards" / "forbidden").exists()


def test_dashboard_denies_igor_and_allows_lumi_before_board_write(tmp_path, monkeypatch):
    root, registry = _authority_root(tmp_path)
    monkeypatch.setenv("HERMES_RUNTIME_REGISTRY", str(registry))
    monkeypatch.setenv("HERMES_HOME", str(root / "profiles" / "igor"))
    plugin = REPO / "plugins/kanban/dashboard/plugin_api.py"
    spec = importlib.util.spec_from_file_location("authority_plugin_api", plugin)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    app = FastAPI()
    app.include_router(module.router)
    client = TestClient(app)

    denied = client.post("/boards", json={"slug": "forbidden"})
    assert denied.status_code == 403
    assert not (root / "kanban" / "boards" / "forbidden").exists()

    monkeypatch.setenv("HERMES_HOME", str(root / "profiles" / "lumi"))
    allowed = client.post("/boards", json={"slug": "allowed"})
    assert allowed.status_code == 200, allowed.text
    assert (root / "kanban" / "boards" / "allowed" / "board.json").is_file()


def test_tool_denies_igor_to_lumi_before_db_connect(tmp_path, monkeypatch):
    root, registry = _authority_root(tmp_path)
    monkeypatch.setenv("HERMES_RUNTIME_REGISTRY", str(registry))
    monkeypatch.setenv("HERMES_HOME", str(root / "profiles" / "igor"))
    from tools import kanban_tools

    called = False

    def forbidden_connect(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("DB must not be opened")

    monkeypatch.setattr(kanban_tools, "_connect", forbidden_connect)
    result = json.loads(kanban_tools._handle_create({"title": "x", "assignee": "lumi"}))
    assert "denies delegation" in result["error"]
    assert called is False


def test_delegate_task_denies_spark_before_child_construction(tmp_path, monkeypatch):
    root, registry = _authority_root(tmp_path)
    monkeypatch.setenv("HERMES_RUNTIME_REGISTRY", str(registry))
    monkeypatch.setenv("HERMES_HOME", str(root / "profiles" / "spark"))
    from tools import delegate_tool

    monkeypatch.setattr(
        delegate_tool,
        "_build_child_agent",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("child must not be built")),
    )
    result = json.loads(delegate_tool.delegate_task(goal="x", parent_agent=object()))
    assert "denies worker delegation" in result["error"]


def test_dispatcher_denies_igor_to_lumi_before_claim_or_spawn(tmp_path, monkeypatch):
    root, registry = _authority_root(tmp_path)
    monkeypatch.setenv("HERMES_RUNTIME_REGISTRY", str(registry))
    monkeypatch.setenv("HERMES_HOME", str(root / "profiles" / "igor"))
    from hermes_cli import kanban_db as kb

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="forbidden", assignee="lumi")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='ready', claim_lock=NULL, claim_expires=NULL WHERE id=?",
                (task_id,),
            )
        monkeypatch.setattr(kb, "_default_spawn", lambda *a, **k: (_ for _ in ()).throw(AssertionError("spawned")))
        result = kb.dispatch_once(conn)
        task = kb.get_task(conn, task_id)
    assert result.skipped_unauthorized == [(task_id, "lumi")]
    assert task.status == "ready"
    assert task.claim_lock is None
