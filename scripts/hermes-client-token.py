#!/usr/bin/env python3
"""Issue and revoke hash-only kmrOS API client keys without leaking them to logs."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path


SURFACES = {"chrome_extension", "raycast_extension"}
AGENTS = ("lumi", "igor", "spark")
DEFAULT_SCOPES = ["status", "chat", "sessions", "capture"]


def slug(value: str) -> str:
    clean = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    if not clean:
        raise SystemExit("principal/device must contain letters or digits")
    return clean


def registry_path(agent: str) -> Path:
    return Path(f"/Users/ai/.hermes/profiles/{agent}/client-keys.json")


def load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {"schema_version": 1, "keys": []}
    if data.get("schema_version") != 1 or not isinstance(data.get("keys"), list):
        raise SystemExit(f"invalid client-key registry: {path}")
    return data


def save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def keychain_store(key_id: str, token: str) -> None:
    subprocess.run([
        "/usr/bin/security", "add-generic-password", "-U",
        "-s", "kmros-api-client-token", "-a", key_id, "-w", token,
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def keychain_delete(key_id: str) -> None:
    subprocess.run([
        "/usr/bin/security", "delete-generic-password",
        "-s", "kmros-api-client-token", "-a", key_id,
    ], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def clipboard(token: str) -> None:
    subprocess.run(["/usr/bin/pbcopy"], input=token.encode(), check=True)


def issue(args: argparse.Namespace) -> None:
    path = registry_path(args.agent)
    data = load(path)
    principal = slug(args.principal)
    device = slug(args.device)
    surface = args.surface
    key_id = f"{args.agent}-{principal}-{device}-{surface.replace('_extension', '')}"
    if any(item.get("key_id") == key_id and not item.get("revoked") for item in data["keys"]):
        raise SystemExit(f"active key already exists: {key_id}; revoke it first")
    token = "kmr_" + secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    entry = {
        "key_id": key_id,
        "principal": principal,
        "device": device,
        "agent": args.agent,
        "surfaces": [surface],
        "scopes": DEFAULT_SCOPES,
        "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
        "created_at": now.isoformat().replace("+00:00", "Z"),
        "expires_at": (now + timedelta(days=args.days)).isoformat().replace("+00:00", "Z"),
        "revoked": False,
    }
    data["keys"].append(entry)
    save(path, data)
    keychain_store(key_id, token)
    if args.copy:
        clipboard(token)
    print(json.dumps({"ok": True, "key_id": key_id, "copied": bool(args.copy), "registry": str(path)}))


def revoke(args: argparse.Namespace) -> None:
    path = registry_path(args.agent)
    data = load(path)
    changed = False
    for item in data["keys"]:
        if item.get("key_id") == args.key_id and not item.get("revoked"):
            item["revoked"] = True
            item["revoked_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            changed = True
    if not changed:
        raise SystemExit(f"active key not found: {args.key_id}")
    save(path, data)
    keychain_delete(args.key_id)
    print(json.dumps({"ok": True, "revoked": args.key_id}))


def list_keys(args: argparse.Namespace) -> None:
    data = load(registry_path(args.agent))
    safe = [{k: item.get(k) for k in ("key_id", "principal", "device", "agent", "surfaces", "scopes", "expires_at", "revoked")}
            for item in data["keys"]]
    print(json.dumps({"keys": safe}, ensure_ascii=False, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("issue")
    create.add_argument("--agent", choices=AGENTS, required=True)
    create.add_argument("--principal", required=True)
    create.add_argument("--device", required=True)
    create.add_argument("--surface", choices=sorted(SURFACES), required=True)
    create.add_argument("--days", type=int, default=365)
    create.add_argument("--copy", action="store_true")
    create.set_defaults(func=issue)
    remove = sub.add_parser("revoke")
    remove.add_argument("--agent", choices=AGENTS, required=True)
    remove.add_argument("--key-id", required=True)
    remove.set_defaults(func=revoke)
    show = sub.add_parser("list")
    show.add_argument("--agent", choices=AGENTS, required=True)
    show.set_defaults(func=list_keys)
    args = parser.parse_args()
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
