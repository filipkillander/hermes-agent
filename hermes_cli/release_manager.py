"""Immutable Hermes release staging, promotion, and rollback.

This module deliberately does not know how to restart a gateway.  It owns the
filesystem transaction only; lifecycle is delegated to an identity-aware
probe/coordinator supplied as argv.  No shell is used and the live source
checkout is never modified.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Sequence


_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_FORBIDDEN_NAMES = {
    ".env",
    "auth.json",
    "bws_cache.json",
    "credentials.json",
    "secrets.json",
}
_FORBIDDEN_PREFIXES = ("client_secret_",)
_FORBIDDEN_DIRS = {".git", "backups", "cache", "logs", "sessions"}
_MANIFEST = ".hermes-release.json"
_SNAPSHOT_MANIFEST = "manifest.json"
_DEFAULT_MAX_RELEASE_BYTES = 4 * 1024 * 1024 * 1024
_DEFAULT_MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024


class ReleaseError(RuntimeError):
    """A release operation failed without intentionally mutating live code."""


@dataclass(frozen=True)
class ProbeResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


def _validate_id(value: str, label: str) -> str:
    if not _ID_RE.fullmatch(value):
        raise ReleaseError(f"invalid {label}: {value!r}")
    return value


def _path_parts(path: str | PurePosixPath) -> tuple[str, ...]:
    pure = PurePosixPath(path)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise ReleaseError(f"unsafe archive path: {str(path)!r}")
    return pure.parts


def _forbidden_path(path: str | PurePosixPath) -> bool:
    parts = _path_parts(path)
    if any(part in _FORBIDDEN_DIRS for part in parts[:-1]):
        return True
    name = parts[-1]
    lower = name.lower()
    return lower in _FORBIDDEN_NAMES or any(
        lower.startswith(prefix) for prefix in _FORBIDDEN_PREFIXES
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _atomic_write(path: Path, payload: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(raw_tmp)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _atomic_symlink(link: Path, target: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = link.parent / f".{link.name}.{uuid.uuid4().hex}"
    try:
        tmp.symlink_to(target)
        os.replace(tmp, link)
    finally:
        if tmp.is_symlink():
            tmp.unlink()


def _resolved_link(link: Path) -> Path | None:
    if not link.is_symlink():
        return None
    return link.resolve(strict=False)


def run_probe(
    argv: Sequence[str],
    *,
    timeout: float = 120.0,
    cwd: Path | str | None = None,
) -> ProbeResult:
    """Run a probe without a shell and with bounded output and time."""
    if not argv or any(not isinstance(item, str) or not item for item in argv):
        raise ReleaseError("probe argv must be a non-empty list of strings")
    try:
        completed = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            cwd=str(cwd) if cwd is not None else None,
            env={
                "HOME": os.environ.get("HOME", ""),
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "LANG": os.environ.get("LANG", "C.UTF-8"),
            },
        )
    except subprocess.TimeoutExpired as exc:
        raise ReleaseError(f"probe timed out after {timeout:g}s") from exc
    return ProbeResult(
        argv=tuple(argv),
        returncode=completed.returncode,
        stdout=completed.stdout[-8192:],
        stderr=completed.stderr[-8192:],
    )


class ImmutableReleaseManager:
    def __init__(self, hermes_home: Path | str):
        self.home = Path(hermes_home).expanduser().resolve()
        self.releases = self.home / "releases"
        self.links = self.home / "runtime-links"
        self.snapshots = self.home / "release-snapshots"
        self.locks = self.home / "release-locks"
        for directory in (self.releases, self.links, self.snapshots, self.locks):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            directory.chmod(0o700)

    def release_path(self, release_id: str) -> Path:
        return self.releases / _validate_id(release_id, "release id")

    def profile_dir(self, profile: str) -> Path:
        return self.links / _validate_id(profile, "profile")

    @contextmanager
    def profile_lock(self, profile: str) -> Iterator[None]:
        profile = _validate_id(profile, "profile")
        lock_path = self.locks / f"{profile}.lock"
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _git(self, source: Path, *args: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(source), *args],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode:
            detail = completed.stderr.strip().splitlines()
            raise ReleaseError(detail[0] if detail else "git command failed")
        return completed.stdout.strip()

    def _safe_extract(self, archive: Path, destination: Path) -> None:
        with tarfile.open(archive, "r:") as bundle:
            members = bundle.getmembers()
            for member in members:
                parts = _path_parts(member.name)
                if _forbidden_path(PurePosixPath(*parts)):
                    raise ReleaseError(f"forbidden release entry: {member.name}")
                if member.isdev() or member.isfifo() or member.islnk():
                    raise ReleaseError(f"unsupported archive entry: {member.name}")
                if member.issym():
                    target = PurePosixPath(member.linkname)
                    if target.is_absolute():
                        raise ReleaseError(f"absolute symlink in release: {member.name}")
                    combined = PurePosixPath(*parts[:-1], target)
                    depth = 0
                    for part in combined.parts:
                        depth += -1 if part == ".." else (0 if part in {"", "."} else 1)
                        if depth < 0:
                            raise ReleaseError(f"escaping symlink in release: {member.name}")
            bundle.extractall(destination, members=members, filter="data")

    def _tree_manifest(self, root: Path) -> dict[str, dict[str, Any]]:
        files: dict[str, dict[str, Any]] = {}
        for path in sorted(root.rglob("*")):
            rel = path.relative_to(root).as_posix()
            if rel == _MANIFEST:
                continue
            if path.is_symlink():
                files[rel] = {"type": "symlink", "target": os.readlink(path)}
            elif path.is_file():
                files[rel] = {
                    "type": "file",
                    "sha256": _sha256_file(path),
                    "size": path.stat().st_size,
                }
        return files

    def stage(
        self,
        source: Path | str,
        release_id: str,
        *,
        ref: str = "HEAD",
        build: Sequence[str] | None = None,
        build_timeout: float = 1800.0,
        max_bytes: int = _DEFAULT_MAX_RELEASE_BYTES,
    ) -> Path:
        source_path = Path(source).expanduser().resolve()
        destination = self.release_path(release_id)
        if destination.exists() or destination.is_symlink():
            raise ReleaseError(f"release already exists: {release_id}")
        commit = self._git(source_path, "rev-parse", "--verify", f"{ref}^{{commit}}")
        tracked = self._git(source_path, "ls-tree", "-r", "--name-only", commit).splitlines()
        forbidden = [item for item in tracked if item and _forbidden_path(item)]
        if forbidden:
            raise ReleaseError(f"tracked forbidden entry: {forbidden[0]}")

        staging = self.releases / f".staging-{release_id}-{uuid.uuid4().hex}"
        staging.mkdir(mode=0o700)
        archive = self.releases / f".archive-{uuid.uuid4().hex}.tar"
        try:
            completed = subprocess.run(
                [
                    "git",
                    "-C",
                    str(source_path),
                    "archive",
                    "--format=tar",
                    f"--output={archive}",
                    commit,
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode:
                raise ReleaseError(completed.stderr.strip() or "git archive failed")
            archive.chmod(0o600)
            self._safe_extract(archive, staging)
            if build:
                result = run_probe(build, timeout=build_timeout, cwd=staging)
                if result.returncode:
                    detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
                    raise ReleaseError(f"release build failed: {detail.splitlines()[0]}")
            tree = self._tree_manifest(staging)
            total_bytes = sum(
                int(record.get("size", 0)) for record in tree.values()
            )
            if total_bytes > max_bytes:
                raise ReleaseError(
                    f"release exceeds size budget: {total_bytes} > {max_bytes} bytes"
                )
            manifest = {
                "schema": 1,
                "release_id": release_id,
                "commit": commit,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "size_bytes": total_bytes,
                "files": tree,
            }
            _atomic_write(staging / _MANIFEST, _json_bytes(manifest), 0o600)

            for path in sorted(staging.rglob("*"), reverse=True):
                if path.is_symlink():
                    continue
                if path.is_dir():
                    path.chmod(0o555)
                elif path.is_file():
                    executable = bool(path.stat().st_mode & stat.S_IXUSR)
                    path.chmod(0o555 if executable else 0o444)
            staging.chmod(0o555)
            os.replace(staging, destination)
        finally:
            if archive.exists():
                archive.unlink()
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
        self.verify(release_id)
        return destination

    def verify(self, release_id: str) -> dict[str, Any]:
        root = self.release_path(release_id)
        manifest_path = root / _MANIFEST
        if not root.is_dir() or not manifest_path.is_file():
            raise ReleaseError(f"release is incomplete: {release_id}")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ReleaseError(f"invalid release manifest: {release_id}") from exc
        if manifest.get("release_id") != release_id or manifest.get("schema") != 1:
            raise ReleaseError(f"release manifest identity mismatch: {release_id}")
        actual = self._tree_manifest(root)
        if actual != manifest.get("files"):
            raise ReleaseError(f"release checksum mismatch: {release_id}")
        for rel in actual:
            if _forbidden_path(rel):
                raise ReleaseError(f"forbidden entry in release: {rel}")
        return manifest

    def snapshot(
        self,
        snapshot_id: str,
        includes: Sequence[Path | str],
        *,
        max_bytes: int = _DEFAULT_MAX_SNAPSHOT_BYTES,
    ) -> Path:
        snapshot_id = _validate_id(snapshot_id, "snapshot id")
        destination = self.snapshots / snapshot_id
        if destination.exists():
            raise ReleaseError(f"snapshot already exists: {snapshot_id}")
        staging = self.snapshots / f".staging-{snapshot_id}-{uuid.uuid4().hex}"
        staging.mkdir(mode=0o700)
        records: dict[str, dict[str, Any]] = {}
        try:
            for raw in includes:
                source = Path(raw).expanduser().resolve()
                try:
                    relative = source.relative_to(self.home)
                except ValueError as exc:
                    raise ReleaseError(f"snapshot path outside Hermes home: {source}") from exc
                rel = relative.as_posix()
                if _forbidden_path(rel):
                    raise ReleaseError(f"forbidden snapshot entry: {rel}")
                if not source.is_file() or source.is_symlink():
                    raise ReleaseError(f"snapshot entry must be a regular file: {source}")
                target = staging / relative
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                shutil.copyfile(source, target)
                target.chmod(0o600)
                records[rel] = {
                    "sha256": _sha256_file(target),
                    "size": target.stat().st_size,
                }
            total_bytes = sum(int(record["size"]) for record in records.values())
            if total_bytes > max_bytes:
                raise ReleaseError(
                    f"snapshot exceeds size budget: {total_bytes} > {max_bytes} bytes"
                )
            manifest = {
                "schema": 1,
                "snapshot_id": snapshot_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "size_bytes": total_bytes,
                "files": records,
            }
            _atomic_write(staging / _SNAPSHOT_MANIFEST, _json_bytes(manifest), 0o600)
            os.replace(staging, destination)
            destination.chmod(0o700)
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
        return destination

    def _require_release_target(self, target: Path) -> Path:
        target = target.resolve(strict=False)
        try:
            target.relative_to(self.releases)
        except ValueError as exc:
            raise ReleaseError(f"runtime link escapes release root: {target}") from exc
        if not target.is_dir():
            raise ReleaseError(f"runtime target does not exist: {target}")
        self.verify(target.name)
        return target

    def _run_required_probe(self, argv: Sequence[str] | None, label: str) -> ProbeResult | None:
        if not argv:
            return None
        result = run_probe(argv)
        if result.returncode:
            detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
            raise ReleaseError(f"{label} failed: {detail.splitlines()[0]}")
        return result

    def promote(
        self,
        profile: str,
        release_id: str,
        *,
        preflight: Sequence[str] | None = None,
        postflight: Sequence[str] | None = None,
    ) -> dict[str, str | None]:
        target = self._require_release_target(self.release_path(release_id))
        with self.profile_lock(profile):
            self._run_required_probe(preflight, "preflight")
            profile_dir = self.profile_dir(profile)
            profile_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            current_link = profile_dir / "current"
            previous_link = profile_dir / "previous"
            old = _resolved_link(current_link)
            if old == target:
                self._run_required_probe(postflight, "postflight")
                return {"current": str(target), "previous": str(_resolved_link(previous_link)) if previous_link.is_symlink() else None}
            if old is not None:
                self._require_release_target(old)
                _atomic_symlink(previous_link, old)
            _atomic_symlink(current_link, target)
            try:
                self._run_required_probe(postflight, "postflight")
            except Exception:
                if old is not None:
                    _atomic_symlink(current_link, old)
                elif current_link.is_symlink():
                    current_link.unlink()
                raise
            return {"current": str(target), "previous": str(old) if old else None}

    def rollback(
        self,
        profile: str,
        *,
        postflight: Sequence[str] | None = None,
    ) -> dict[str, str]:
        with self.profile_lock(profile):
            profile_dir = self.profile_dir(profile)
            current_link = profile_dir / "current"
            previous_link = profile_dir / "previous"
            current = _resolved_link(current_link)
            previous = _resolved_link(previous_link)
            if current is None or previous is None:
                raise ReleaseError(f"profile {profile!r} has no rollback pair")
            current = self._require_release_target(current)
            previous = self._require_release_target(previous)
            _atomic_symlink(current_link, previous)
            _atomic_symlink(previous_link, current)
            try:
                self._run_required_probe(postflight, "rollback postflight")
            except Exception:
                _atomic_symlink(current_link, current)
                _atomic_symlink(previous_link, previous)
                raise
            return {"current": str(previous), "previous": str(current)}

    def protected_releases(self) -> set[Path]:
        protected: set[Path] = set()
        for profile_dir in self.links.iterdir():
            if not profile_dir.is_dir():
                continue
            for name in ("current", "previous"):
                target = _resolved_link(profile_dir / name)
                if target is None:
                    continue
                try:
                    target.relative_to(self.releases)
                except ValueError:
                    continue
                protected.add(target)
        return protected

    def prune(self, *, keep: int = 2, apply: bool = False) -> list[str]:
        """Plan or apply retention without ever deleting linked releases."""
        if keep < 2:
            raise ReleaseError("release retention must keep at least two releases")
        protected = self.protected_releases()
        releases = sorted(
            (
                path
                for path in self.releases.iterdir()
                if path.is_dir() and not path.name.startswith(".staging-")
            ),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
        retained_unlinked = 0
        candidates: list[Path] = []
        for path in releases:
            if path in protected:
                continue
            if retained_unlinked < keep:
                retained_unlinked += 1
                continue
            candidates.append(path)
        if apply:
            for path in candidates:
                for child in path.rglob("*"):
                    if not child.is_symlink():
                        child.chmod(0o700 if child.is_dir() else 0o600)
                path.chmod(0o700)
                shutil.rmtree(path)
        return [path.name for path in candidates]


def _parse_probe(value: str | None) -> list[str] | None:
    if value is None:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ReleaseError("probe must be a JSON argv array") from exc
    if not isinstance(parsed, list) or not parsed or not all(
        isinstance(item, str) and item for item in parsed
    ):
        raise ReleaseError("probe must be a non-empty JSON argv array")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", default=os.environ.get("HERMES_HOME", "~/.hermes"))
    sub = parser.add_subparsers(dest="command", required=True)

    stage = sub.add_parser("stage")
    stage.add_argument("release_id")
    stage.add_argument("--source", required=True)
    stage.add_argument("--ref", default="HEAD")
    stage.add_argument("--build-json")
    stage.add_argument("--build-timeout", type=float, default=1800.0)
    stage.add_argument("--max-bytes", type=int, default=_DEFAULT_MAX_RELEASE_BYTES)

    verify = sub.add_parser("verify")
    verify.add_argument("release_id")

    snapshot = sub.add_parser("snapshot")
    snapshot.add_argument("snapshot_id")
    snapshot.add_argument("--include", action="append", required=True)
    snapshot.add_argument("--max-bytes", type=int, default=_DEFAULT_MAX_SNAPSHOT_BYTES)

    promote = sub.add_parser("promote")
    promote.add_argument("profile")
    promote.add_argument("release_id")
    promote.add_argument("--preflight-json")
    promote.add_argument("--postflight-json")

    rollback = sub.add_parser("rollback")
    rollback.add_argument("profile")
    rollback.add_argument("--postflight-json")

    prune = sub.add_parser("prune")
    prune.add_argument("--keep", type=int, default=2)
    prune.add_argument("--apply", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    manager = ImmutableReleaseManager(args.home)
    try:
        if args.command == "stage":
            result: Any = manager.stage(
                args.source,
                args.release_id,
                ref=args.ref,
                build=_parse_probe(args.build_json),
                build_timeout=args.build_timeout,
                max_bytes=args.max_bytes,
            )
        elif args.command == "verify":
            result = manager.verify(args.release_id)
        elif args.command == "snapshot":
            result = manager.snapshot(
                args.snapshot_id,
                args.include,
                max_bytes=args.max_bytes,
            )
        elif args.command == "promote":
            result = manager.promote(
                args.profile,
                args.release_id,
                preflight=_parse_probe(args.preflight_json),
                postflight=_parse_probe(args.postflight_json),
            )
        elif args.command == "rollback":
            result = manager.rollback(
                args.profile,
                postflight=_parse_probe(args.postflight_json),
            )
        else:
            result = manager.prune(keep=args.keep, apply=args.apply)
    except ReleaseError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
    print(json.dumps({"ok": True, "result": str(result) if isinstance(result, Path) else result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
