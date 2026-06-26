"""Regression tests for the sync-test event loop fixture."""

import asyncio


_previous_loop = None


def test_sync_event_loop_fixture_provides_current_loop():
    """The autouse fixture should install a loop for legacy sync tests."""
    global _previous_loop
    _previous_loop = asyncio.get_event_loop_policy().get_event_loop()
    assert _previous_loop is not None
    assert not _previous_loop.is_closed()


def test_sync_event_loop_fixture_closes_its_loop_between_tests():
    """A loop created for one sync test must not leak into the next test."""
    assert _previous_loop is not None
    assert _previous_loop.is_closed()
