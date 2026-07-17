"""QA evidence levels for production sign-off gates.

Track 5 of the kmrOS stop-semantics & effect-gates work.

The evidence hierarchy is strict: each level subsumes the levels below it.
A test result is considered *sufficient* only when the evidence level it
was produced at meets or exceeds the required level for its category.

Levels (low → high):

    static                 # static analysis / source inspection
    build                  # successful build, lint, or typecheck
    HTTP                   # unauthenticated HTTP probe (route reachable, 200)
    authenticated_e2e      # full authenticated end-to-end run
    provider_verified      # the upstream provider reports success (highest)

The ``rls_check`` category is special: it accepts ``static`` as minimum
evidence for routine runs but must be upgraded to ``authenticated_e2e``
before production sign-off.  Callers that care about the upgrade gate
should pass ``require_production=True`` to ``check_evidence_sufficient``
or inspect the ``rls_check`` entry of ``block_done_on_insufficient_evidence``
output.
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import List, Mapping

logger = logging.getLogger(__name__)


class EvidenceLevel(str, Enum):
    """Ordered evidence levels, lowest to highest.

    Inheriting from ``str`` lets values be serialised directly into JSON
    test reports and compared as plain strings when needed.
    """

    static = "static"
    build = "build"
    HTTP = "HTTP"
    authenticated_e2e = "authenticated_e2e"
    provider_verified = "provider_verified"


# Numeric strength for ordering.  Higher == stronger evidence.
_LEVEL_STRENGTH: Mapping[EvidenceLevel, int] = {
    EvidenceLevel.static: 0,
    EvidenceLevel.build: 1,
    EvidenceLevel.HTTP: 2,
    EvidenceLevel.authenticated_e2e: 3,
    EvidenceLevel.provider_verified: 4,
}


# Default required evidence per test category.  Categories not listed
# here default to ``EvidenceLevel.static`` (i.e. anything is sufficient).
REQUIRED_EVIDENCE: Mapping[str, EvidenceLevel] = {
    "login_logout": EvidenceLevel.authenticated_e2e,
    "user_isolation": EvidenceLevel.authenticated_e2e,
    "crud_operations": EvidenceLevel.authenticated_e2e,
    "file_upload": EvidenceLevel.authenticated_e2e,
    "auth_redirect": EvidenceLevel.HTTP,
    "route_availability": EvidenceLevel.HTTP,
    "lint": EvidenceLevel.build,
    "typecheck": EvidenceLevel.build,
    "build": EvidenceLevel.build,
    # rls_check accepts static for routine runs but must be upgraded to
    # authenticated_e2e for production sign-off (see module docstring).
    "rls_check": EvidenceLevel.static,
}

# Categories that require an upgrade to authenticated_e2e at sign-off.
_PRODUCTION_UPGRADE_CATEGORIES = frozenset({"rls_check"})


def _resolve_level(value) -> EvidenceLevel:
    """Coerce a string or EvidenceLevel into an EvidenceLevel.

    Accepts the enum directly, the string value, or a case-insensitive
    variant of the string value (e.g. ``"http"`` → ``EvidenceLevel.HTTP``).
    Raises ``ValueError`` if the value cannot be resolved.
    """
    if isinstance(value, EvidenceLevel):
        return value
    if isinstance(value, str):
        # Try exact value first, then case-insensitive match.
        try:
            return EvidenceLevel(value)
        except ValueError:
            for level in EvidenceLevel:
                if level.value.lower() == value.lower():
                    return level
    raise ValueError(f"Unknown evidence level: {value!r}")


def check_evidence_sufficient(
    category: str,
    provided_level,
    *,
    require_production: bool = False,
) -> bool:
    """Return True when ``provided_level`` meets or exceeds the requirement.

    Parameters
    ----------
    category
        The test category key (e.g. ``"login_logout"``).  Unknown
        categories default to ``static`` requirement.
    provided_level
        The evidence level actually produced.  Accepts an
        ``EvidenceLevel`` or its string value.
    require_production
        When True, categories in ``_PRODUCTION_UPGRADE_CATEGORIES`` are
        treated as if they required ``authenticated_e2e``.  Use this at
        production sign-off time.
    """
    provided = _resolve_level(provided_level)
    required = REQUIRED_EVIDENCE.get(category, EvidenceLevel.static)
    if require_production and category in _PRODUCTION_UPGRADE_CATEGORIES:
        required = EvidenceLevel.authenticated_e2e
    return _LEVEL_STRENGTH[provided] >= _LEVEL_STRENGTH[required]


def block_done_on_insufficient_evidence(
    results: List[dict],
    *,
    require_production: bool = False,
) -> List[str]:
    """Return blocker messages for results with insufficient evidence.

    Each dict in ``results`` must carry ``category`` and
    ``evidence_level`` keys.  Additional keys (e.g. ``name``, ``status``)
    are ignored but may be referenced in the message when present.

    The returned list is sorted by category then by provided level, so
    output is deterministic across runs.  An empty list means no
    blockers — ``/done`` may proceed.
    """
    blockers: List[str] = []
    for result in results:
        category = result.get("category")
        if category is None:
            # Malformed result; skip rather than crash the gate.
            logger.warning("evidence check skipped result without 'category': %r", result)
            continue
        provided = result.get("evidence_level")
        if provided is None:
            blockers.append(
                f"[evidence] {category}: missing 'evidence_level' — cannot verify"
            )
            continue
        try:
            sufficient = check_evidence_sufficient(
                category, provided, require_production=require_production
            )
        except (ValueError, TypeError) as exc:
            blockers.append(
                f"[evidence] {category}: invalid evidence_level={provided!r} — {exc}"
            )
            continue
        if sufficient:
            continue
        required = REQUIRED_EVIDENCE.get(category, EvidenceLevel.static)
        if require_production and category in _PRODUCTION_UPGRADE_CATEGORIES:
            required = EvidenceLevel.authenticated_e2e
        name = result.get("name", category)
        blockers.append(
            f"[evidence] {name} ({category}): provided {provided} < required {required.value}"
        )
    blockers.sort()
    return blockers


def format_evidence_report(results: List[dict]) -> str:
    """Render a human-readable evidence summary from ``results``.

    The report lists each result with its category, provided level,
    required level, and a pass/fail marker.  Results with unknown
    categories or missing fields are surfaced explicitly rather than
    hidden.
    """
    if not results:
        return "Evidence report: no results to report."

    lines: List[str] = ["Evidence report:", ""]
    width_cat = max(len(str(r.get("category", "?"))) for r in results)
    width_cat = max(width_cat, len("category"))

    for result in results:
        category = result.get("category", "?")
        provided = result.get("evidence_level")
        if provided is None:
            lines.append(
                f"  {str(category).ljust(width_cat)}  provided=?                required=?                FAIL (missing evidence_level)"
            )
            continue
        try:
            provided_level = _resolve_level(provided)
        except ValueError:
            lines.append(
                f"  {str(category).ljust(width_cat)}  provided={provided!r:<22} required=?                FAIL (unknown level)"
            )
            continue
        except TypeError:
            lines.append(
                f"  {str(category).ljust(width_cat)}  provided={provided!r:<22} required=?                FAIL (invalid level)"
            )
            continue
        required = REQUIRED_EVIDENCE.get(category, EvidenceLevel.static)
        ok = check_evidence_sufficient(category, provided_level)
        marker = "PASS" if ok else "FAIL"
        lines.append(
            f"  {str(category).ljust(width_cat)}  provided={provided_level.value:<22} required={required.value:<22} {marker}"
        )

    blockers = block_done_on_insufficient_evidence(results)
    lines.append("")
    if blockers:
        lines.append(f"Blockers ({len(blockers)}):")
        for b in blockers:
            lines.append(f"  - {b}")
    else:
        lines.append("No evidence blockers. /done may proceed.")
    return "\n".join(lines)
