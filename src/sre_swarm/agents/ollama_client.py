"""Ollama-backed LLM client with a tool-calling loop.

Replaces the claude-agent-sdk runtime so the swarm can use a locally-hosted
free model (e.g. llama3.1, qwen2.5) via http://localhost:11434/api/chat.

The agent tools are still the same `@tool`-decorated coroutines defined in
`sre_swarm.tools.*`. We read their `.tool_name`, `.tool_description`, and
`.tool_schema` attributes, translate the schema into OpenAI-style JSON Schema,
hand the spec to Ollama, and execute the tool calls Ollama returns until the
model emits a final assistant message with no further tool calls.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import typing
from typing import Any, Awaitable, Callable, Optional

import httpx


ToolFn = Callable[[dict], Awaitable[dict]]


_PRIMITIVES = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    dict: "object",
    list: "array",
}


def _python_type_to_json_schema(py_type: Any) -> tuple[dict, bool]:
    """Return (json_schema_fragment, required) for a Python type hint.

    `required` is False if the type is Optional/Union[..., None].
    """
    required = True
    origin = typing.get_origin(py_type)
    args = typing.get_args(py_type)

    if origin is typing.Union:
        non_none = [a for a in args if a is not type(None)]  # noqa: E721
        if len(non_none) != len(args):
            required = False
        if len(non_none) == 1:
            inner, _ = _python_type_to_json_schema(non_none[0])
            return inner, required
        return {"type": "string"}, required

    if origin in (list, typing.List):
        item_schema = {"type": "string"}
        if args:
            item_schema, _ = _python_type_to_json_schema(args[0])
        return {"type": "array", "items": item_schema}, required

    if origin in (dict, typing.Dict):
        return {"type": "object"}, required

    if py_type in _PRIMITIVES:
        return {"type": _PRIMITIVES[py_type]}, required

    return {"type": "string"}, required


def tool_to_openai_spec(fn: ToolFn) -> dict:
    """Build an OpenAI-style tool spec from a @tool-decorated callable."""
    name = getattr(fn, "tool_name", fn.__name__)
    description = getattr(fn, "tool_description", "") or ""
    raw_schema = getattr(fn, "tool_schema", {}) or {}
    properties: dict[str, dict] = {}
    required: list[str] = []
    for prop_name, prop_type in raw_schema.items():
        schema, is_required = _python_type_to_json_schema(prop_type)
        properties[prop_name] = schema
        if is_required:
            required.append(prop_name)
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


def _stringify_tool_result(result: Any) -> str:
    """Compact a tool result dict into a string the LLM can read."""
    if isinstance(result, dict):
        content = result.get("content")
        if isinstance(content, list) and content:
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(str(item.get("text", "")))
            if parts:
                return "\n".join(parts)
        try:
            return json.dumps(result, default=str)
        except (TypeError, ValueError):
            return str(result)
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result, default=str)
    except (TypeError, ValueError):
        return str(result)


def _coerce_arguments(raw: Any) -> dict:
    """Ollama returns arguments as a dict, but some models emit a JSON string."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass
    return {}


class OllamaClient:
    """Thin async wrapper around the Ollama /api/chat endpoint."""

    def __init__(
        self,
        url: str = "http://localhost:11434",
        model: str = "llama3.1",
        timeout_s: float = 180.0,
        keep_alive: str = "10m",
    ) -> None:
        self.url = url.rstrip("/")
        self.model = model
        self.timeout_s = float(timeout_s)
        self.keep_alive = keep_alive

    async def chat(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        format_json: bool = False,
    ) -> dict:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "keep_alive": self.keep_alive,
            "options": {"temperature": 0.2},
        }
        if tools:
            payload["tools"] = tools
        if format_json:
            payload["format"] = "json"
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            resp = await client.post(f"{self.url}/api/chat", json=payload)
            resp.raise_for_status()
            return resp.json()


