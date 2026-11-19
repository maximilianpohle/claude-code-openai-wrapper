"""OpenAI function calling (tools / tool_choice) emulated via structured output."""

import json

import pytest
from fastapi.testclient import TestClient

import src.auth
import src.main as main
from src import tool_calling

SCHEMA_TOOL = {
    "type": "function",
    "function": {
        "name": "generate_insights_and_discoveries",
        "description": "Return the attack discoveries",
        "parameters": {
            "type": "object",
            "properties": {
                "insights": {"type": "array", "items": {"$ref": "#/$defs/Insight"}},
            },
            "required": ["insights"],
            "$defs": {
                "Insight": {
                    "type": "object",
                    "properties": {"title": {"type": "string"}},
                    "required": ["title"],
                }
            },
        },
    },
}
WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Weather for a city",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
    },
}
FORCED = {"type": "function", "function": {"name": "generate_insights_and_discoveries"}}


@pytest.fixture
def client(monkeypatch):
    if main.limiter:
        monkeypatch.setattr(main.limiter, "enabled", False)
    monkeypatch.setattr(src.auth.auth_manager, "env_api_key", None)
    monkeypatch.setattr(main, "runtime_api_key", None)
    monkeypatch.setattr(main, "validate_claude_code_auth", lambda: (True, {"method": "test"}))
    return TestClient(main.app)


@pytest.fixture
def claude(monkeypatch):
    """Fake Claude: `claude.answer` is what the SDK returns, `claude.calls` the kwargs."""

    class Fake:
        calls = []
        structured = None
        text = None

    async def fake_run_completion(**kwargs):
        Fake.calls.append(kwargs)
        yield {"subtype": "success", "result": Fake.text, "structured_output": Fake.structured}

    Fake.calls = []
    Fake.structured = None
    Fake.text = None
    monkeypatch.setattr(main.claude_cli, "run_completion", fake_run_completion)
    return Fake


def _post(client, **extra):
    body = {
        "model": "claude-sonnet-4-6",
        "messages": [{"role": "user", "content": "Analysiere die Alerts"}],
    }
    body.update(extra)
    return client.post("/v1/chat/completions", json=body)


class TestForcedFunction:
    def test_returns_tool_calls(self, client, claude):
        claude.structured = {
            "tool_calls": [
                {
                    "name": "generate_insights_and_discoveries",
                    "arguments": {"insights": [{"title": "A"}]},
                }
            ]
        }
        r = _post(client, tools=[SCHEMA_TOOL], tool_choice=FORCED)
        assert r.status_code == 200
        choice = r.json()["choices"][0]
        assert choice["finish_reason"] == "tool_calls"
        assert choice["message"]["content"] is None
        call = choice["message"]["tool_calls"][0]
        assert call["type"] == "function" and call["id"].startswith("call_")
        assert call["function"]["name"] == "generate_insights_and_discoveries"
        assert json.loads(call["function"]["arguments"]) == {"insights": [{"title": "A"}]}

    def test_schema_and_prompt_passed_to_claude(self, client, claude):
        claude.structured = {
            "tool_calls": [{"name": SCHEMA_TOOL["function"]["name"], "arguments": {}}]
        }
        _post(client, tools=[SCHEMA_TOOL], tool_choice=FORCED)
        call = claude.calls[0]
        schema = call["output_format"]["schema"]
        assert call["output_format"]["type"] == "json_schema"
        assert schema["required"] == ["tool_calls"]
        assert schema["properties"]["tool_calls"]["minItems"] == 1
        assert schema["properties"]["tool_calls"]["maxItems"] == 1
        assert "Insight" in schema["$defs"]  # $defs hoisted to the schema root
        assert "generate_insights_and_discoveries" in call["system_prompt"]
        # Claude Code's own tools stay off
        assert call["tools"] == [] and call["permission_mode"] is None

    def test_json_in_text_is_accepted(self, client, claude):
        claude.text = '```json\n{"tool_calls": [{"name": "generate_insights_and_discoveries", "arguments": {"insights": []}}]}\n```'
        r = _post(client, tools=[SCHEMA_TOOL], tool_choice=FORCED)
        assert r.json()["choices"][0]["finish_reason"] == "tool_calls"

    def test_no_call_although_required_is_502(self, client, claude):
        claude.text = "Ich weiß nicht"
        r = _post(client, tools=[SCHEMA_TOOL], tool_choice=FORCED)
        assert r.status_code == 502

    def test_unknown_function_is_502(self, client, claude):
        claude.structured = {"tool_calls": [{"name": "rm_rf", "arguments": {}}]}
        assert _post(client, tools=[SCHEMA_TOOL], tool_choice=FORCED).status_code == 502


