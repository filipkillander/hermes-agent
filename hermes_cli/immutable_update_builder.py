"""Fail-closed build phase for immutable Hermes updates.

This command is intentionally incapable of promotion, rollback, restart, or
scheduling.  It validates a pinned, clean source commit, runs the fixed
focused test harness, and stages a sealed release with messaging dependencies.
Promotion remains a separate human-approved canary operation.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Sequence

from hermes_cli.release_manager import ImmutableReleaseManager, ReleaseError


_COMMIT_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_DEFAULT_MAX_BYTES = 4 * 1024 * 1024 * 1024
_FOCUS_TIMEOUT = 1800.0
_BUILD_TIMEOUT = 1800.0
_STATUS_SCHEMA = 1
_HARNESS_SCHEMA = 1

# This is the official local safety gate for the self-healing overlay.  It is
# deliberately fixed in code: a scheduled caller cannot weaken it with flags.
FOCUS_TESTS: tuple[str, ...] = (
    "tests/hermes_cli/test_immutable_update_builder.py",
    "tests/hermes_cli/test_release_manager.py",
    "tests/hermes_cli/test_control_plane_g1.py",
    "tests/test_bitwarden_secrets.py",
    "tests/test_env_loader_secret_sources.py",
    "tests/test_secret_diagnostic_no_side_effects.py",
    "tests/gateway/test_delivery_envelope.py",
    "tests/gateway/test_delivery_envelope_adapters.py",
    "tests/gateway/test_multiplex_adapter_registry.py",
    "tests/gateway/test_telegram_format.py",
    "tests/gateway/test_telegram_rich_messages.py",
)

_IMPORT_PROBE = r"""
import importlib
import json
import pathlib
import sys

root = pathlib.Path(sys.prefix).resolve()
for name in (
    "hermes_cli.release_manager",
    "gateway.delivery_envelope",
    "telegram",
    "discord",
    "aiohttp",
):
    module = importlib.import_module(name)
    origin = pathlib.Path(module.__file__).resolve()
    origin.relative_to(root)

for path in root.glob("lib/python*/site-packages/**/*.dist-info/direct_url.json"):
    record = json.loads(path.read_text(encoding="utf-8"))
    if record.get("dir_info", {}).get("editable") is True:
        raise SystemExit(20)

for path in root.glob("lib/python*/site-packages/*.pth"):
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("import "):
            continue
        candidate = pathlib.Path(line)
        if candidate.is_absolute():
            candidate.resolve().relative_to(root)

hermes_cli = importlib.import_module("hermes_cli")
dashboard = pathlib.Path(hermes_cli.__file__).resolve().parent / "web_dist" / "index.html"
if not dashboard.is_file() or dashboard.stat().st_size == 0:
    raise SystemExit(21)
