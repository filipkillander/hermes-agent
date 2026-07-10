#!/usr/bin/env python3
"""Stable launcher for profile-scoped immutable Hermes releases."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import yaml


_PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_BOARD_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class LaunchRejected(RuntimeError):
    pass


@dataclass(frozen=True)
class LaunchSpec:
    profile: str
    service: str
    release: Path
    release_id: str
    commit: str
    python: Path
    argv: tuple[str, ...]
    env: dict[str, str]


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _private_key_is_valid(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size >= 32 and not (path.stat().st_mode & 0o077)
    except OSError:
        return False


def _registered_default_board(registry: Path, profile: str, profile_home: Path) -> str:
    try:
        document = yaml.safe_load(registry.read_text(encoding="utf-8"))
        entry = document["profiles"][profile]
    except (OSError, yaml.YAMLError, KeyError, TypeError) as exc:
        raise LaunchRejected("runtime registry profile is missing or invalid") from exc
    if document.get("schema_version") != 2 or entry.get("role") != "external_gateway":
        raise LaunchRejected("runtime registry profile is not an external gateway")
    try:
        registered_home = Path(entry["home"]).resolve()
    except (KeyError, TypeError, OSError) as exc:
        raise LaunchRejected("runtime registry profile home is invalid") from exc
    if registered_home != profile_home.resolve():
        raise LaunchRejected("runtime registry profile home mismatch")
    board = entry.get("default_board")
    if not isinstance(board, str) or not _BOARD_RE.fullmatch(board):
        raise LaunchRejected("runtime registry default_board is missing or invalid")
    if board != "default" and not (
        registry.parent / "kanban" / "boards" / board / "board.json"
    ).is_file():
        raise LaunchRejected("runtime registry default_board does not exist")
    return board


def resolve_launch(
    profile: str,
    root: Path,
    inherited: dict[str, str],
    *,
    service: str = "gateway",
    service_args: Sequence[str] = (),
) -> LaunchSpec:
    if not _PROFILE_RE.fullmatch(profile):
        raise LaunchRejected("invalid profile id")
    if service not in {"gateway", "dashboard", "cli"}:
        raise LaunchRejected("invalid service")
    if service == "gateway" and service_args:
        raise LaunchRejected("gateway does not accept launcher passthrough arguments")
    root = root.expanduser().resolve()
    current = root / "runtime-links" / profile / "current"
    if not current.is_symlink():
        raise LaunchRejected("profile has no immutable current release")
    release = current.resolve(strict=True)
    releases_root = (root / "releases").resolve()
    if not _inside(release, releases_root):
        raise LaunchRejected("current release escapes the release root")

    manifest_path = release / ".hermes-release.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LaunchRejected("release manifest is missing or invalid") from exc
    release_id = manifest.get("release_id")
    commit = manifest.get("commit")
    if release_id != release.name or not isinstance(commit, str) or not re.fullmatch(
        r"[0-9a-f]{40}", commit
    ):
        raise LaunchRejected("release manifest identity mismatch")

    registry = root / "runtime-registry.yaml"
    if not registry.is_file():
        raise LaunchRejected("runtime registry is missing")
    fingerprint_key = root / "control-plane" / "bot-fingerprint.key"
    if not _private_key_is_valid(fingerprint_key):
        raise LaunchRejected("bot fingerprint key is missing or insecure")
    profile_home = root / "profiles" / profile
    if not (profile_home / "config.yaml").is_file():
        raise LaunchRejected("profile config is missing")
    default_board = _registered_default_board(registry, profile, profile_home)

    python = release / ".venv" / "bin" / "python"
    if not python.is_file() or not os.access(python, os.X_OK):
        raise LaunchRejected("release Python environment is missing")

    env = dict(inherited)
    inherited_path = env.get("PATH", "/usr/bin:/bin")
    clean_path = [
        entry
        for entry in inherited_path.split(os.pathsep)
        if entry
        and not entry.startswith(str(root / "hermes-agent"))
        and "/hermes-agent/" not in entry
    ]
    release_bins = [str(release / ".venv" / "bin")]
    node_bin = release / "node_modules" / ".bin"
    if node_bin.is_dir():
        release_bins.append(str(node_bin))
    env.update(
        {
            "HERMES_HOME": str(profile_home),
            "HERMES_PROFILE": profile,
            "HERMES_RELEASE_REVISION": commit,
            "HERMES_RUNTIME_REGISTRY": str(registry),
            "HERMES_BOT_FINGERPRINT_KEY_FILE": str(fingerprint_key),
            "HERMES_KANBAN_BOARD": default_board,
            "VIRTUAL_ENV": str(release / ".venv"),
            "PYTHONNOUSERSITE": "1",
            "PATH": os.pathsep.join([*release_bins, *clean_path]),
        }
    )
    env.pop("PYTHONPATH", None)
    if service == "gateway":
        command = ("gateway", "run")
    elif service == "dashboard":
        command = ("dashboard", *service_args)
    else:
        command = tuple(service_args)
    argv = (str(python), "-m", "hermes_cli.main", "--profile", profile, *command)
    return LaunchSpec(profile, service, release, release_id, commit, python, argv, env)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile")
    parser.add_argument("service", nargs="?", choices=("gateway", "dashboard", "cli"), default="gateway")
    parser.add_argument("--root", type=Path, default=None)
    parser.add_argument("--check", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args, service_args = build_parser().parse_known_args(argv)
    root = args.root or Path(os.environ.get("HERMES_ROOT", Path.home() / ".hermes"))
    try:
        spec = resolve_launch(
            args.profile,
            root,
            dict(os.environ),
            service=args.service,
            service_args=service_args,
        )
    except LaunchRejected as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 2
    if args.check:
        print(
            json.dumps(
                {
                    "ok": True,
                    "profile": spec.profile,
                    "service": spec.service,
                    "release_id": spec.release_id,
                    "commit": spec.commit,
                    "python": str(spec.python),
                },
                sort_keys=True,
            )
        )
        return 0
    os.execvpe(str(spec.python), list(spec.argv), spec.env)
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
