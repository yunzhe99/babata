from agents import Agent

from babata.agent import for_request


def test_request_context_does_not_mutate_base_or_leak_into_next_request():
    base = Agent(name="test", instructions="BASE")
    voice = for_request(base, "PRIVATE_MARKER", "voice", True)
    other = for_request(base, "", "text")
    assert "PRIVATE_MARKER" in voice.instructions
    assert "没有播完" in voice.instructions
    assert "语音对话" in voice.instructions
    assert other.instructions == base.instructions == "BASE"
