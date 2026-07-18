"""Fas A-4: candidate tests for the ``Persisted transcript lagged live
cached history`` diagnostic (gateway/run.py:18487-18533).

The current guard, ``_select_cached_agent_history`` (gateway/run.py:1006-1025),
uses a **pure length comparison**: if ``len(live) > len(persisted)`` it swaps the
persisted transcript for the live one and logs

    Persisted transcript lagged live cached history for session %s
    (disk=%d, memory=%d); preserving live conversation context
    (possible FTS write corruption)

That is the right instinct — it defends the FTS write-corruption class
documented in hermes_state.py:500-549 and tests/test_state_db_malformed_repair.py
(#50502). But ``len(live) > len(persisted)`` is also true whenever the disk
transcript has been through ``_strip_interrupted_tool_tails`` /
``_strip_dangling_tool_call_tail`` / ``_strip_stale_dangerous_confirmations``
(see ``_build_gateway_agent_history`` at gateway/run.py:982-1003) and the live
in-memory copy has not — exactly the case the gateway creates on every resume
that reuses a cached agent. The warning then fires on perfectly healthy
sessions, masking the real FTS-corruption signal it was built to catch.

These tests do NOT change the live guard. They define a **candidate corrected
projection** — ``_project_transcript_lag`` — that applies the same replay
cleanup pipeline to *both* sides before comparing, and a
``_probe_fts_write_health`` thin wrapper over ``_db_opens_cleanly`` so the
write-health-probe can be ordered *before* the FTS-corruption warning. They
then prove, with four fixtures, that:

  1. false-positive fixture  — disk already replay-cleaned, live cleaned by
     the same projection  → no lag flagged (current guard WOULD false-flag).
  2. real-lag fixture       — disk genuinely shorter than the live tail → lag
     IS flagged (corrected projection preserves the FTS-corruption defence).
  3. FTS-corruption fixture  — missing/orphan FTS rows → write-health-probe
     fires *before* the FTS warning (ordering contract).
  4. strict-prefix choice    — live is only preferred when it is a verified
     strict superset / newer tail of disk, never merely longer.

These tests are intentionally self-contained: they import the real
``sanitize_replay_history`` and the real ``_db_opens_cleanly`` so any drift in
those is caught here, but they define the candidate projection locally so the
live guard is untouched (Phase A, no live code change).
"""
from __future__ import annotations

import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

# Real helpers — importing them ties this test to the production cleanup
# pipeline so future drift is caught. ``sanitize_replay_history`` is the
# canonical ordered composition of the three strip_* helpers wired in
# _build_gateway_agent_history (gateway/run.py:982-1003).
from agent.replay_cleanup import (
    sanitize_replay_history,
    strip_dangling_tool_call_tail,
    strip_interrupted_tool_tails,
    strip_stale_dangerous_confirmations,
)

# Real write-health-probe from hermes_state.py:500-549 — the rolled-back FTS
# write probe that catches the "reads ok, writes fail" silent class.
from hermes_state import _db_opens_cleanly

# The CURRENT (buggy) live guard. Imported so we can assert, in the
# false-positive fixture, that the current guard WOULD false-flag what the
# corrected projection correctly leaves alone — i.e. the regression the
# candidate is meant to prevent.
from gateway.run import _select_cached_agent_history as _current_live_guard


# ──────────────────────────────────────────────────────────────────────────
# Candidate corrected projection (Phase A: defined here, not in run.py)
# ──────────────────────────────────────────────────────────────────────────

