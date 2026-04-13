"""Tests for parse_json_safe — the shared JSON parser for CLI output."""


from sentinel.providers.interface import parse_json_safe


class TestParseJsonSafe:
    def test_valid_json(self) -> None:
        result = parse_json_safe('{"key": "value", "num": 42}')
        assert result == {"key": "value", "num": 42}

    def test_valid_json_with_whitespace(self) -> None:
        result = parse_json_safe('  {"key": "value"}  ')
        assert result == {"key": "value"}

    def test_json_with_trailing_garbage(self) -> None:
        """Gemini CLI appends hook output after the JSON."""
        result = parse_json_safe(
            '{"response": "hello", "stats": {}}\nSessionEnd hook output here'
        )
        assert result is not None
        assert result["response"] == "hello"

    def test_json_with_trailing_text_lines(self) -> None:
        result = parse_json_safe(
            '{"result": "ok"}\n\nSome random text\nMore text'
        )
        assert result is not None
        assert result["result"] == "ok"

    def test_empty_string(self) -> None:
        assert parse_json_safe("") is None

    def test_whitespace_only(self) -> None:
        assert parse_json_safe("   \n\t  ") is None

    def test_non_json(self) -> None:
        assert parse_json_safe("this is not json at all") is None

    def test_malformed_json(self) -> None:
        assert parse_json_safe('{"key": "value",}') is None

    def test_nested_json(self) -> None:
        result = parse_json_safe(
            '{"outer": {"inner": [1, 2, 3]}, "flag": true}'
        )
        assert result is not None
        assert result["outer"]["inner"] == [1, 2, 3]

    def test_json_with_braces_in_strings(self) -> None:
        result = parse_json_safe('{"code": "if (x) { return }"}')
        assert result is not None
        assert "return" in result["code"]

    def test_claude_error_response(self) -> None:
        """Claude CLI returns is_error=true with valid JSON."""
        result = parse_json_safe(
            '{"type":"result","is_error":true,"result":"Not logged in"}'
        )
        assert result is not None
        assert result["is_error"] is True
        assert result["result"] == "Not logged in"
