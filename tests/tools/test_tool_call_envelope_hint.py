"""The tool_call bridge restates the expected envelope when calls is not valid JSON, so a small model that wrote
a call with no name and an unclosed brace does not repeat the same broken call."""
import json

from tools.tool_search_validation import normalize_tool_call_entries


def test_bad_json_calls_string_gets_the_expected_shape_in_the_error():
    bad = {"calls": "[{\"arguments\": {\"content\": \"test\", \"kind\": \"markdown\"}]"}
    entries, error = normalize_tool_call_entries(bad)
    assert entries == []
    assert "is not valid JSON" in error
    assert "{\"calls\":[{\"name\":\"<tool>\",\"arguments\":{...}}]}" in error


def test_valid_array_and_valid_json_string_are_unchanged():
    call = [{"name": "terminal", "arguments": {"command": "ls"}}]
    assert normalize_tool_call_entries({"calls": call}) == (call, None)
    assert normalize_tool_call_entries({"calls": json.dumps(call)}) == (call, None)
