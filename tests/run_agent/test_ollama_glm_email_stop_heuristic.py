"""Regression tests for email-specific Ollama/GLM stop classification."""

from types import SimpleNamespace

from run_agent import AIAgent


def _agent(*, platform: str = "email") -> AIAgent:
    return AIAgent(
        api_key="test-key",
        base_url="https://ollama.example.test/v1",
        provider="ollama",
        model="glm-5.2",
        platform=platform,
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )


def _is_truncated(agent: AIAgent, content: str) -> bool:
    assistant = SimpleNamespace(content=content, tool_calls=None)
    return agent._should_treat_stop_as_truncated(
        "stop",
        assistant,
        messages=[{"role": "tool", "content": "result"}],
    )


def test_complete_mail_ending_in_manual_url_signature_remains_stop():
    content = (
        "Hej Filip!\n\nHär är hela svaret.\n\n"
        "Med vänlig hälsning,\n\nLumi\nAssistent\n"
        "Studio: killandermusicrecords.com\n"
        "Portfolio: linktr.ee/portfolio"
    )
    assert _is_truncated(_agent(), content) is False


def test_unpunctuated_partial_mail_still_becomes_length():
    assert _is_truncated(
        _agent(),
        "Här är analysen som fortfarande fortsätter med nästa viktiga del",
    ) is True


def test_url_like_tail_without_mail_signature_signal_still_becomes_length():
    assert _is_truncated(
        _agent(),
        "Jag fortsätter undersökningen på https://example.com/path",
    ) is True


def test_signature_exception_is_scoped_to_email_platform():
    content = (
        "Här är hela svaret.\n\nMed vänlig hälsning,\nLumi\n"
        "Portfolio: linktr.ee/portfolio"
    )
    assert _is_truncated(_agent(platform="discord"), content) is True
