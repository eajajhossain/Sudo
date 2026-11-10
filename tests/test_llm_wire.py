"""Checks the exact HTTP each provider sends and parses, against a local fake endpoint.
This proves the request and response handling; it does not call a real model."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from sudo_agent.config import Settings
from sudo_agent.control import Stopped, TaskControl
from sudo_agent.llm import AnthropicProvider, LLMError, OpenAICompatibleProvider

TOOLS = [{"name": "file_read", "description": "d", "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}]


def sse(events):
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


ANTHROPIC_EVENTS = [
    {"type": "message_start", "message": {"id": "m1", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "content": [],
                                          "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 3, "output_tokens": 1}}},
    {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig123"}},
    {"type": "content_block_stop", "index": 0},
    {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Looking"}},
    {"type": "content_block_stop", "index": 1},
    {"type": "content_block_start", "index": 2, "content_block": {"type": "tool_use", "id": "toolu_1", "name": "file_read", "input": {}}},
    {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": "{\"path\": \"a.txt\"}"}},
    {"type": "content_block_stop", "index": 2},
    {"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None}, "usage": {"output_tokens": 9}},
    {"type": "message_stop"},
]

OPENAI_CHUNKS = [
    {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": "m", "choices": [{"index": 0, "delta": {"role": "assistant", "content": "Hi"}, "finish_reason": None}]},
    {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": "m", "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call_1", "type": "function", "function": {"name": "file_read", "arguments": "{\"pa"}}]}, "finish_reason": None}]},
    {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": "m", "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": "th\": \"a\"}"}}]}, "finish_reason": None}]},
    {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": "m", "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
]


@pytest.fixture
def endpoint():
    state = {"requests": [], "status": 200}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            state["requests"].append({"path": self.path, "headers": dict(self.headers), "body": body})
            if state["status"] != 200:
                payload = json.dumps({"type": "error", "error": {"type": "authentication_error", "message": "bad key"}}).encode()
                self.send_response(state["status"])
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            if "chat/completions" in self.path:
                payload = "".join(f"data: {json.dumps(c)}\n\n" for c in OPENAI_CHUNKS).encode() + b"data: [DONE]\n\n"
            else:
                payload = sse(ANTHROPIC_EVENTS)
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state["url"] = f"http://127.0.0.1:{server.server_address[1]}"
    yield state
    server.shutdown()


MESSAGES = [{"role": "user", "content": [{"type": "text", "text": "go"}]}]


def test_anthropic_request_and_response(endpoint):
    provider = AnthropicProvider(Settings(), "test-key", base_url=endpoint["url"])
    texts = []
    response = provider.complete("SYS", TOOLS, MESSAGES, TaskControl(), texts.append)
    request = endpoint["requests"][0]
    body, headers = request["body"], {k.lower(): v for k, v in request["headers"].items()}
    assert body["model"] == "claude-opus-5-5" and body["stream"] is True
    assert body["output_config"] == {"effort": "high"} and body["fallbacks"] == "default"
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert body["tools"][0]["eager_input_streaming"] is True
    assert body["context_management"]["edits"][0]["type"] == "clear_tool_uses_20250919"
    assert "server-side-fallback-2026-07-01" in headers["anthropic-beta"]
    assert "thinking" not in body and "temperature" not in body
    assert texts == ["Looking"] and response.stop_reason == "tool_use"
    assert response.tool_uses[0]["input"] == {"path": "a.txt"}
    assert response.blocks[0]["type"] == "thinking" and response.blocks[0]["signature"] == "sig123"
    json.dumps(response.blocks)


def test_anthropic_bad_key_is_a_clear_error(endpoint):
    endpoint["status"] = 401
    provider = AnthropicProvider(Settings(), "wrong", base_url=endpoint["url"])
    with pytest.raises(LLMError, match="rejected"):
        provider.complete("S", TOOLS, MESSAGES, TaskControl(), lambda t: None)


def test_anthropic_stop_interrupts_the_stream(endpoint):
    provider = AnthropicProvider(Settings(), "k", base_url=endpoint["url"])
    control = TaskControl()
    control.stop()
    with pytest.raises(Stopped):
        provider.complete("S", TOOLS, MESSAGES, control, lambda t: None)


def test_openai_compatible_request_and_response(endpoint):
    settings = Settings(provider="openai", model="m", openai_base_url=endpoint["url"] + "/v1")
    response = OpenAICompatibleProvider(settings, "k").complete("SYS", TOOLS, MESSAGES, TaskControl(), lambda t: None)
    body = endpoint["requests"][0]["body"]
    assert body["messages"][0] == {"role": "system", "content": "SYS"}
    assert body["tools"][0]["function"]["name"] == "file_read"
    assert response.text == "Hi" and response.stop_reason == "tool_use"
    assert response.tool_uses[0]["input"] == {"path": "a"}


def test_openai_conversion_of_a_full_turn():
    image = {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "AAA"}}
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "go"}]},
        {"role": "assistant", "content": [{"type": "thinking", "thinking": "", "signature": "s"},
                                          {"type": "tool_use", "id": "a", "name": "t", "input": {"x": 1}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a", "content": [{"type": "text", "text": "seen"}, image]},
                                     {"type": "text", "text": "[OWNER UPDATE] change"}]},
    ]
    out = OpenAICompatibleProvider.convert("S", messages)
    assert [m["role"] for m in out] == ["system", "user", "assistant", "tool", "user"]
    assert out[2]["tool_calls"][0]["function"]["arguments"] == '{"x": 1}'
    assert out[3] == {"role": "tool", "tool_call_id": "a", "content": "seen"}
    kinds = [p["type"] for p in out[4]["content"]]
    assert sorted(kinds) == ["image_url", "text"]


def test_openai_conversion_keeps_only_recent_images():
    image = {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "AAA"}}
    messages = [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": str(i), "content": [{"type": "text", "text": "t"}, image]}]} for i in range(5)]
    out = OpenAICompatibleProvider.convert("S", messages)
    assert sum(1 for m in out if m["role"] == "user") == OpenAICompatibleProvider.KEEP_IMAGES
