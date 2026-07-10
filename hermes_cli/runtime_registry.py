"""Canonical, secret-free registry for Hermes runtime identities.

The registry is deliberately separate from per-profile ``config.yaml``.  It is
operator-owned control-plane data: which profiles may run externally, which
service label and port belong to them, and whether they may own the machine's
single Kanban dispatcher.  Missing data always denies optional capabilities.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional
from urllib.parse import urlparse

import yaml

from hermes_constants import get_default_hermes_root


SCHEMA_VERSION = 2
_PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_SERVICE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ROLES = frozenset({"external_gateway", "internal_worker"})
_DOMAINS = frozenset({"general", "smart_home", "worker"})
_DELEGATION_GROUPS = frozenset({"workers"})


class RegistryError(RuntimeError):
    """The operator-owned runtime registry is absent or invalid."""


@dataclass(frozen=True)
class ProfileRuntime:
    name: str
    role: str
    home: Path
    service_label: Optional[str] = None
    port: Optional[int] = None
    health_url: Optional[str] = None
    dispatcher: bool = False
    allowed_platforms: tuple[str, ...] = ()
    required_platforms: tuple[str, ...] = ()
    bot_fingerprints: Mapping[str, str] = field(default_factory=dict)
    release_revision: Optional[str] = None
    domain: str = "general"
    can_delegate_to: tuple[str, ...] = ()
    can_create_boards: bool = False

    @property
    def restartable(self) -> bool:
        return (
            self.role == "external_gateway"
            and bool(self.service_label)
            and self.port is not None
            and bool(self.health_url)
        )


@dataclass(frozen=True)
class RuntimeRegistry:
    path: Path
    revision: str
    profiles: Mapping[str, ProfileRuntime]

    def get(self, name: str) -> Optional[ProfileRuntime]:
        return self.profiles.get(name.strip().lower())

    def require(self, name: str) -> ProfileRuntime:
        profile = self.get(name)
        if profile is None:
            raise RegistryError(f"Profile {name!r} is not authorized by {self.path}")
        return profile


def default_registry_path() -> Path:
    override = os.environ.get("HERMES_RUNTIME_REGISTRY", "").strip()
    return Path(override) if override else get_default_hermes_root() / "runtime-registry.yaml"


def default_fingerprint_key_path() -> Path:
    override = os.environ.get("HERMES_BOT_FINGERPRINT_KEY_FILE", "").strip()
    return (
        Path(override)
        if override
        else get_default_hermes_root() / "control-plane" / "bot-fingerprint.key"
    )


def credential_fingerprint(
    credential: str,
    *,
    key_path: Optional[Path] = None,
) -> Optional[str]:
    """Return a keyed, non-reversible equality fingerprint or fail closed."""
    if not isinstance(credential, str) or not credential.strip():
        return None
    path = Path(key_path or default_fingerprint_key_path())
    try:
        key = path.read_bytes()
        mode = path.stat().st_mode & 0o777
    except OSError:
        return None
    if len(key) < 32 or mode & 0o077:
        return None
    digest = hmac.new(key, credential.strip().encode("utf-8"), hashlib.sha256)
    return f"hmac-sha256:{digest.hexdigest()}"


def _as_string_list(raw: Any, field_name: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(item, str) and item.strip() for item in raw):
        raise RegistryError(f"{field_name} must be a list of non-empty strings")
    normalized = tuple(dict.fromkeys(item.strip().lower() for item in raw))
    if any(not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", item) for item in normalized):
        raise RegistryError(f"{field_name} contains an invalid platform id")
    return normalized


def _validate_loopback_health_url(raw: Any, port: Optional[int], name: str) -> Optional[str]:
    if raw is None:
        return f"http://127.0.0.1:{port}/health/detailed" if port is not None else None
    if not isinstance(raw, str):
        raise RegistryError(f"profiles.{name}.health_url must be a string")
    parsed = urlparse(raw)
    try:
        parsed_port = parsed.port
    except ValueError as exc:
        raise RegistryError(f"profiles.{name}.health_url has an invalid port") from exc
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise RegistryError(
            f"profiles.{name}.health_url must be a loopback HTTP URL; remote restart probes are denied"
        )
    if port is not None and parsed_port != port:
        raise RegistryError(f"profiles.{name}.health_url port must equal profiles.{name}.port")
    return raw


def _parse_profile(name: str, raw: Any) -> ProfileRuntime:
    if not _PROFILE_RE.fullmatch(name):
        raise RegistryError(f"Invalid profile id {name!r}")
    if not isinstance(raw, dict):
        raise RegistryError(f"profiles.{name} must be a mapping")
    allowed_keys = {
        "role", "home", "service_label", "port", "health_url", "dispatcher",
        "allowed_platforms", "required_platforms", "bot_fingerprints",
        "release_revision",
        "domain", "can_delegate_to", "can_create_boards",
    }
    unknown = set(raw) - allowed_keys
    if unknown:
        raise RegistryError(f"profiles.{name} has unknown fields: {', '.join(sorted(unknown))}")

    role = raw.get("role")
    if role not in _ROLES:
        raise RegistryError(f"profiles.{name}.role must be one of {sorted(_ROLES)}")
    home_raw = raw.get("home")
    if not isinstance(home_raw, str) or not Path(home_raw).is_absolute():
        raise RegistryError(f"profiles.{name}.home must be an absolute path")

    label = raw.get("service_label")
    if label is not None and (not isinstance(label, str) or not _SERVICE_RE.fullmatch(label)):
        raise RegistryError(f"profiles.{name}.service_label is invalid")
    port = raw.get("port")
    if port is not None and (isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535):
        raise RegistryError(f"profiles.{name}.port must be an integer from 1024 to 65535")

    dispatcher = raw.get("dispatcher", False)
    if not isinstance(dispatcher, bool):
        raise RegistryError(f"profiles.{name}.dispatcher must be boolean")
    allowed = _as_string_list(raw.get("allowed_platforms"), f"profiles.{name}.allowed_platforms")
    required = _as_string_list(raw.get("required_platforms"), f"profiles.{name}.required_platforms")
    if not set(required).issubset(allowed):
        raise RegistryError(f"profiles.{name}.required_platforms must be a subset of allowed_platforms")

    fingerprints = raw.get("bot_fingerprints") or {}
    if not isinstance(fingerprints, dict) or not all(
        isinstance(k, str)
        and isinstance(v, str)
        and bool(re.fullmatch(r"hmac-sha256:[0-9a-f]{64}", v))
        for k, v in fingerprints.items()
    ):
        raise RegistryError(
            f"profiles.{name}.bot_fingerprints must contain only hmac-sha256:<64 lowercase hex> values"
        )
    if not set(key.lower() for key in fingerprints).issubset(allowed):
        raise RegistryError(f"profiles.{name}.bot_fingerprints contains a platform not in allowed_platforms")
    release_revision = raw.get("release_revision")
    if release_revision is not None and (
        not isinstance(release_revision, str) or not re.fullmatch(r"[A-Za-z0-9._+-]{7,128}", release_revision)
    ):
        raise RegistryError(f"profiles.{name}.release_revision is invalid")

    domain = raw.get("domain")
    if domain not in _DOMAINS:
        raise RegistryError(f"profiles.{name}.domain must be one of {sorted(_DOMAINS)}")
    can_delegate_to = _as_string_list(
        raw.get("can_delegate_to"), f"profiles.{name}.can_delegate_to"
    )
    can_create_boards = raw.get("can_create_boards")
    if not isinstance(can_create_boards, bool):
        raise RegistryError(f"profiles.{name}.can_create_boards must be boolean")

    if role == "internal_worker" and any((label, port, raw.get("health_url"), dispatcher, allowed, fingerprints)):
        raise RegistryError(
            f"profiles.{name}: internal_worker may not own a service, port, dispatcher, platform, or bot identity"
        )
    if role == "internal_worker" and (
        domain != "worker" or can_delegate_to or can_create_boards
    ):
        raise RegistryError(
            f"profiles.{name}: internal_worker must use domain=worker and may not delegate or create boards"
        )
    if role == "external_gateway" and domain == "worker":
        raise RegistryError(f"profiles.{name}: external_gateway may not use domain=worker")
    if role == "external_gateway" and not all((label, port)):
        raise RegistryError(f"profiles.{name}: external_gateway requires service_label and port")

    return ProfileRuntime(
        name=name,
        role=role,
        home=Path(home_raw),
        service_label=label,
        port=port,
        health_url=_validate_loopback_health_url(raw.get("health_url"), port, name),
        dispatcher=dispatcher,
        allowed_platforms=allowed,
        required_platforms=required,
        bot_fingerprints={str(k).lower(): str(v) for k, v in fingerprints.items()},
        release_revision=release_revision,
        domain=domain,
        can_delegate_to=can_delegate_to,
        can_create_boards=can_create_boards,
    )


def _identity_revision(doc: Mapping[str, Any]) -> str:
    """Hash stable ownership identity while release intent is checked separately."""
    identity_doc: dict[str, Any] = {
        "schema_version": doc.get("schema_version"),
        "profiles": {},
    }
    profiles = doc.get("profiles")
    if isinstance(profiles, dict):
        for name, raw in profiles.items():
            if isinstance(raw, dict):
                identity_doc["profiles"][name] = {
                    key: value for key, value in raw.items() if key != "release_revision"
                }
            else:
                identity_doc["profiles"][name] = raw
    payload = json.dumps(
        identity_doc,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def load_runtime_registry(path: Optional[Path] = None, *, required: bool = True) -> RuntimeRegistry:
    path = Path(path or default_registry_path())
    try:
        raw_bytes = path.read_bytes()
    except FileNotFoundError:
        if required:
            raise RegistryError(f"Runtime registry does not exist: {path}")
        return RuntimeRegistry(path=path, revision="missing", profiles={})
    except OSError as exc:
        raise RegistryError(f"Cannot read runtime registry {path}: {exc}") from exc
    try:
        doc = yaml.safe_load(raw_bytes) or {}
    except yaml.YAMLError as exc:
        raise RegistryError(f"Runtime registry is invalid YAML: {exc}") from exc
    if not isinstance(doc, dict):
        raise RegistryError("Runtime registry root must be a mapping")
    if set(doc) - {"schema_version", "profiles"}:
        raise RegistryError(f"Runtime registry has unknown root fields: {sorted(set(doc) - {'schema_version', 'profiles'})}")
    if doc.get("schema_version") != SCHEMA_VERSION:
        raise RegistryError(f"runtime registry schema_version must be {SCHEMA_VERSION}")
    profiles_raw = doc.get("profiles")
    if not isinstance(profiles_raw, dict):
        raise RegistryError("Runtime registry profiles must be a mapping")
    profiles: dict[str, ProfileRuntime] = {}
    for raw_name, value in profiles_raw.items():
        if not isinstance(raw_name, str):
            raise RegistryError("Runtime registry profile ids must be strings")
        name = raw_name.lower()
        if name in profiles:
            raise RegistryError(f"Duplicate profile id after normalization: {raw_name!r}")
        profiles[name] = _parse_profile(name, value)

    ports: dict[int, str] = {}
    labels: dict[str, str] = {}
    homes: dict[Path, str] = {}
    dispatchers = []
    for name, profile in profiles.items():
        resolved_home = profile.home.resolve()
        if resolved_home in homes:
            raise RegistryError(f"Home {profile.home} is assigned to both {homes[resolved_home]} and {name}")
        homes[resolved_home] = name
        if profile.port is not None:
            if profile.port in ports:
                raise RegistryError(f"Port {profile.port} is assigned to both {ports[profile.port]} and {name}")
            ports[profile.port] = name
        if profile.service_label:
            if profile.service_label in labels:
                raise RegistryError(f"Service {profile.service_label} is assigned twice")
            labels[profile.service_label] = name
        if profile.dispatcher:
            dispatchers.append(name)
    if len(dispatchers) > 1:
        raise RegistryError(f"Only one profile may own the dispatcher, got {dispatchers}")
    for name, profile in profiles.items():
        for target in profile.can_delegate_to:
            if target in _DELEGATION_GROUPS:
                continue
            if target not in profiles:
                raise RegistryError(
                    f"profiles.{name}.can_delegate_to references unknown profile {target!r}"
                )
            if target == name:
                raise RegistryError(f"profiles.{name}.can_delegate_to may not include itself")
    return RuntimeRegistry(
        path=path,
        revision=_identity_revision(doc),
        profiles=profiles,
    )


def profile_name_for_home(home: Path) -> str:
    home = Path(home)
    return home.name.lower() if home.parent.name == "profiles" else "default"


def dispatcher_authorized(home: Path) -> bool:
    """Fail closed when registry/profile/authorization is absent."""
    try:
        registry = load_runtime_registry(required=True)
        profile = registry.require(profile_name_for_home(home))
        return profile.role == "external_gateway" and profile.dispatcher
    except RegistryError:
        return False


def board_creation_authorized(home: Path) -> bool:
    """Return true only when the operator registry grants board creation."""
    try:
        registry = load_runtime_registry(required=True)
        profile = registry.require(profile_name_for_home(Path(home)))
        return profile.home.resolve() == Path(home).resolve() and profile.can_create_boards
    except (RegistryError, OSError):
        return False


def delegation_authorized(home: Path, target: str) -> bool:
    """Authorize a profile-to-profile (or local worker-group) delegation.

    ``workers`` is a closed registry group: it grants local ``delegate_task``
    spawning and assignment to profiles whose registry role is
    ``internal_worker``. Unknown targets always deny.
    """
    try:
        registry = load_runtime_registry(required=True)
        source = registry.require(profile_name_for_home(Path(home)))
        if source.home.resolve() != Path(home).resolve():
            return False
        normalized = str(target or "").strip().lower()
        if not normalized:
            return False
        if normalized == "workers":
            return "workers" in source.can_delegate_to
        target_profile = registry.get(normalized)
        if target_profile is None:
            return False
        if normalized in source.can_delegate_to:
            return True
        return (
            "workers" in source.can_delegate_to
            and target_profile.role == "internal_worker"
        )
    except (RegistryError, OSError):
        return False


def runtime_identity(home: Optional[Path] = None) -> dict[str, Any]:
    """Build the non-secret identity persisted in readiness/status payloads."""
    home = Path(home or os.environ.get("HERMES_HOME") or get_default_hermes_root())
    name = profile_name_for_home(home)
    config_path = home / "config.yaml"
    try:
        config_revision = hashlib.sha256(config_path.read_bytes()).hexdigest()
    except OSError:
        config_revision = None
    try:
        registry = load_runtime_registry(required=True)
        profile = registry.require(name)
        try:
            from hermes_cli.env_loader import get_external_secret_readiness

            secret_readiness = get_external_secret_readiness(home)
        except Exception:
            secret_readiness = {"state": "unavailable", "ready": False}
        return {
            "profile": profile.name,
            "role": profile.role,
            "home": str(profile.home),
            "service_label": profile.service_label,
            "port": profile.port,
            "allowed_platforms": list(profile.allowed_platforms),
            "required_platforms": list(profile.required_platforms),
            "bot_fingerprints": dict(profile.bot_fingerprints),
            "domain": profile.domain,
            "can_delegate_to": list(profile.can_delegate_to),
            "can_create_boards": profile.can_create_boards,
            "config_revision": config_revision,
            "code_revision": os.environ.get("HERMES_RELEASE_REVISION") or profile.release_revision,
            "registry_revision": registry.revision,
            "registry_verified": profile.home.resolve() == home.resolve(),
            "secret_readiness": secret_readiness,
        }
    except (RegistryError, OSError):
        return {
            "profile": name,
            "role": "unknown",
            "home": str(home),
            "service_label": None,
            "port": None,
            "registry_revision": "missing-or-invalid",
            "registry_verified": False,
            "config_revision": config_revision,
            "code_revision": os.environ.get("HERMES_RELEASE_REVISION") or None,
            "secret_readiness": {"state": "unavailable", "ready": False},
        }