class TestModelWritesTextInsteadOfToolCall:
    """The CLI demands the StructuredOutput tool; models sometimes answer with plain JSON."""

    def test_json_text_recovered_after_cli_gave_up(self, client, monkeypatch):
        answer = {
            "tool_calls": [
                {"name": "generate_insights_and_discoveries", "arguments": {"insights": []}}
            ]
        }

        async def fake_run_completion(**kwargs):
            yield {"model": "claude-x", "content": [{"type": "text", "text": json.dumps(answer)}]}
            # hook feedback of the CLI (a user message, no "model")
            yield {
                "content": [
                    {
                        "type": "text",
                        "text": "[structured-output-enforce] You MUST call the StructuredOutput tool",
                    }
                ]
            }
            yield {"type": "result", "subtype": "error_during_execution", "is_error": True}

        monkeypatch.setattr(main.claude_cli, "run_completion", fake_run_completion)
        r = _post(client, tools=[SCHEMA_TOOL], tool_choice=FORCED)
        assert r.status_code == 200
        assert r.json()["choices"][0]["finish_reason"] == "tool_calls"

    def test_assistant_texts_skip_hook_messages(self):
        from src.claude_cli import ClaudeCodeCLI

        messages = [
            {"model": "m", "content": [{"type": "text", "text": "A"}]},
            {"content": [{"type": "text", "text": "hook"}]},
            {"model": "m", "content": [{"type": "text", "text": "B"}]},
        ]
        assert ClaudeCodeCLI.assistant_texts(messages) == ["A", "B"]

    def test_instructions_name_structured_output_tool(self):
        spec = tool_calling.build_tool_spec([SCHEMA_TOOL], FORCED)
        assert "StructuredOutput" in spec.instructions


class TestAutoChoice:
    def test_plain_answer(self, client, claude):
        claude.structured = {"content": "Es sind 20 Grad."}
        r = _post(client, tools=[WEATHER_TOOL, SCHEMA_TOOL])
        choice = r.json()["choices"][0]
        assert choice["finish_reason"] == "stop"
        assert choice["message"]["content"] == "Es sind 20 Grad."
        assert not choice["message"]["tool_calls"]

    def test_picks_one_of_several_tools(self, client, claude):
        claude.structured = {
            "tool_calls": [{"name": "get_weather", "arguments": {"city": "Berlin"}}]
        }
        r = _post(client, tools=[WEATHER_TOOL, SCHEMA_TOOL])
        assert (
            r.json()["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "get_weather"
        )
        assert (
            "anyOf"
            in claude.calls[0]["output_format"]["schema"]["properties"]["tool_calls"]["items"]
        )

    def test_tool_choice_none_ignores_tools(self, client, claude):
        claude.text = "Hallo"
        claude.structured = None
        r = _post(client, tools=[WEATHER_TOOL], tool_choice="none")
        assert r.json()["choices"][0]["message"]["content"] == "Hallo"
        assert "output_format" not in claude.calls[0]


class TestConversation:
    def test_tool_result_round_trip_is_accepted(self, client, claude):
        claude.structured = {"content": "Berlin: 20 Grad"}
        messages = [
            {"role": "user", "content": "Wetter in Berlin?"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": '{"city": "Berlin"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": '{"temp": 20}'},
        ]
        r = _post(client, messages=messages, tools=[WEATHER_TOOL])
        assert r.status_code == 200
        prompt = claude.calls[0]["prompt"]
        assert "get_weather" in prompt and "call_1" in prompt and '{"temp": 20}' in prompt


class TestStreaming:
    def test_stream_emits_tool_call_deltas(self, client, claude):
        claude.structured = {
            "tool_calls": [{"name": "get_weather", "arguments": {"city": "Berlin"}}]
        }
        r = _post(client, tools=[WEATHER_TOOL], tool_choice="required", stream=True)
        assert r.status_code == 200
        events = [e[len("data: ") :] for e in r.text.strip().split("\n\n")]
        assert events[-1] == "[DONE]"
        chunks = [json.loads(e) for e in events[:-1]]
        tool_deltas = [
            c["choices"][0]["delta"] for c in chunks if "tool_calls" in c["choices"][0]["delta"]
        ]
        assert tool_deltas[0]["tool_calls"][0]["index"] == 0
        assert tool_deltas[0]["tool_calls"][0]["function"]["name"] == "get_weather"
        assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"


class TestValidation:
    def test_invalid_tool_definition_is_400(self, client, claude):
        assert _post(client, tools=[{"type": "retrieval"}]).status_code == 400

    def test_tool_choice_unknown_function_is_400(self, client, claude):
        r = _post(
            client,
            tools=[WEATHER_TOOL],
            tool_choice={"type": "function", "function": {"name": "x"}},
        )
        assert r.status_code == 400

    def test_no_tools_no_change(self, client, claude):
        claude.text = "Hi"
        r = _post(client)
        assert r.json()["choices"][0]["message"]["content"] == "Hi"


def test_build_tool_spec_without_tools():
    assert tool_calling.build_tool_spec(None, None) is None
    assert tool_calling.build_tool_spec([WEATHER_TOOL], "none") is None


class TestDebugDump:
    def test_dump_files_written_only_when_enabled(self, client, claude, tmp_path, monkeypatch):
        claude.structured = {
            "tool_calls": [{"name": "get_weather", "arguments": {"city": "Berlin"}}]
        }
        _post(client, tools=[WEATHER_TOOL])
        assert list(tmp_path.iterdir()) == []

        monkeypatch.setenv("DEBUG_DUMP_DIR", str(tmp_path))
        r = _post(client, tools=[WEATHER_TOOL])
        assert r.status_code == 200
        suffixes = {p.name.split("_", 2)[2] for p in tmp_path.iterdir()}
        assert suffixes == {"request.json", "claude-input.json", "claude-output.json"}
        request_file = next(p for p in tmp_path.iterdir() if p.name.endswith("_request.json"))
        assert json.loads(request_file.read_text())["tools"][0]["function"]["name"] == "get_weather"
        assert oct(request_file.stat().st_mode & 0o777) == "0o600"
