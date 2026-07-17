"""Tests for hermes_cli.evidence_levels (track 5: QA evidence gates)."""

from __future__ import annotations

import pytest

from hermes_cli.evidence_levels import (
    EvidenceLevel,
    REQUIRED_EVIDENCE,
    block_done_on_insufficient_evidence,
    check_evidence_sufficient,
    format_evidence_report,
)


# ---------------------------------------------------------------------------
# EvidenceLevel enum
# ---------------------------------------------------------------------------


def test_evidence_level_values():
    assert EvidenceLevel.static.value == "static"
    assert EvidenceLevel.build.value == "build"
    assert EvidenceLevel.HTTP.value == "HTTP"
    assert EvidenceLevel.authenticated_e2e.value == "authenticated_e2e"
    assert EvidenceLevel.provider_verified.value == "provider_verified"


def test_evidence_level_is_str_enum():
    # str-enum means values compare equal to their string form.
    assert EvidenceLevel.build == "build"


# ---------------------------------------------------------------------------
# REQUIRED_EVIDENCE mapping
# ---------------------------------------------------------------------------


def test_required_evidence_keys():
    expected = {
        "login_logout",
        "user_isolation",
        "crud_operations",
        "file_upload",
        "auth_redirect",
        "route_availability",
        "lint",
        "typecheck",
        "build",
        "rls_check",
    }
    assert expected.issubset(REQUIRED_EVIDENCE.keys())


def test_required_evidence_values():
    assert REQUIRED_EVIDENCE["login_logout"] is EvidenceLevel.authenticated_e2e
    assert REQUIRED_EVIDENCE["crud_operations"] is EvidenceLevel.authenticated_e2e
    assert REQUIRED_EVIDENCE["file_upload"] is EvidenceLevel.authenticated_e2e
    assert REQUIRED_EVIDENCE["auth_redirect"] is EvidenceLevel.HTTP
    assert REQUIRED_EVIDENCE["route_availability"] is EvidenceLevel.HTTP
    assert REQUIRED_EVIDENCE["lint"] is EvidenceLevel.build
    assert REQUIRED_EVIDENCE["typecheck"] is EvidenceLevel.build
    assert REQUIRED_EVIDENCE["build"] is EvidenceLevel.build
    assert REQUIRED_EVIDENCE["rls_check"] is EvidenceLevel.static


# ---------------------------------------------------------------------------
# check_evidence_sufficient
# ---------------------------------------------------------------------------


def test_check_meets_exact():
    assert check_evidence_sufficient("lint", EvidenceLevel.build) is True


def test_check_exceeds():
    assert check_evidence_sufficient("lint", EvidenceLevel.HTTP) is True
    assert check_evidence_sufficient("lint", EvidenceLevel.authenticated_e2e) is True


def test_check_insufficient():
    assert check_evidence_sufficient("lint", EvidenceLevel.static) is False
    assert check_evidence_sufficient("login_logout", EvidenceLevel.HTTP) is False
    assert check_evidence_sufficient("login_logout", EvidenceLevel.static) is False


def test_check_unknown_category_defaults_to_static():
    # Unknown categories default to static — anything satisfies them.
    assert check_evidence_sufficient("nope", EvidenceLevel.static) is True
    assert check_evidence_sufficient("nope", EvidenceLevel.build) is True


def test_check_accepts_string_level():
    assert check_evidence_sufficient("lint", "build") is True
    assert check_evidence_sufficient("lint", "static") is False
    # Case-insensitive variant should also work.
    assert check_evidence_sufficient("auth_redirect", "http") is True


def test_check_invalid_level_raises():
    with pytest.raises(ValueError):
        check_evidence_sufficient("lint", "bogus")


def test_rls_check_production_upgrade():
    # Routine: static is sufficient.
    assert check_evidence_sufficient("rls_check", EvidenceLevel.static) is True
    # Production sign-off: static no longer sufficient.
    assert (
        check_evidence_sufficient(
            "rls_check", EvidenceLevel.static, require_production=True
        )
        is False
    )
    assert (
        check_evidence_sufficient(
            "rls_check", EvidenceLevel.authenticated_e2e, require_production=True
        )
        is True
    )


# ---------------------------------------------------------------------------
# block_done_on_insufficient_evidence
# ---------------------------------------------------------------------------


def test_block_no_blockers_when_all_sufficient():
    results = [
        {"category": "lint", "evidence_level": EvidenceLevel.build},
        {"category": "login_logout", "evidence_level": EvidenceLevel.authenticated_e2e},
    ]
    assert block_done_on_insufficient_evidence(results) == []


def test_block_returns_messages_sorted():
    results = [
        {"category": "login_logout", "evidence_level": EvidenceLevel.static},
        {"category": "lint", "evidence_level": EvidenceLevel.static},
    ]
    blockers = block_done_on_insufficient_evidence(results)
    assert len(blockers) == 2
    # Sorted by category — lint before login_logout.
    assert blockers[0].startswith("[evidence] lint")
    assert blockers[1].startswith("[evidence] login_logout")
    # Each message includes provided and required levels.
    assert "static" in blockers[0]
    assert "build" in blockers[0]
    assert "authenticated_e2e" in blockers[1]


def test_block_uses_name_when_present():
    results = [
        {"name": "Login flow", "category": "login_logout", "evidence_level": "static"},
    ]
    blockers = block_done_on_insufficient_evidence(results)
    assert blockers and "Login flow" in blockers[0]


def test_block_missing_evidence_level():
    results = [{"category": "lint"}]
    blockers = block_done_on_insufficient_evidence(results)
    assert len(blockers) == 1
    assert "missing" in blockers[0]


def test_block_skips_result_without_category(caplog):
    results = [{"evidence_level": "build"}]
    blockers = block_done_on_insufficient_evidence(results)
    assert blockers == []
    assert any("without 'category'" in r.message for r in caplog.records)


def test_block_production_upgrade_blocks_rls_static():
    results = [{"category": "rls_check", "evidence_level": EvidenceLevel.static}]
    # Routine: no blocker.
    assert block_done_on_insufficient_evidence(results) == []
    # Production: blocker appears.
    blockers = block_done_on_insufficient_evidence(results, require_production=True)
    assert len(blockers) == 1
    assert "authenticated_e2e" in blockers[0]


# ---------------------------------------------------------------------------
# format_evidence_report
# ---------------------------------------------------------------------------


def test_format_empty_results():
    report = format_evidence_report([])
    assert "no results" in report


def test_format_pass_and_fail():
    results = [
        {"category": "lint", "evidence_level": EvidenceLevel.build},
        {"category": "login_logout", "evidence_level": EvidenceLevel.static},
    ]
    report = format_evidence_report(results)
    assert "Evidence report:" in report
    assert "PASS" in report
    assert "FAIL" in report
    assert "Blockers (1):" in report
    assert "login_logout" in report


def test_format_all_pass():
    results = [
        {"category": "lint", "evidence_level": EvidenceLevel.build},
        {"category": "login_logout", "evidence_level": EvidenceLevel.authenticated_e2e},
    ]
    report = format_evidence_report(results)
    assert "No evidence blockers" in report
    assert "/done may proceed" in report


def test_format_missing_level_does_not_crash():
    results = [{"category": "lint"}, {"category": "bogus", "evidence_level": "wat"}]
    report = format_evidence_report(results)
    assert "FAIL" in report
    assert "missing" in report or "unknown" in report