"""


def _relocate_venv_shebangs(staging: Path, final_release: Path) -> int:
    """Rewrite uv-generated absolute staging shebangs before sealing."""
    staging = staging.resolve()
    final_release = final_release.resolve()
    if final_release.parent != staging.parent or final_release.name.startswith(".staging-"):
        raise UpdateBuildError("invalid_final_release_path", "stage_build")
    prefix = ("#!" + str(staging)).encode("utf-8")
    replacement = ("#!" + str(final_release)).encode("utf-8")
    changed = 0
    bin_dir = staging / ".venv" / "bin"
    for path in sorted(bin_dir.iterdir()):
        if path.is_symlink() or not path.is_file():
            continue
        payload = path.read_bytes()
        if not payload.startswith(prefix):
            continue
        newline = payload.find(b"\n")
        if newline < 0:
            raise UpdateBuildError("invalid_venv_shebang", "stage_build")
        path.write_bytes(replacement + payload[len(prefix):newline] + payload[newline:])
        changed += 1
    return changed


class UpdateBuildError(RuntimeError):
    """A sanitized build failure whose message never contains command output."""

    def __init__(
        self,
        code: str,
        phase: str,
        *,
        output_sha256: str | None = None,
        output_bytes: int = 0,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.phase = phase
        self.output_sha256 = output_sha256
        self.output_bytes = output_bytes


@dataclass(frozen=True)
class CommandDigest:
    returncode: int
    output_sha256: str
    output_bytes: int


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_env(home: Path) -> dict[str, str]:
    return {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TZ": "UTC",
        "PYTHONHASHSEED": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def _run_digest(
    argv: Sequence[str],
    *,
    cwd: Path,
    home: Path,
    timeout: float,
) -> CommandDigest:
    """Run without a shell or inherited credentials; retain only output digest/size."""
    if not argv or any(not isinstance(item, str) or not item for item in argv):
        raise UpdateBuildError("invalid_argv", "command")
    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen(
            list(argv),
            cwd=cwd,
            env=_safe_env(home),
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            output.seek(0)
            payload = output.read()
            raise UpdateBuildError(
                "command_timeout",
                "command",
                output_sha256=_sha256_bytes(payload),
                output_bytes=len(payload),
            ) from exc
        output.seek(0)
        payload = output.read()
    return CommandDigest(returncode, _sha256_bytes(payload), len(payload))


def _atomic_status(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    payload = (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(raw)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _git(source: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(source), *args],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"},
    )
    if result.returncode:
        payload = (result.stdout + result.stderr).encode("utf-8", errors="replace")
        raise UpdateBuildError(
            "git_validation_failed",
            "source",
            output_sha256=_sha256_bytes(payload),
            output_bytes=len(payload),
        )
    return result.stdout.strip()


def _harness_sha256() -> str:
    payload = json.dumps(
        {"schema": _HARNESS_SCHEMA, "tests": FOCUS_TESTS},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256_bytes(payload)


class ImmutableUpdateBuilder:
    def __init__(self, hermes_home: Path | str) -> None:
        self.manager = ImmutableReleaseManager(hermes_home)
        self.home = self.manager.home
        self.status_path = self.home / "release-status" / "immutable-update-builder.json"
        self.lock_path = self.manager.locks / "immutable-update-builder.lock"

    @contextmanager
    def lock(self) -> Iterator[None]:
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.fchmod(fd, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise UpdateBuildError("builder_busy", "lock") from exc
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def validate_source(self, source: Path, ref: str, expected_commit: str) -> str:
        source = source.expanduser().resolve()
        expected_commit = expected_commit.lower()
        if not _COMMIT_RE.fullmatch(expected_commit):
            raise UpdateBuildError("invalid_expected_commit", "source")
        if not ref or ref.startswith("-") or len(ref) > 1024 or any(char.isspace() for char in ref):
            raise UpdateBuildError("invalid_source_ref", "source")
        if Path(_git(source, "rev-parse", "--show-toplevel")).resolve() != source:
            raise UpdateBuildError("source_not_repo_root", "source")
        if _git(source, "status", "--porcelain=v1", "--untracked-files=all"):
            raise UpdateBuildError("source_not_clean", "source")
        resolved = _git(
            source,
            "rev-parse",
            "--verify",
            "--end-of-options",
            f"{ref}^{{commit}}",
        ).lower()
        head = _git(source, "rev-parse", "--verify", "HEAD^{commit}").lower()
        if resolved != expected_commit or head != expected_commit:
            raise UpdateBuildError("source_ref_mismatch", "source")
        if _git(source, "cat-file", "-t", expected_commit) != "commit":
            raise UpdateBuildError("source_ref_not_commit", "source")
        return expected_commit

    def compare_current(self, profiles: Sequence[str], candidate: str) -> dict[str, Any]:
        if not profiles or len(set(profiles)) != len(profiles):
            raise UpdateBuildError("invalid_profiles", "current")
        commits: set[str] = set()
        matching = 0
        missing = 0
        for profile in profiles:
            current = self.manager.profile_dir(profile) / "current"
            if not current.exists() and not current.is_symlink():
                missing += 1
                continue
            if not current.is_symlink():
                raise UpdateBuildError("invalid_current_pointer", "current")
            target = current.resolve(strict=False)
            try:
                target.relative_to(self.manager.releases)
            except ValueError as exc:
                raise UpdateBuildError("current_pointer_escape", "current") from exc
            manifest = self.manager.verify(target.name)
            commit = str(manifest.get("commit", "")).lower()
            if not _COMMIT_RE.fullmatch(commit):
                raise UpdateBuildError("invalid_current_manifest", "current")
            commits.add(commit)
            matching += int(commit == candidate)
        return {
            "profile_count": len(profiles),
            "current_missing_count": missing,
            "current_matching_count": matching,
            "current_unique_commit_count": len(commits),
            "current_commit_hashes": sorted(commits),
        }

    def run_focus_harness(self, source: Path, commit: str) -> CommandDigest:
        runner = source / "scripts" / "run_tests.sh"
        venv_python = source / ".venv" / "bin" / "python"
        if not runner.is_file() or not os.access(runner, os.X_OK) or not venv_python.is_file():
            raise UpdateBuildError("focus_harness_unavailable", "focus_tests")
        for test in FOCUS_TESTS:
            if not (source / test).is_file():
                raise UpdateBuildError("focus_test_missing", "focus_tests")
            _git(source, "cat-file", "-e", f"{commit}:{test}")
        with tempfile.TemporaryDirectory(prefix="hermes-focus-home-") as raw_home:
            result = _run_digest(
                [str(runner), "-j", "1", *FOCUS_TESTS],
                cwd=source,
                home=Path(raw_home),
                timeout=_FOCUS_TIMEOUT,
            )
        if result.returncode:
            raise UpdateBuildError(
                "focus_tests_failed",
                "focus_tests",
                output_sha256=result.output_sha256,
                output_bytes=result.output_bytes,
            )
        return result

    def verify_staged_release(
        self,
        release_id: str,
        expected_commit: str,
        max_bytes: int,
    ) -> dict[str, Any]:
        manifest = self.manager.verify(release_id)
        release = self.manager.release_path(release_id)
        if manifest.get("commit") != expected_commit:
            raise UpdateBuildError("release_commit_mismatch", "verify")
        files = manifest.get("files")
        if not isinstance(files, dict):
            raise UpdateBuildError("release_manifest_invalid", "verify")
        size_bytes = int(manifest.get("size_bytes", -1))
        actual_size = sum(int(record.get("size", 0)) for record in files.values())
        if size_bytes < 0 or size_bytes != actual_size or size_bytes > max_bytes:
            raise UpdateBuildError("release_size_invalid", "verify")
        for path in release.rglob("*"):
            if path.is_symlink():
                continue
            mode = stat.S_IMODE(path.stat().st_mode)
            if mode & 0o222:
                raise UpdateBuildError("release_is_writable", "verify")
        probe = _run_digest(
            [str(release / ".venv" / "bin" / "python"), "-I", "-c", _IMPORT_PROBE],
            cwd=release,
            home=release,
            timeout=120.0,
        )
        if probe.returncode:
            raise UpdateBuildError(
                "release_import_probe_failed",
                "verify",
                output_sha256=probe.output_sha256,
                output_bytes=probe.output_bytes,
            )
        cli_probe = _run_digest(
            [str(release / ".venv" / "bin" / "hermes"), "--help"],
            cwd=release,
            home=release,
            timeout=120.0,
        )
        if cli_probe.returncode:
            raise UpdateBuildError(
                "release_cli_probe_failed",
                "verify",
                output_sha256=cli_probe.output_sha256,
                output_bytes=cli_probe.output_bytes,
            )
        return {
            "file_count": len(files),
            "size_bytes": size_bytes,
            "manifest_sha256": _sha256_file(release / ".hermes-release.json"),
            "import_output_sha256": probe.output_sha256,
            "import_output_bytes": probe.output_bytes,
            "cli_output_sha256": cli_probe.output_sha256,
            "cli_output_bytes": cli_probe.output_bytes,
        }

    def build(
        self,
        *,
        source: Path,
        ref: str,
        expected_commit: str,
        release_id: str,
        profiles: Sequence[str],
        max_bytes: int = _DEFAULT_MAX_BYTES,
        uv_path: Path | None = None,
    ) -> dict[str, Any]:
        source = source.expanduser().resolve()
        if max_bytes <= 0:
            raise UpdateBuildError("invalid_size_budget", "source")
        with self.lock():
            commit = self.validate_source(source, ref, expected_commit)
            current = self.compare_current(profiles, commit)
            base: dict[str, Any] = {
                "schema": _STATUS_SCHEMA,
                "candidate_commit": commit,
                "release_id_sha256": _sha256_bytes(release_id.encode("utf-8")),
                "harness_sha256": _harness_sha256(),
                **current,
            }
            if current["current_matching_count"] == current["profile_count"]:
                status = {**base, "state": "no_change", "phase": "current_compare"}
                _atomic_status(self.status_path, status)
                return status

            focus = self.run_focus_harness(source, commit)
            # Close the branch/ref/dirty-worktree TOCTOU window before archiving.
            self.validate_source(source, ref, commit)
            uv = uv_path or (Path(found) if (found := shutil.which("uv")) else None)
            if uv is None or not uv.is_file():
                raise UpdateBuildError("uv_unavailable", "stage")
            npm = Path(found) if (found := shutil.which("npm")) else None
            if npm is None or not npm.is_file():
                raise UpdateBuildError("npm_unavailable", "stage")
            build_command = [
                sys.executable,
                "-m",
                "hermes_cli.immutable_update_builder",
                "_stage-build",
                "--uv",
                str(uv.resolve()),
                "--npm",
                str(npm.resolve()),
                "--final-release",
                str(self.manager.release_path(release_id)),
            ]
            try:
                self.manager.stage(
                    source,
                    release_id,
                    ref=commit,
                    build=build_command,
                    build_timeout=_BUILD_TIMEOUT,
                    max_bytes=max_bytes,
                )
            except ReleaseError as exc:
                digest = _sha256_bytes(str(exc).encode("utf-8", errors="replace"))
                raise UpdateBuildError(
                    "release_stage_failed",
                    "stage",
                    output_sha256=digest,
                    output_bytes=len(str(exc).encode("utf-8", errors="replace")),
                ) from exc
            verified = self.verify_staged_release(release_id, commit, max_bytes)
            status = {
                **base,
                "state": "staged",
                "phase": "complete",
                "focus_output_sha256": focus.output_sha256,
                "focus_output_bytes": focus.output_bytes,
                **verified,
            }
            _atomic_status(self.status_path, status)
            return status


def _stage_build(uv: Path, npm: Path, final_release: Path) -> int:
    """Build and validate inside release-manager staging; never promote."""
    cwd = Path.cwd().resolve()
    if cwd.parent.name != "releases" or not cwd.name.startswith(".staging-") or (cwd / ".git").exists():
        raise UpdateBuildError("not_release_staging", "stage_build")
    with tempfile.TemporaryDirectory(prefix="hermes-build-home-") as raw_home:
        home = Path(raw_home)
        npm_install = _run_digest(
            [
                str(npm),
                "ci",
                "--workspace",
                "web",
                "--include-workspace-root",
                "--ignore-scripts",
            ],
            cwd=cwd,
            home=home,
            timeout=_BUILD_TIMEOUT,
        )
        if npm_install.returncode:
            print(
                json.dumps(
                    {
                        "state": "failed",
                        "phase": "dashboard_dependencies",
                        "output_sha256": npm_install.output_sha256,
                        "output_bytes": npm_install.output_bytes,
                    },
                    sort_keys=True,
                )
            )
            return 1
        dashboard_build = _run_digest(
            [str(npm), "run", "build", "--workspace", "web"],
            cwd=cwd,
            home=home,
            timeout=_BUILD_TIMEOUT,
        )
        if dashboard_build.returncode:
            print(
                json.dumps(
                    {
                        "state": "failed",
                        "phase": "dashboard_build",
                        "output_sha256": dashboard_build.output_sha256,
                        "output_bytes": dashboard_build.output_bytes,
                    },
                    sort_keys=True,
                )
            )
            return 1
        dashboard_index = cwd / "hermes_cli" / "web_dist" / "index.html"
        if not dashboard_index.is_file() or dashboard_index.stat().st_size == 0:
            raise UpdateBuildError("dashboard_assets_missing", "stage_build")
        for dependency_dir in (
            cwd / "node_modules",
            cwd / "web" / "node_modules",
            cwd / "apps" / "shared" / "node_modules",
        ):
            if dependency_dir.exists() or dependency_dir.is_symlink():
                shutil.rmtree(dependency_dir, ignore_errors=False)
        result = _run_digest(
            [
                str(uv),
                "sync",
                "--frozen",
                "--no-dev",
                "--no-editable",
                "--extra",
                "messaging",
            ],
            cwd=cwd,
            home=home,
            timeout=_BUILD_TIMEOUT,
        )
        if result.returncode:
            print(
                json.dumps(
                    {
                        "state": "failed",
                        "phase": "dependency_build",
                        "output_sha256": result.output_sha256,
                        "output_bytes": result.output_bytes,
                    },
                    sort_keys=True,
                )
            )
            return 1
        relocated_scripts = _relocate_venv_shebangs(cwd, final_release)
        probe = _run_digest(
            [str(cwd / ".venv" / "bin" / "python"), "-I", "-c", _IMPORT_PROBE],
            cwd=cwd,
            home=home,
            timeout=120.0,
        )
    record = {
        "state": "passed" if probe.returncode == 0 else "failed",
        "phase": "import_probe",
        "dependency_output_sha256": result.output_sha256,
        "dependency_output_bytes": result.output_bytes,
        "dashboard_install_output_sha256": npm_install.output_sha256,
        "dashboard_install_output_bytes": npm_install.output_bytes,
        "dashboard_build_output_sha256": dashboard_build.output_sha256,
        "dashboard_build_output_bytes": dashboard_build.output_bytes,
        "import_output_sha256": probe.output_sha256,
        "import_output_bytes": probe.output_bytes,
        "relocated_script_count": relocated_scripts,
    }
    print(json.dumps(record, sort_keys=True))
    return 0 if probe.returncode == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build", help="test and stage; never promote")
    build.add_argument("release_id")
    build.add_argument("--home", required=True)
    build.add_argument("--source", required=True)
    build.add_argument("--ref", required=True)
    build.add_argument("--expected-commit", required=True)
    build.add_argument("--profile", action="append", required=True)
    build.add_argument("--max-bytes", type=int, default=_DEFAULT_MAX_BYTES)

    internal = subparsers.add_parser("_stage-build", help=argparse.SUPPRESS)
    internal.add_argument("--uv", required=True)
    internal.add_argument("--npm", required=True)
    internal.add_argument("--final-release", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "_stage-build":
        try:
            return _stage_build(
                Path(args.uv).resolve(),
                Path(args.npm).resolve(),
                Path(args.final_release).resolve(),
            )
        except (OSError, UpdateBuildError):
            print(json.dumps({"state": "failed", "phase": "stage_build"}, sort_keys=True))
            return 1

    try:
        builder = ImmutableUpdateBuilder(args.home)
        result = builder.build(
            source=Path(args.source),
            ref=args.ref,
            expected_commit=args.expected_commit,
            release_id=args.release_id,
            profiles=args.profile,
            max_bytes=args.max_bytes,
        )
    except (OSError, ReleaseError, UpdateBuildError) as exc:
        if isinstance(exc, UpdateBuildError):
            record: dict[str, Any] = {
                "schema": _STATUS_SCHEMA,
                "state": "failed",
                "phase": exc.phase,
                "error_code": exc.code,
                "output_bytes": exc.output_bytes,
            }
            if exc.output_sha256:
                record["output_sha256"] = exc.output_sha256
        else:
            record = {
                "schema": _STATUS_SCHEMA,
                "state": "failed",
                "phase": "internal",
                "error_code": "sanitized_internal_error",
                "error_sha256": _sha256_bytes(type(exc).__name__.encode("utf-8")),
            }
        if "builder" in locals():
            try:
                _atomic_status(builder.status_path, record)
            except OSError:
                pass
        print(json.dumps(record, sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
