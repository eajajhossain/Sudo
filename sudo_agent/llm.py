"""Model providers. The agent loop speaks one neutral message format (Claude's content
blocks); each provider adapts it. Adding a provider means adding one class here."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable

from .config import Settings, get_key
from .control import TaskControl


class LLMError(Exception):
    pass


class ConfigError(LLMError):
    pass


@dataclass
class LLMResponse:
    blocks: list[dict]
    stop_reason: str
    usage: dict = field(default_factory=dict)

    @property
    def tool_uses(self) -> list[dict]:
        return [b for b in self.blocks if b.get("type") == "tool_use"]

    @property
    def text(self) -> str:
        return "".join(b.get("text", "") for b in self.blocks if b.get("type") == "text")


class Provider:
    name = "base"

    def complete(self, system: str, tools: list[dict], messages: list[dict], control: TaskControl,
                 on_text: Callable[[str], None]) -> LLMResponse:
        raise NotImplementedError


class AnthropicProvider(Provider):
    name = "anthropic"
    # History stays append-only; old tool results and screenshots are cleared server-side.
    _BETAS = ["context-management-2025-06-27"]
    _FALLBACK_BETA = "server-side-fallback-2026-07-01"

    def __init__(self, settings: Settings, api_key: str, base_url: str | None = None):
        import anthropic

        self._sdk = anthropic
        self.client = anthropic.Anthropic(api_key=api_key, base_url=base_url, max_retries=4, timeout=600.0)
        self.settings = settings

    def complete(self, system, tools, messages, control, on_text):
        sdk = self._sdk
        model = self.settings.model
        betas = list(self._BETAS)
        extra: dict = {}
        if any(model.startswith(p) for p in ("claude-opus-5", "claude-fable-5", "claude-sonnet-5-5")):
            betas.append(self._FALLBACK_BETA)
            extra["fallbacks"] = "default"
        wire_tools = [{**t, "eager_input_streaming": True} for t in tools]
        try:
            with self.client.beta.messages.stream(
                model=model,
                max_tokens=32000,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                tools=wire_tools,
                messages=messages,
                output_config={"effort": self.settings.effort},
                context_management={"edits": [{"type": "clear_tool_uses_20250919"}]},
                betas=betas,
                **extra,
            ) as stream:
                for event in stream:
                    control.raise_if_stopped()
                    if event.type == "text":
                        on_text(event.text)
                final = stream.get_final_message()
        except sdk.AuthenticationError as exc:
            raise LLMError("The API key was rejected. Check it in Settings.") from exc
        except sdk.PermissionDeniedError as exc:
            raise LLMError(f"This key cannot use model {model}.") from exc
        except sdk.NotFoundError as exc:
            raise LLMError(f"Model {model} was not found.") from exc
        except sdk.RateLimitError as exc:
            raise LLMError("Rate limit hit even after retries. Wait and try again.") from exc
        except sdk.APIStatusError as exc:
            raise LLMError(f"The AI service returned {exc.status_code}: {exc.message}") from exc
        except sdk.APIConnectionError as exc:
            raise LLMError("Could not reach the AI service. Check the internet connection.") from exc
        blocks = [b.model_dump(mode="json", exclude_none=True) for b in final.content]
        return LLMResponse(blocks, final.stop_reason or "", final.usage.model_dump(mode="json", exclude_none=True))


class OpenAICompatibleProvider(Provider):
    """OpenAI, Groq, OpenRouter, local servers: anything with chat completions and tools."""

    name = "openai"
    KEEP_IMAGES = 2
    KEEP_RESULTS = 40

    def __init__(self, settings: Settings, api_key: str):
        import openai

        self._sdk = openai
        self.client = openai.OpenAI(api_key=api_key, base_url=settings.openai_base_url or None, max_retries=4, timeout=600.0)
        self.settings = settings

    @classmethod
    def convert(cls, system: str, messages: list[dict]) -> list[dict]:
        total_images = sum(1 for m in messages for b in _iter_blocks(m) if b.get("type") == "image")
        total_results = sum(1 for m in messages for b in _iter_blocks(m) if b.get("type") == "tool_result")
        seen_images = seen_results = 0
        out: list[dict] = [{"role": "system", "content": system}]
        for message in messages:
            content = message["content"]
            if isinstance(content, str):
                out.append({"role": message["role"], "content": content})
                continue
            if message["role"] == "assistant":
                text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
                calls = [{"id": b["id"], "type": "function",
                          "function": {"name": b["name"], "arguments": json.dumps(b["input"], ensure_ascii=False)}}
                         for b in content if b.get("type") == "tool_use"]
                entry: dict = {"role": "assistant", "content": text or None}
                if calls:
                    entry["tool_calls"] = calls
                out.append(entry)
                continue
            follow_up: list[dict] = []
            for block in content:
                kind = block.get("type")
                if kind == "text":
                    follow_up.append({"type": "text", "text": block["text"]})
                elif kind == "tool_result":
                    seen_results += 1
                    parts = block.get("content", "")
                    if isinstance(parts, str):
                        parts = [{"type": "text", "text": parts}]
                    text = "\n".join(p["text"] for p in parts if p.get("type") == "text")
                    if total_results - seen_results >= cls.KEEP_RESULTS:
                        text = "[older result cleared]"
                    out.append({"role": "tool", "tool_call_id": block["tool_use_id"], "content": text or "(no output)"})
                    for part in parts:
                        if part.get("type") == "image":
                            seen_images += 1
                            if total_images - seen_images < cls.KEEP_IMAGES:
                                follow_up.append(_image_part(part))
            if follow_up:
                out.append({"role": "user", "content": follow_up})
        return out

    def complete(self, system, tools, messages, control, on_text):
        sdk = self._sdk
        wire_tools = [{"type": "function", "function": {
            "name": t["name"], "description": t["description"], "parameters": t["input_schema"]}} for t in tools]
        text: list[str] = []
        calls: dict[int, dict] = {}
        finish = ""
        try:
            stream = self.client.chat.completions.create(
                model=self.settings.model, messages=self.convert(system, messages), tools=wire_tools, stream=True)
            for chunk in stream:
                control.raise_if_stopped()
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                finish = choice.finish_reason or finish
                delta = choice.delta
                if delta.content:
                    text.append(delta.content)
                    on_text(delta.content)
                for call in delta.tool_calls or []:
                    slot = calls.setdefault(call.index, {"id": "", "name": "", "args": ""})
                    slot["id"] = call.id or slot["id"]
                    if call.function:
                        slot["name"] = call.function.name or slot["name"]
                        slot["args"] += call.function.arguments or ""
        except sdk.AuthenticationError as exc:
            raise LLMError("The API key was rejected. Check it in Settings.") from exc
        except sdk.APIStatusError as exc:
            raise LLMError(f"The AI service returned {exc.status_code}: {exc.message}") from exc
        except sdk.APIConnectionError as exc:
            raise LLMError("Could not reach the AI service. Check the internet connection and base URL.") from exc
        blocks: list[dict] = []
        if text:
            blocks.append({"type": "text", "text": "".join(text)})
        for _, slot in sorted(calls.items()):
            try:
                parsed = json.loads(slot["args"] or "{}")
            except ValueError:
                parsed = {"_invalid_json": slot["args"][:200]}
            blocks.append({"type": "tool_use", "id": slot["id"], "name": slot["name"], "input": parsed})
        reason = {"tool_calls": "tool_use", "stop": "end_turn", "length": "max_tokens", "content_filter": "refusal"}.get(finish, finish)
        return LLMResponse(blocks, reason)


def _iter_blocks(message: dict):
    content = message["content"]
    if isinstance(content, str):
        return
    for block in content:
        if block.get("type") == "tool_result" and isinstance(block.get("content"), list):
            yield block
            yield from block["content"]
        else:
            yield block


def _image_part(part: dict) -> dict:
    source = part["source"]
    return {"type": "image_url", "image_url": {"url": f"data:{source['media_type']};base64,{source['data']}"}}


class OpenAIResponsesProvider(Provider):
    """OpenAI's own API. Its current models need the Responses API for tool calling.

    Nothing is stored server-side. Each turn's raw output, including encrypted reasoning,
    is replayed unchanged on the next request."""

    name = "openai"
    KEEP_IMAGES = 2
    KEEP_RESULTS = 40
    EFFORT = {"max": "xhigh"}

    def __init__(self, settings: Settings, api_key: str, base_url: str | None = None):
        import openai

        self._sdk = openai
        self.client = openai.OpenAI(api_key=api_key, base_url=base_url, max_retries=4, timeout=600.0)
        self.settings = settings

    @staticmethod
    def is_reasoning_model(model: str) -> bool:
        """Older models such as gpt-4o-mini reject the reasoning settings."""
        return not model.lower().startswith(("gpt-4", "gpt-3.5", "chatgpt-4"))

    @classmethod
    def convert(cls, messages: list[dict]) -> list[dict]:
        total_images = sum(1 for m in messages for b in _iter_blocks(m) if b.get("type") == "image")
        total_results = sum(1 for m in messages for b in _iter_blocks(m) if b.get("type") == "tool_result")
        seen_images = seen_results = 0
        out: list[dict] = []
        for message in messages:
            content = message["content"]
            if isinstance(content, str):
                out.append({"role": message["role"], "content": content})
                continue
            if message["role"] == "assistant":
                raw = next((b for b in content if b.get("type") == "openai_raw"), None)
                if raw:
                    out.extend(raw["items"])
                    continue
                for block in content:
                    if block.get("type") == "text":
                        out.append({"role": "assistant", "content": block["text"]})
                    elif block.get("type") == "tool_use":
                        out.append({"type": "function_call", "call_id": block["id"], "name": block["name"],
                                    "arguments": json.dumps(block["input"], ensure_ascii=False)})
                continue
            follow_up: list[dict] = []
            for block in content:
                kind = block.get("type")
                if kind == "text":
                    follow_up.append({"type": "input_text", "text": block["text"]})
                elif kind == "tool_result":
                    seen_results += 1
                    parts = block.get("content", "")
                    if isinstance(parts, str):
                        parts = [{"type": "text", "text": parts}]
                    text = "\n".join(p["text"] for p in parts if p.get("type") == "text")
                    if total_results - seen_results >= cls.KEEP_RESULTS:
                        text = "[older result cleared]"
                    out.append({"type": "function_call_output", "call_id": block["tool_use_id"], "output": text or "(no output)"})
                    for part in parts:
                        if part.get("type") == "image":
                            seen_images += 1
                            if total_images - seen_images < cls.KEEP_IMAGES:
                                source = part["source"]
                                follow_up.append({"type": "input_image",
                                                  "image_url": f"data:{source['media_type']};base64,{source['data']}"})
            if follow_up:
                out.append({"role": "user", "content": follow_up})
        return out

    def complete(self, system, tools, messages, control, on_text):
        sdk = self._sdk
        wire_tools = [{"type": "function", "name": t["name"], "description": t["description"],
                       "parameters": t["input_schema"], "strict": False} for t in tools]
        final = None
        extra: dict = {}
        if self.is_reasoning_model(self.settings.model):
            extra = {"reasoning": {"effort": self.EFFORT.get(self.settings.effort, self.settings.effort)},
                     "include": ["reasoning.encrypted_content"]}
        try:
            stream = self.client.responses.create(
                model=self.settings.model, instructions=system, input=self.convert(messages), tools=wire_tools,
                tool_choice="auto", store=False, stream=True, **extra)
            for event in stream:
                control.raise_if_stopped()
                if event.type == "response.output_text.delta":
                    on_text(event.delta)
                elif event.type in ("response.completed", "response.incomplete", "response.failed"):
                    final = event.response
        except sdk.AuthenticationError as exc:
            raise LLMError("The API key was rejected. Check it in Settings.") from exc
        except sdk.PermissionDeniedError as exc:
            raise LLMError(f"This key cannot use model {self.settings.model}.") from exc
        except sdk.NotFoundError as exc:
            raise LLMError(f"Model {self.settings.model} was not found.") from exc
        except sdk.RateLimitError as exc:
            raise LLMError("Rate limit or out of credit, even after retries. Check your OpenAI billing.") from exc
        except sdk.APIStatusError as exc:
            raise LLMError(f"The AI service returned {exc.status_code}: {exc.message}") from exc
        except sdk.APIConnectionError as exc:
            raise LLMError("Could not reach the AI service. Check the internet connection.") from exc
        if final is None:
            raise LLMError("The AI service ended the reply early.")
        if final.status == "failed":
            raise LLMError(f"The AI service failed: {final.error.message if final.error else 'unknown error'}")
        raw_items, blocks, refused = [], [], False
        for item in final.output:
            dumped = item.model_dump(mode="json", exclude_none=True)
            dumped.pop("parsed_arguments", None)
            raw_items.append(dumped)
            if item.type == "message":
                for part in item.content:
                    if part.type == "output_text":
                        blocks.append({"type": "text", "text": part.text})
                    elif part.type == "refusal":
                        refused = True
            elif item.type == "function_call":
                try:
                    parsed = json.loads(item.arguments or "{}")
                except ValueError:
                    parsed = {"_invalid_json": (item.arguments or "")[:200]}
                blocks.append({"type": "tool_use", "id": item.call_id, "name": item.name, "input": parsed})
        reason = "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn"
        if final.status == "incomplete":
            cause = final.incomplete_details.reason if final.incomplete_details else ""
            reason = "refusal" if cause == "content_filter" else "max_tokens"
        if refused:
            reason = "refusal"
        return LLMResponse([{"type": "openai_raw", "items": raw_items}, *blocks], reason)


class ScriptedProvider(Provider):
    """Replays fixed turns. Lets tests drive the real loop without a model or a key."""

    name = "scripted"

    def __init__(self, turns: list):
        self.turns = list(turns)
        self.calls: list[list[dict]] = []
        self._next_id = 0

    def complete(self, system, tools, messages, control, on_text):
        self.calls.append(json.loads(json.dumps(messages)))
        control.raise_if_stopped()
        if not self.turns:
            return LLMResponse([{"type": "text", "text": "Script exhausted."}], "end_turn")
        turn = self.turns.pop(0)
        if callable(turn):
            turn = turn(messages)
        blocks = []
        for item in turn if isinstance(turn, list) else [turn]:
            if isinstance(item, str):
                on_text(item)
                blocks.append({"type": "text", "text": item})
            else:
                name, args = item
                self._next_id += 1
                blocks.append({"type": "tool_use", "id": f"call_{self._next_id}", "name": name, "input": args})
        return LLMResponse(blocks, "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn")


def build_provider(settings: Settings) -> Provider:
    key = get_key(settings.provider)
    if not key:
        if settings.provider == "openai" and settings.openai_base_url:
            key = "not-needed"  # Local servers often take no key.
        else:
            raise ConfigError(f"No API key for {settings.provider}. Add one in Settings.")
    if settings.provider == "openai":
        if settings.openai_base_url and "api.openai.com" not in settings.openai_base_url:
            return OpenAICompatibleProvider(settings, key)
        return OpenAIResponsesProvider(settings, key)
    return AnthropicProvider(settings, key)


def list_models(settings: Settings) -> list[str]:
    """Model IDs the saved key can use, for the Settings dropdown."""
    provider = build_provider(settings)
    return sorted(model.id for model in provider.client.models.list())