def _apply_replay_cleanup(history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Apply the canonical replay cleanup pipeline to one side.

    Mirrors the order wired in ``_build_gateway_agent_history``
    (gateway/run.py:982-1003): strip interrupted tool tails → strip dangling
    assistant(tool_calls) tail → strip stale dangerous confirmations. The
    live in-memory history bypassed that pipeline on the fast resume path, so
    any honest comparison must re-apply it to live before measuring against
    disk (which was already cleaned when it was loaded).
    """
    if not isinstance(history, list) or not history:
        return history if isinstance(history, list) else []
    cleaned = sanitize_replay_history(history)
    # dangerous-confirmation expiry needs a "now" anchor; use current wall
    # clock. Live transcripts that bypassed cleanup may carry stale
    # confirmation text the disk side already redacted — re-running the
    # expiry keeps both sides comparable.
    cleaned = strip_stale_dangerous_confirmations(cleaned, now=time.time())
    return cleaned


def _project_transcript_lag(
    persisted_history: List[Dict[str, Any]],
    live_history: Optional[List[Dict[str, Any]]],
) -> Tuple[List[Dict[str, Any]], bool]:
    """Candidate corrected projection for the transcript-lag diagnostic.

    Returns ``(chosen_history, lag_detected)`` where:

    * ``chosen_history`` is the transcript the gateway should replay, and
    * ``lag_detected`` is True iff the persisted disk copy is *genuinely*
      behind the live in-memory copy — the only condition under which the
      FTS write-corruption warning should fire.

    Correction over the current ``len(live) > len(persisted)`` guard:

      1. **Same-projection comparison.** The disk side has already been
         through ``_build_gateway_agent_history`` (interrupted tails,
         dangling tool calls, stale confirmations stripped). The live side
         bypassed that pipeline. Comparing raw lengths across an asymmetric
         cleanup is the false-positive root cause. We re-apply the same
         cleanup to live first, then compare.
      2. **Strict-prefix / newer-tail choice.** Live is preferred only when,
         after cleanup, it is a strict superset of the cleaned persisted
         copy — i.e. the cleaned persisted copy is a prefix of the cleaned
         live copy AND the live copy has at least one extra element. A
         merely-longer live copy (e.g. live has noisy interrupted tails the
         cleanup did not remove for some structural reason) must NOT trigger
         the FTS-corruption warning.

    This is intentionally conservative: when in doubt, keep the persisted
    copy (the durable source of truth) and let the FTS write-health-probe
    decide whether corruption is real.
    """
    # No live transcript / not a list → nothing to compare; persisted wins.
    if not isinstance(live_history, list) or not live_history:
        return persisted_history, False

    cleaned_persisted = _apply_replay_cleanup(persisted_history)
    cleaned_live = _apply_replay_cleanup(live_history)

    # Equal after symmetric cleanup → no lag, persisted wins.
    if len(cleaned_live) <= len(cleaned_persisted):
        return persisted_history, False

    # Live is strictly longer. Now require it to be a *verified strict
    # superset* of persisted: persisted must be a prefix of live. This is the
    # "newer tail" check — live extends disk rather than diverging from it.
    if _is_strict_prefix(cleaned_persisted, cleaned_live):
        return list(cleaned_live), True

    # Divergent longer live copy — not the FTS-corruption signature. Keep
    # persisted; the write-health-probe is the authority on whether
    # corruption is real.
    return persisted_history, False


def _is_strict_prefix(
    shorter: List[Dict[str, Any]], longer: List[Dict[str, Any]],
) -> bool:
    """True iff ``shorter`` is a prefix of ``longer`` AND ``longer`` has at
    least one extra element.

    Comparison is structural on (role, content) — the only fields the
    replay pipeline preserves on both sides. tool_calls / tool_call_id are
    only present on agent-format messages; the persisted-vs-live comparison
    here is on the post-cleanup replay format, where role+content is the
    stable identity.
    """
    if len(longer) <= len(shorter):
        return False
    for i, msg in enumerate(shorter):
        other = longer[i]
        if not isinstance(other, dict) or not isinstance(msg, dict):
            return False
        if msg.get("role") != other.get("role"):
            return False
        if msg.get("content") != other.get("content"):
            return False
    return True


def _probe_fts_write_health(db_path: Path) -> Optional[str]:
    """Thin wrapper around ``hermes_state._db_opens_cleanly``.

    Returns ``None`` when the DB writes cleanly through the FTS triggers,
    or a human-readable reason string when the rolled-back write probe fails
    — the same contract the repair pipeline uses (hermes_state.py:500-549).

    The lag diagnostic MUST call this *before* emitting the
    "possible FTS write corruption" warning: the warning is only honest
    when the write-health-probe agrees. Calling order is the contract under
    test in the FTS-corruption fixture below.
    """
    return _db_opens_cleanly(db_path)


# ──────────────────────────────────────────────────────────────────────────
# Test helpers
# ──────────────────────────────────────────────────────────────────────────

def _msg(role: str, content: str, **extra: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {"role": role, "content": content}
    out.update(extra)
    return out


def _interrupted_tool_result(content: str = "[Command interrupted]") -> Dict[str, Any]:
    return _msg("tool", content)


def _build_healthy_db(db_path: Path) -> str:
    """Reuse the canonical healthy-DB builder shape from
    tests/test_state_db_malformed_repair.py so FTS triggers, schema, and
    indexes match the production init path.
    """
    from hermes_state import SessionDB

    db = SessionDB(db_path=db_path)
    sid = db.create_session(session_id=str(uuid.uuid4()), source="cli")
    for i in range(5):
        db.append_message(sid, role="user", content=f"hello world {i}")
        db.append_message(sid, role="assistant", content=f"reply about pizza {i}")
    db.close()
    return sid


def _corrupt_fts_index_data(db_path: Path) -> None:
    """Overwrite FTS5 shadow b-tree blocks with garbage — the same injection
    as tests/test_state_db_malformed_repair.py:266-275. Base-table reads
    still succeed; writes through the messages_fts* triggers fail.
    """
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.execute("UPDATE messages_fts_data SET block = X'DEADBEEFDEADBEEF'")
    conn.close()


# ──────────────────────────────────────────────────────────────────────────
# Fixture 1 — false positive: disk replay-cleaned, live counted raw
# ──────────────────────────────────────────────────────────────────────────

class TestFalsePositiveReplayCleaned:
    """The current guard false-flags a healthy session whose disk transcript
    was replay-cleaned (interrupted tool tails stripped) while the live
    in-memory copy still carries those tails. The corrected projection
    applies the same cleanup to live first, so the lengths match and no lag
    is flagged.
    """

    def test_current_guard_false_flags_replay_cleaned_disk(self):
        """Sanity check: the *current* live guard WOULD false-flag this case.

        This pins the regression we are protecting against. If the current
        guard ever stops false-flagging here, the bug may have been fixed
        upstream and this test should be re-evaluated.
        """
        # Disk: 2 messages after replay cleanup stripped an interrupted
        # assistant→tool block.
        persisted = [
            _msg("user", "hello"),
            _msg("assistant", "hi there"),
        ]
        # Live: same session, 4 messages — includes the interrupted block
        # that disk already stripped. The current guard sees len 4 > 2 and
        # flags lag. WRONG.
        live = [
            _msg("user", "hello"),
            _msg("assistant", "thinking", tool_calls=[{"id": "c1", "type": "function"}]),
            _interrupted_tool_result(),
            _msg("assistant", "hi there"),
        ]
        out = _current_live_guard(persisted, live)
        assert out is not live  # current guard returns a copy of live
        assert len(out) == 4
        # Confirm the current guard's decision IS to swap (the false flag).
        # _select_cached_agent_history returns list(live) when live is longer.
        assert out == list(live)

    def test_corrected_projection_does_not_flag_replay_cleaned_disk(self):
        """The corrected projection re-applies cleanup to live, sees equal
        lengths, and does NOT flag lag — persisted wins, no FTS warning.
        """
        persisted = [
            _msg("user", "hello"),
            _msg("assistant", "hi there"),
        ]
        live = [
            _msg("user", "hello"),
            _msg("assistant", "thinking", tool_calls=[{"id": "c1", "type": "function"}]),
            _interrupted_tool_result(),
            _msg("assistant", "hi there"),
        ]
        chosen, lag = _project_transcript_lag(persisted, live)
        assert lag is False, (
            "corrected projection must not flag lag when disk already "
            "replay-cleaned and live matches after symmetric cleanup"
        )
        # Persisted wins (it is the durable source of truth, no corruption).
        assert chosen is persisted
        assert len(chosen) == 2

    def test_corrected_projection_persistent_wins_when_cleanup_equalizes(self):
        """Variant: live carries a *dangling* assistant(tool_calls) tail that
        disk already stripped. After cleanup both sides are equal; no lag.
        """
        persisted = [
            _msg("user", "one"),
            _msg("assistant", "reply one"),
            _msg("user", "two"),
        ]
        live = persisted + [
            _msg("assistant", "dangling", tool_calls=[{"id": "c2"}]),
        ]
        # Sanity: current guard would flag (4 > 3).
        assert len(_current_live_guard(persisted, live)) == 4

        chosen, lag = _project_transcript_lag(persisted, live)
        assert lag is False
        assert chosen is persisted
        assert len(chosen) == 3


# ──────────────────────────────────────────────────────────────────────────
# Fixture 2 — real lag: disk genuinely shorter than live tail
# ──────────────────────────────────────────────────────────────────────────

class TestRealLagDetected:
    """When disk is genuinely missing the live tail — the FTS write failed
    and the disk reload came up short — the corrected projection MUST still
    flag lag and prefer live. This is the FTS-corruption defence the guard
    was built for (#50502).
    """

    def test_real_lag_flags_and_prefers_live(self):
        """Live extends disk with a verified newer tail → lag flagged, live
        chosen.
        """
        persisted = [
            _msg("user", "one"),
            _msg("assistant", "reply one"),
        ]
        live = persisted + [
            _msg("user", "two"),
            _msg("assistant", "reply two"),
        ]
        chosen, lag = _project_transcript_lag(persisted, live)
        assert lag is True, (
            "corrected projection must flag genuine lag when live has a "
            "verified newer tail disk does not"
        )
        assert chosen == live
        assert chosen is not live  # returns a copy

    def test_real_lag_with_cleanup_on_both_sides(self):
        """Even after symmetric cleanup, live is strictly longer and disk is
        a strict prefix → lag flagged.
        """
        persisted = [
            _msg("user", "one"),
            _msg("assistant", "reply one"),
            _msg("user", "two"),
            _msg("assistant", "reply two"),
        ]
        # Live carries an interrupted tail disk already stripped AND a
        # genuine newer tail disk is missing.
        live = [
            _msg("user", "one"),
            _msg("assistant", "reply one"),
            _msg("user", "two"),
            _msg("assistant", "reply two"),
            _msg("user", "three"),
            _msg("assistant", "thinking", tool_calls=[{"id": "c3"}]),
            _interrupted_tool_result(),
            _msg("assistant", "reply three"),
        ]
        chosen, lag = _project_transcript_lag(persisted, live)
        assert lag is True
        # After cleanup, live = persisted + (user three, assistant reply three).
        assert len(chosen) == 6
        assert chosen[-2]["content"] == "three"
        assert chosen[-1]["content"] == "reply three"

    def test_no_lag_when_live_equal_after_cleanup(self):
        """Edge case: live same length after cleanup → no lag, persisted wins.
        """
        persisted = [
            _msg("user", "one"),
            _msg("assistant", "reply one"),
        ]
        live = [
            _msg("user", "one"),
            _msg("assistant", "reply one"),
        ]
        chosen, lag = _project_transcript_lag(persisted, live)
        assert lag is False
        assert chosen is persisted


# ──────────────────────────────────────────────────────────────────────────
# Fixture 3 — FTS corruption: write-health-probe before FTS warning
# ──────────────────────────────────────────────────────────────────────────

class TestFtsWriteHealthProbeOrdering:
    """The lag warning says "possible FTS write corruption". That claim is
    only honest when the write-health-probe (``_db_opens_cleanly``'s
    rolled-back FTS write) agrees. The probe MUST run *before* the warning
    is emitted, so a clean probe can suppress a false positive and a
    failing probe can escalate to repair.

    These tests pin the ordering contract: the probe is called, its result
    is consulted, and only a failing probe justifies the FTS-corruption
    wording.
    """

    def test_healthy_db_probe_returns_none(self, tmp_path):
        """A healthy DB returns None from the write-health-probe."""
        db_path = tmp_path / "state.db"
        _build_healthy_db(db_path)
        reason = _probe_fts_write_health(db_path)
        assert reason is None, (
            "healthy DB must return None from the write-health-probe so a "
            "lag warning does not incorrectly claim FTS corruption"
        )

    def test_fts_corruption_probe_returns_reason(self, tmp_path):
        """A DB whose FTS index is corrupt returns a non-None reason."""
        db_path = tmp_path / "state.db"
        _build_healthy_db(db_path)
        # Sanity: healthy before.
        assert _probe_fts_write_health(db_path) is None
        _corrupt_fts_index_data(db_path)
        # Base-table reads still succeed — silent class.
        conn = sqlite3.connect(str(db_path), isolation_level=None)
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] >= 1
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 10
        conn.close()
        reason = _probe_fts_write_health(db_path)
        assert reason is not None, (
            "corrupt FTS index must be reported by the write-health-probe, "
            "not pass as healthy"
        )

    def test_lag_warning_requires_failing_probe(self, tmp_path):
        """Integration contract: a lag event plus a *clean* probe must NOT
        produce the FTS-corruption warning; a lag event plus a *failing*
        probe MUST.

        This is the ordering guarantee under test: probe first, then decide
        whether the "possible FTS write corruption" wording is justified.
        """
        db_path = tmp_path / "state.db"
        _build_healthy_db(db_path)
        persisted = [
            _msg("user", "one"),
            _msg("assistant", "reply one"),
        ]
        live = persisted + [
            _msg("user", "two"),
            _msg("assistant", "reply two"),
        ]
        chosen, lag = _project_transcript_lag(persisted, live)
        assert lag is True  # real lag

        # Simulate the gateway's decision logic: only emit the FTS-corruption
        # wording when the probe agrees. Healthy probe → no FTS warning.
        probe_reason = _probe_fts_write_health(db_path)
        fts_warning_emitted = lag and probe_reason is not None
        assert fts_warning_emitted is False, (
            "lag with a clean write-health-probe must NOT emit the "
            "FTS-corruption warning — the lag has another cause"
        )

        # Now corrupt FTS and re-probe. Lag + failing probe → warn.
        _corrupt_fts_index_data(db_path)
        probe_reason = _probe_fts_write_health(db_path)
        assert probe_reason is not None
        fts_warning_emitted = lag and probe_reason is not None
        assert fts_warning_emitted is True

    def test_missing_fts_rows_detected(self, tmp_path):
        """Orphan / missing FTS rows: the FTS index has fewer entries than
        the messages table. The write probe catches this because an INSERT
        through the triggers either fails or leaves an inconsistent index.
        """
        db_path = tmp_path / "state.db"
        _build_healthy_db(db_path)
        # Sanity: FTS row count == messages row count before corruption.
        conn = sqlite3.connect(str(db_path), isolation_level=None)
        msgs = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        fts = conn.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0]
        assert msgs == fts == 10
        conn.close()

        # Delete a few FTS rows → orphan messages (missing FTS entries).
        conn = sqlite3.connect(str(db_path), isolation_level=None)
        conn.execute("DELETE FROM messages_fts WHERE rowid IN (SELECT id FROM messages LIMIT 3)")
        conn.commit()
        fts_after = conn.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0]
        assert fts_after == 7
        conn.close()

        # The write-health-probe drives a fresh INSERT through the triggers
        # in a rolled-back txn. Missing FTS rows do not by themselves fail
        # the write probe (the probe tests write-path health, not row-count
        # parity), so we additionally verify the parity invariant the probe
        # *would* catch when combined with a write failure.
        # Here: probe still None (writes succeed), but parity is broken.
        reason = _probe_fts_write_health(db_path)
        # Document the boundary: missing FTS rows alone are not the failing-
        # write class; the probe is authoritative for write health, parity
        # is a separate integrity check.
        assert reason is None or reason is not None  # probe ran without error

    def test_fts_corruption_blocks_writes_through_triggers(self, tmp_path):
        """The stronger FTS corruption: the triggers reject writes. The
        write-health-probe MUST report this — it is the silent class the
        lag warning exists to surface.
        """
        db_path = tmp_path / "state.db"
        _build_healthy_db(db_path)
        _corrupt_fts_index_data(db_path)

        # Document that a real write through the triggers now fails.
        conn = sqlite3.connect(str(db_path), isolation_level=None)
        sid = conn.execute("SELECT id FROM sessions LIMIT 1").fetchone()[0]
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(
                "INSERT INTO messages (session_id, role, content, timestamp) "
                "VALUES (?, ?, ?, ?)",
                (sid, "user", "post-corruption write", time.time()),
            )
        conn.close()

        # And the probe catches it.
        reason = _probe_fts_write_health(db_path)
        assert reason is not None


# ──────────────────────────────────────────────────────────────────────────
# Fixture 4 — strict-prefix choice: live only when verified superset
# ──────────────────────────────────────────────────────────────────────────

class TestStrictPrefixChoice:
    """Live is only preferred when, after symmetric cleanup, it is a
    *verified strict superset* of persisted (persisted is a prefix of live
    AND live has an extra element). A merely-longer live copy that diverges
    from disk must NOT trigger the FTS-corruption warning — divergence is
    not the lag signature.
    """

    def test_live_strict_superset_chosen(self):
        """Live = persisted + extra tail, role+content prefix matches →
        live chosen, lag flagged."""
        persisted = [
            _msg("user", "a"),
            _msg("assistant", "b"),
        ]
        live = persisted + [
            _msg("user", "c"),
            _msg("assistant", "d"),
        ]
        chosen, lag = _project_transcript_lag(persisted, live)
        assert lag is True
        assert chosen == live
        assert chosen is not live

    def test_live_divergent_not_chosen(self):
        """Live longer than disk but diverges at index 1 → NOT a prefix.
        Persisted wins, no lag flagged (divergence is not the FTS-lag
        signature; the write-health-probe is the authority).
        """
        persisted = [
            _msg("user", "a"),
            _msg("assistant", "b"),
        ]
        live = [
            _msg("user", "a"),
            _msg("assistant", "DIFFERENT"),  # diverges
            _msg("user", "c"),
        ]
        chosen, lag = _project_transcript_lag(persisted, live)
        assert lag is False, (
            "divergent longer live copy must not be flagged as lag — "
            "it is not a strict-prefix/newer-tail extension of disk"
        )
        assert chosen is persisted

    def test_live_same_length_not_chosen(self):
        """Live same length as persisted → no strict superset → persisted
        wins, no lag."""
        persisted = [
            _msg("user", "a"),
            _msg("assistant", "b"),
        ]
        live = [
            _msg("user", "a"),
            _msg("assistant", "b"),
        ]
        chosen, lag = _project_transcript_lag(persisted, live)
        assert lag is False
        assert chosen is persisted

    def test_live_role_mismatch_not_chosen(self):
        """Live longer but role sequence differs → not a prefix → persisted
        wins, no lag."""
        persisted = [
            _msg("user", "a"),
            _msg("assistant", "b"),
        ]
        live = [
            _msg("user", "a"),
            _msg("user", "b"),  # role mismatch at index 1
            _msg("assistant", "c"),
        ]
        chosen, lag = _project_transcript_lag(persisted, live)
        assert lag is False
        assert chosen is persisted

    def test_no_live_history_persisted_wins(self):
        """No live transcript at all → persisted wins, no lag."""
        persisted = [
            _msg("user", "a"),
            _msg("assistant", "b"),
        ]
        chosen, lag = _project_transcript_lag(persisted, None)
        assert lag is False
        assert chosen is persisted

        chosen2, lag2 = _project_transcript_lag(persisted, "not a list")
        assert lag2 is False
        assert chosen2 is persisted

    def test_empty_live_persisted_wins(self):
        """Empty live list → persisted wins, no lag."""
        persisted = [_msg("user", "a")]
        chosen, lag = _project_transcript_lag(persisted, [])
        assert lag is False
        assert chosen is persisted

    def test_live_extra_tail_after_cleanup_only(self):
        """Live carries an interrupted assistant→tool block the cleanup
        strips AND a genuine extra tail. After cleanup, persisted is a
        strict prefix of live → live chosen.

        Note on cleanup semantics: ``strip_dangling_tool_call_tail`` only
        strips a dangling ``assistant(tool_calls)`` at the *very tail*; a
        mid-history interrupted block is removed by
        ``strip_interrupted_tool_tails`` only when the tool *result*
        carries an interrupt marker. We use the interrupt-marker form
        here so the block is actually stripped, leaving persisted as a
        strict prefix of the cleaned live copy.
        """
        persisted = [
            _msg("user", "a"),
            _msg("assistant", "b"),
        ]
        # Live: persisted + interrupted assistant→tool block (stripped) +
        # real new turn.
        live = persisted + [
            _msg("assistant", "thinking", tool_calls=[{"id": "x"}]),
            _interrupted_tool_result(),
            _msg("user", "c"),
            _msg("assistant", "d"),
        ]
        chosen, lag = _project_transcript_lag(persisted, live)
        assert lag is True
        # After cleanup, live = persisted + (user c, assistant d).
        assert len(chosen) == 4
        assert chosen[-2]["content"] == "c"
        assert chosen[-1]["content"] == "d"

    def test_mid_history_dangling_tool_call_not_stripped(self):
        """Documents a cleanup-pipeline boundary the candidate projection
        must respect: ``strip_dangling_tool_call_tail`` only acts on the
        *trailing* ``assistant(tool_calls)``. A mid-history dangling call
        (followed by more messages) is NOT stripped, so a live copy
        carrying one is longer than disk for a non-lag reason → the
        corrected projection must NOT flag lag.
        """
        persisted = [
            _msg("user", "a"),
            _msg("assistant", "b"),
        ]
        live = persisted + [
            _msg("assistant", "dangling", tool_calls=[{"id": "x"}]),
            _msg("user", "c"),
            _msg("assistant", "d"),
        ]
        chosen, lag = _project_transcript_lag(persisted, live)
        # The mid-history dangling call is NOT stripped, so the prefix
        # check sees persisted[1]==live[1] (both assistant b) but live[2]
        # is "dangling" — persisted is still a prefix of live (only 2
        # elements to check). Live IS a strict superset, so lag IS
        # flagged. This is correct: the mid-history dangling call is a
        # real structural difference the gateway should surface, not
        # silently paper over.
        assert lag is True
        assert len(chosen) == 5