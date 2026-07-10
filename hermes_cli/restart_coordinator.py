"""Bounded, identity-aware gateway restart coordination.

This module never sends signals and never discovers a target by port alone.
The operator registry identifies the service; the service manager performs the
restart. A listening PID must match the service manager, a profile-scoped
gateway command, and the registry-bound readiness identity or the operation
fails closed.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

import psutil

from gateway.status import evaluate_runtime_readiness
from hermes_cli.config import read_config_snapshot, validate_config_structure
from hermes_cli.runtime_registry import ProfileRuntime, RuntimeRegistry, load_runtime_registry
from utils import atomic_json_write


class RestartRejected(RuntimeError):
    """A restart was rejected without mutating the target service."""


class ServiceDriver(Protocol):
    def pid(self, label: str) -> Optional[int]: ...
    def restart(self, label: str) -> None: ...


class LaunchdServiceDriver:
    """Minimal launchd adapter; all subprocess arguments come from validated registry data."""

    def __init__(self, uid: Optional[int] = None):
        self.uid = os.getuid() if uid is None else uid

    def _target(self, label: str) -> str:
        return f"gui/{self.uid}/{label}"

    def pid(self, label: str) -> Optional[int]:
        proc = subprocess.run(
            ["launchctl", "print", self._target(label)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if proc.returncode != 0:
            return None
        match = re.search(r"^\s*pid\s*=\s*(\d+)\s*$", proc.stdout, re.MULTILINE)
        return int(match.group(1)) if match else None

    def restart(self, label: str) -> None:
        proc = subprocess.run(
            ["launchctl", "kickstart", "-k", self._target(label)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "launchctl kickstart failed").strip()
            raise RestartRejected(detail[:500])


def listening_pids(port: int) -> set[int]:
    """Return every PID listening on a TCP port; inability to inspect is fatal."""
    try:
        conns = psutil.net_connections(kind="tcp")
    except (psutil.AccessDenied, psutil.Error) as exc:
        raise RestartRejected(f"Cannot prove owner of port {port}: {exc}") from exc
    owners: set[int] = set()
    for conn in conns:
        if not conn.laddr or conn.laddr.port != port:
            continue
        if conn.status != psutil.CONN_LISTEN:
            continue
        if conn.pid is None:
            raise RestartRejected(f"Port {port} has a listener whose PID is not visible")
        owners.add(int(conn.pid))
    return owners


def process_matches_profile(pid: int, profile: ProfileRuntime) -> bool:
    """Prove a service PID is a Hermes gateway command for this profile."""
    try:
        command = " ".join(psutil.Process(pid).cmdline())
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.Error):
        return False
    if not command:
        return False
    from gateway.status import (
        _command_line_belongs_to_profile,
        looks_like_gateway_runtime_command_line,
    )

    return looks_like_gateway_runtime_command_line(command) and _command_line_belongs_to_profile(
        command, profile.home
    )


def fetch_health(url: str, timeout: float = 3.0) -> Optional[dict[str, Any]]:
    headers = {"Accept": "application/json"}
    token = os.environ.get("HERMES_HEALTH_BEARER_TOKEN", "")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                return None
            payload = json.loads(response.read(1024 * 1024).decode("utf-8"))
            return payload if isinstance(payload, dict) else None
    except (OSError, ValueError, urllib.error.URLError):
        return None


@dataclass(frozen=True)
class RestartResult:
    profile: str
    old_pid: Optional[int]
    new_pid: int
    config_revision: str
    stable_probes: int


class RestartCoordinator:
    def __init__(
        self,
        registry: RuntimeRegistry,
        *,
        driver: Optional[ServiceDriver] = None,
        state_dir: Optional[Path] = None,
        health_probe: Callable[[str, float], Optional[dict[str, Any]]] = fetch_health,
        port_probe: Callable[[int], set[int]] = listening_pids,
        process_probe: Callable[[int, ProfileRuntime], bool] = process_matches_profile,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
        max_attempts: int = 2,
        window_seconds: int = 1800,
        postflight_timeout: int = 90,
        stable_probes: int = 6,
        probe_interval: float = 2.0,
        max_probe_interval: float = 10.0,
    ):
        if (
            max_attempts < 1
            or stable_probes < 1
            or postflight_timeout < 1
            or probe_interval <= 0
            or max_probe_interval < probe_interval
        ):
            raise ValueError("invalid restart/probe bounds")
        self.registry = registry
        self.driver = driver or LaunchdServiceDriver()
        self.state_dir = Path(state_dir or registry.path.parent / "control-plane")
        self.health_probe = health_probe
        self.port_probe = port_probe
        self.process_probe = process_probe
        self.clock = clock
        self.sleeper = sleeper
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self.postflight_timeout = postflight_timeout
        self.stable_probes = stable_probes
        self.probe_interval = probe_interval
        self.max_probe_interval = max_probe_interval

    @contextmanager
    def _lock(self, profile: str):
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.state_dir, 0o700)
        except OSError:
            pass
        lock_path = self.state_dir / f"restart-{profile}.lock"
        handle = open(lock_path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                if not handle.read(1):
                    handle.seek(0)
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    raise RestartRejected(f"Restart already in progress for {profile}") from exc
            else:
                import fcntl

                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise RestartRejected(f"Restart already in progress for {profile}") from exc
            yield
        finally:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            handle.close()

    def _history_path(self, profile: str) -> Path:
        return self.state_dir / f"restart-history-{profile}.json"

    def _load_history(self, profile: str) -> list[float]:
        try:
            payload = json.loads(self._history_path(profile).read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError):
            return []
        if not isinstance(payload, list):
            return []
        return [float(value) for value in payload if isinstance(value, (int, float))]

    def _record_attempt(self, profile: str) -> None:
        now = self.clock()
        recent = [stamp for stamp in self._load_history(profile) if now - stamp < self.window_seconds]
        if len(recent) >= self.max_attempts:
            raise RestartRejected(
                f"Circuit open for {profile}: {len(recent)} restart attempts in {self.window_seconds}s"
            )
        recent.append(now)
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic_json_write(self._history_path(profile), recent, mode=0o600)

    def _validate_config(self, profile: ProfileRuntime) -> str:
        snapshot = read_config_snapshot(profile.home / "config.yaml")
        errors = [issue.message for issue in validate_config_structure(snapshot.data) if issue.severity == "error"]
        if errors:
            raise RestartRejected(f"Config validation failed: {'; '.join(errors[:5])}")
        if not snapshot.content_hash:
            raise RestartRejected(f"Config is missing for {profile.name}")
        return snapshot.content_hash

    def _validate_identity(
        self,
        profile: ProfileRuntime,
        payload: dict[str, Any],
        config_revision: str,
        *,
        allow_code_revision_mismatch: bool = False,
    ) -> tuple[bool, list[str]]:
        ready, failures = evaluate_runtime_readiness(
            payload,
            expected_profile=profile.name,
            expected_service_label=profile.service_label,
            expected_port=profile.port,
            expected_registry_revision=self.registry.revision,
        )
        identity = payload.get("runtime_identity") if isinstance(payload.get("runtime_identity"), dict) else {}
        if identity.get("config_revision") != config_revision:
            failures.append("config_revision_mismatch")
        if profile.release_revision and identity.get("code_revision") != profile.release_revision:
            failures.append("code_revision_mismatch")
        if allow_code_revision_mismatch:
            # Promotion/rollback preflight is expected to observe the OLD
            # release. This narrow exception never applies to postflight and
            # cannot suppress any other identity/readiness failure.
            failures = [failure for failure in failures if failure != "code_revision_mismatch"]
        return not failures and ready, failures

    def restart(
        self,
        profile_name: str,
        *,
        allow_release_transition: bool = False,
    ) -> RestartResult:
        """Restart one registry-owned gateway.

        ``allow_release_transition`` permits only the expected old-vs-desired
        code revision mismatch during preflight. Postflight always requires the
        registry's desired release revision.
        """
        profile = self.registry.require(profile_name)
        if not profile.restartable or profile.port is None or not profile.service_label or not profile.health_url:
            raise RestartRejected(f"Profile {profile_name} is not an authorized restartable gateway")

        with self._lock(profile.name):
            config_revision = self._validate_config(profile)
            old_service_pid = self.driver.pid(profile.service_label)
            if old_service_pid is not None and not self.process_probe(old_service_pid, profile):
                raise RestartRejected(
                    f"Service {profile.service_label} PID {old_service_pid} is not the registered profile command"
                )
            owners = self.port_probe(profile.port)
            if len(owners) > 1:
                raise RestartRejected(f"Port {profile.port} has multiple listeners: {sorted(owners)}")
            if owners and old_service_pid not in owners:
                raise RestartRejected(
                    f"Port {profile.port} is owned by unknown PID {next(iter(owners))}; refusing to kill or restart"
                )

            before = self.health_probe(profile.health_url, 3.0)
            if before is not None:
                identity_ok, failures = self._validate_identity(
                    profile,
                    before,
                    config_revision,
                    allow_code_revision_mismatch=allow_release_transition,
                )
                if not identity_ok:
                    raise RestartRejected(f"Existing listener has wrong identity: {', '.join(failures)}")
                health_pid = before.get("pid")
                if old_service_pid is None or health_pid != old_service_pid or owners != {old_service_pid}:
                    raise RestartRejected("Service PID, port owner, and health PID do not identify the same process")
                from gateway.status import parse_active_agents

                if parse_active_agents(before.get("active_agents")) > 0:
                    raise RestartRejected("Gateway has active agents; restart deferred")
            elif owners:
                # A service-owned but unidentifiable listener may be wedged, but
                # killing it would violate identity-before-mutation. Escalate.
                raise RestartRejected("Listening service did not provide verifiable readiness identity")

            self._record_attempt(profile.name)
            self.driver.restart(profile.service_label)

            deadline = self.clock() + self.postflight_timeout
            stable = 0
            new_pid: Optional[int] = None
            last_failures: list[str] = ["postflight_not_started"]
            failed_probe_streak = 0
            while self.clock() < deadline:
                service_pid = self.driver.pid(profile.service_label)
                payload = self.health_probe(profile.health_url, 3.0)
                owners = self.port_probe(profile.port)
                if payload is not None and service_pid is not None:
                    valid, failures = self._validate_identity(profile, payload, config_revision)
                    pid_matches = (
                        payload.get("pid") == service_pid
                        and owners == {service_pid}
                        and self.process_probe(service_pid, profile)
                    )
                    changed = old_service_pid is None or service_pid != old_service_pid
                    if valid and pid_matches and changed:
                        stable += 1
                        failed_probe_streak = 0
                        new_pid = service_pid
                        if stable >= self.stable_probes:
                            return RestartResult(
                                profile=profile.name,
                                old_pid=old_service_pid,
                                new_pid=new_pid,
                                config_revision=config_revision,
                                stable_probes=stable,
                            )
                    else:
                        stable = 0
                        failed_probe_streak += 1
                        last_failures = failures or ["pid_or_port_identity_mismatch"]
                else:
                    stable = 0
                    failed_probe_streak += 1
                if stable:
                    delay = self.probe_interval
                else:
                    delay = min(
                        self.max_probe_interval,
                        self.probe_interval * (2 ** min(failed_probe_streak - 1, 4)),
                    )
                self.sleeper(delay)
            raise RestartRejected(
                f"Postflight failed for {profile.name}: {', '.join(last_failures)}"
            )


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Identity-aware Hermes restart coordinator")
    parser.add_argument("profile", help="profile id from runtime-registry.yaml")
    parser.add_argument("--registry", type=Path, default=None)
    parser.add_argument(
        "--allow-release-transition",
        action="store_true",
        help=(
            "allow only a preflight code-revision mismatch for an intentional "
            "promotion/rollback; postflight remains strict"
        ),
    )
    args = parser.parse_args(argv)
    try:
        registry = load_runtime_registry(args.registry, required=True)
        result = RestartCoordinator(registry).restart(
            args.profile,
            allow_release_transition=args.allow_release_transition,
        )
    except Exception as exc:
        print(f"restart rejected: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": "restarted",
                "profile": result.profile,
                "old_pid": result.old_pid,
                "new_pid": result.new_pid,
                "config_revision": result.config_revision,
                "stable_probes": result.stable_probes,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