async def run_tool_loop(
    client: OllamaClient,
    system_prompt: str,
    user_prompt: str,
    tools: list[ToolFn],
    on_tool_call: Optional[Callable[[str, str, dict], Awaitable[None]]] = None,
    on_tool_result: Optional[Callable[[str, Any, bool], Awaitable[None]]] = None,
    on_message: Optional[Callable[[str], Awaitable[None]]] = None,
    max_turns: int = 12,
) -> str:
    """Drive Ollama through a tool-calling loop until it returns a final message.

    Returns the final assistant content string (may be empty if the model only
    ever emitted tool calls, in which case the caller's _parse_json will fall
    back to a stub-style empty response).
    """
    tool_by_name = {getattr(fn, "tool_name", fn.__name__): fn for fn in tools}
    tool_specs = [tool_to_openai_spec(fn) for fn in tools]

    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    final_text = ""
    for turn in range(max_turns):
        resp = await client.chat(messages, tools=tool_specs if tool_specs else None)
        msg = resp.get("message") or {}
        content = msg.get("content") or ""
        tool_calls = msg.get("tool_calls") or []

        # Always record what the model said this turn so the conversation stays coherent.
        assistant_record: dict[str, Any] = {"role": "assistant", "content": content}
        if tool_calls:
            assistant_record["tool_calls"] = tool_calls
        messages.append(assistant_record)

        if content:
            final_text = content
            if on_message is not None:
                await on_message(content)

        if not tool_calls:
            break

        for idx, tc in enumerate(tool_calls):
            fn_block = tc.get("function") or {}
            name = fn_block.get("name") or ""
            args = _coerce_arguments(fn_block.get("arguments"))
            tool_id = tc.get("id") or f"call_{turn}_{idx}"

            if on_tool_call is not None:
                await on_tool_call(tool_id, name, args)

            handler = tool_by_name.get(name)
            if handler is None:
                result: dict = {
                    "isError": True,
                    "content": [{"type": "text", "text": f"unknown tool: {name}"}],
                }
            else:
                try:
                    result = await handler(args)
                except Exception as exc:  # noqa: BLE001
                    result = {
                        "isError": True,
                        "content": [{"type": "text", "text": f"{name} raised: {exc!r}"}],
                    }

            is_error = bool(isinstance(result, dict) and result.get("isError"))
            if on_tool_result is not None:
                await on_tool_result(tool_id, result, is_error)

            messages.append({
                "role": "tool",
                "name": name,
                "content": _stringify_tool_result(result),
            })
    else:
        if os.environ.get("SRE_SWARM_DEBUG"):
            sys.stderr.write(
                f"[debug] ollama tool loop hit max_turns={max_turns} without final answer\n"
            )

    # llama3.1 and many small open models emit tool calls as JSON-in-content instead of
    # using the native tool_calls channel. When that happens, final_text is a stray tool
    # invocation, not the answer. Force a strict-JSON synthesis turn so we always close
    # the loop with a parseable structured response.
    #
    # Small models also tend to summarize tool results in prose ("The log entries are
    # mostly related to..."). Treat any final_text that isn't a JSON object as needing
    # synthesis — the system prompt requires JSON, anything else is a contract miss.
    if (
        _looks_like_tool_invocation(final_text)
        or not final_text.strip()
        or not _looks_like_json_object(final_text)
    ):
        synthesis_messages = list(messages) + [{
            "role": "user",
            "content": (
                "Produce the FINAL ANSWER as strict JSON only, matching the schema "
                "specified in the system prompt. Do NOT call any more tools. Do NOT "
                "include prose, markdown, or code fences. Reply with ONE JSON object "
                "and nothing else."
            ),
        }]
        try:
            resp = await client.chat(synthesis_messages, tools=None, format_json=True)
            content = (resp.get("message") or {}).get("content") or ""
            if content:
                final_text = content
                if on_message is not None:
                    await on_message(content)
        except Exception as exc:  # noqa: BLE001
            if os.environ.get("SRE_SWARM_DEBUG"):
                sys.stderr.write(f"[debug] synthesis turn failed: {exc!r}\n")

    return final_text


_TOOL_INVOCATION_KEYS = ('"name"', '"parameters"', '"arguments"')


def _looks_like_json_object(text: str) -> bool:
    """True only if text looks like a single JSON object (the answer schema).

    A leading `{` plus a trailing `}` is the cheap check small models reliably
    miss — they emit summaries like "The log entries are mostly related to…"
    that bypass the synthesis fallback unless we treat non-JSON as a miss.
    """
    s = (text or "").strip()
    return s.startswith("{") and s.endswith("}")


def _looks_like_tool_invocation(text: str) -> bool:
    """True if the model emitted a tool-call shape instead of the answer schema.

    Some open models wrap the spurious tool call in prose ("Here's the function
    call..."), so scan the whole text rather than only its leading character.
    """
    if not text:
        return False
    if '"name"' not in text:
        return False
    return any(k in text for k in ('"parameters"', '"arguments"'))


async def health_check(url: str, timeout_s: float = 5.0) -> bool:
    """Quick probe so the CLI can warn the operator if Ollama is offline."""
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.get(f"{url.rstrip('/')}/api/tags")
            return resp.status_code == 200
    except (httpx.HTTPError, asyncio.TimeoutError):
        return False
