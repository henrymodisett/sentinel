"""Tests for the OpenAI provider's NDJSON parser."""

from sentinel.providers.openai import OpenAIProvider


class TestParseNdjson:
    def setup_method(self) -> None:
        self.p = OpenAIProvider()

    def test_empty_output(self) -> None:
        content, inp, out = self.p._parse_ndjson("")
        assert content == ""
        assert inp == 0
        assert out == 0

    def test_whitespace_only(self) -> None:
        content, inp, out = self.p._parse_ndjson("   \n\n  \n")
        assert content == ""
        assert inp == 0
        assert out == 0

    def test_single_agent_message(self) -> None:
        ndjson = (
            '{"type":"item.completed","item":'
            '{"type":"agent_message","text":"Hello world"}}\n'
        )
        content, inp, out = self.p._parse_ndjson(ndjson)
        assert content == "Hello world"

    def test_last_agent_message_wins(self) -> None:
        """If there are multiple agent_message items, keep the last."""
        ndjson = (
            '{"type":"item.completed","item":'
            '{"type":"agent_message","text":"first"}}\n'
            '{"type":"item.completed","item":'
            '{"type":"agent_message","text":"second"}}\n'
        )
        content, _, _ = self.p._parse_ndjson(ndjson)
        assert content == "second"

    def test_usage_from_turn_completed(self) -> None:
        ndjson = (
            '{"type":"item.completed","item":'
            '{"type":"agent_message","text":"ok"}}\n'
            '{"type":"turn.completed","usage":'
            '{"input_tokens":100,"output_tokens":50}}\n'
        )
        content, inp, out = self.p._parse_ndjson(ndjson)
        assert content == "ok"
        assert inp == 100
        assert out == 50

    def test_usage_accumulates_across_turns(self) -> None:
        ndjson = (
            '{"type":"turn.completed","usage":'
            '{"input_tokens":10,"output_tokens":5}}\n'
            '{"type":"turn.completed","usage":'
            '{"input_tokens":20,"output_tokens":8}}\n'
        )
        _, inp, out = self.p._parse_ndjson(ndjson)
        assert inp == 30
        assert out == 13

    def test_ignores_malformed_lines(self) -> None:
        ndjson = (
            'not json at all\n'
            '{"type":"item.completed","item":'
            '{"type":"agent_message","text":"survived"}}\n'
            '{broken json\n'
        )
        content, _, _ = self.p._parse_ndjson(ndjson)
        assert content == "survived"

    def test_ignores_non_message_items(self) -> None:
        """command_execution items should not set content."""
        ndjson = (
            '{"type":"item.completed","item":'
            '{"type":"command_execution","command":"ls"}}\n'
            '{"type":"item.completed","item":'
            '{"type":"agent_message","text":"done"}}\n'
        )
        content, _, _ = self.p._parse_ndjson(ndjson)
        assert content == "done"

    def test_thread_started_event_ignored(self) -> None:
        """thread.started shouldn't produce content."""
        ndjson = '{"type":"thread.started","thread_id":"abc"}\n'
        content, inp, out = self.p._parse_ndjson(ndjson)
        assert content == ""
        assert inp == 0

    def test_handles_trailing_whitespace(self) -> None:
        ndjson = (
            '{"type":"item.completed","item":'
            '{"type":"agent_message","text":"ok"}}\n   \n'
        )
        content, _, _ = self.p._parse_ndjson(ndjson)
        assert content == "ok"

    def test_missing_usage_fields(self) -> None:
        """turn.completed without usage should still work."""
        ndjson = '{"type":"turn.completed"}\n'
        _, inp, out = self.p._parse_ndjson(ndjson)
        assert inp == 0
        assert out == 0
