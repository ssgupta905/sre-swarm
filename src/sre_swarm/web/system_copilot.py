"""LLM-backed Copilot for the /system page.

Runs Ollama with a small tool-belt that lets the model inspect and edit the
IntegrationRegistry. The system prompt is deliberately broad: the operator
should be able to ask "why is splunk down?", "summarize my config", "what
does the github card need?", etc., not just issue mechanical commands.
"""
from __future__ import annotations

import json
from typing import Any, Callable

from ..agents.ollama_client import OllamaClient, run_tool_loop
from ..config import Config
from ..tools import tool
from .integrations import IntegrationRegistry


SYSTEM_PROMPT = """You are the SRE Swarm Config Helper — a copilot embedded in the System Components page.

Your job: help the operator inspect, debug, and edit the integrations that
SRE Swarm depends on. The integrations are:

  - prometheus    metrics source (URL probe via /-/healthy)
  - kubernetes    cluster control plane (probed via `kubectl get ns`)
  - splunk        log search (URL probe via /health)
  - github        source-control reads (requires a personal access token; probed via api.github.com/user)
  - ollama        the LLM serving this very chat
  - any number of operator-added custom HTTP integrations

You have tools to:
  list_integrations()              -- snapshot of every card (name, kind, status, detail, last_error)
  test_integration(name)           -- probe one integration and return the fresh result
  update_integration(name, fields) -- patch the persistent config (allowed fields vary per kind)
  add_custom_integration(name, url) -- register a new custom HTTP probe

Rules:
  1. Before answering ANY question about reachability, configuration, or
     errors, call list_integrations() so your answer reflects current state.
  2. If the user wants to test, edit, or add an integration, actually call
     the tool — do not just describe what you would do.
  3. When an integration is unreachable, read its `last_error` and explain
     the likely cause in one or two sentences (auth, wrong URL, missing
     token, host down, etc.).
  4. When the user gives a token or URL, call update_integration immediately,
     then test_integration to confirm.
  5. Respond in plain text. Short bullet lists are fine. No JSON, no code
     fences, no markdown headers. You're talking to a human SRE in a chat
     bubble — be terse, technical, and useful.
"""


def _ok(text: str, data: Any = None) -> dict:
    body: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if data is not None:
        body["data"] = data
    return body


def _err(text: str) -> dict:
    return {"isError": True, "content": [{"type": "text", "text": text}]}


def build_tools(registry: IntegrationRegistry) -> list[Callable]:
    """Wrap the registry as @tool-decorated coroutines for the Ollama loop."""

    @tool(
        name="list_integrations",
        description="Return a snapshot of every integration (name, kind, status, detail, last_error, last_tested_at).",
        schema={},
    )
    async def list_integrations(args: dict) -> dict:
        cards = registry.list()
        return _ok(json.dumps(cards, default=str), data=cards)

    @tool(
        name="test_integration",
        description="Probe one integration end-to-end and return the fresh result. Use this when the user asks if something is up, reachable, or working.",
        schema={"name": str},
    )
    async def test_integration(args: dict) -> dict:
        name = str(args.get("name", "")).strip().lower()
        if not name:
            return _err("missing 'name'")
        # Case-insensitive lookup.
        match = next((c["name"] for c in registry.list() if c["name"].lower() == name), None)
        if match is None:
            return _err(f"no integration named '{name}'. known: {[c['name'] for c in registry.list()]}")
        result = await registry.test(match)
        return _ok(json.dumps(result, default=str), data=result)

    @tool(
        name="update_integration",
        description=(
            "Patch the persistent config for one integration. `fields` is an object whose keys are limited to the card's editable_fields. "
            "For prometheus: url, timeout_s. For kubernetes: context, namespace. For splunk: url, token. For github: token, api_url. "
            "For ollama: url, model. For custom: url. After updating, the caller should also call test_integration to confirm reachability."
        ),
        schema={"name": str, "fields": dict},
    )
    async def update_integration(args: dict) -> dict:
        name = str(args.get("name", "")).strip().lower()
        fields = args.get("fields") or {}
        if not name:
            return _err("missing 'name'")
        if not isinstance(fields, dict) or not fields:
            return _err("'fields' must be a non-empty object")
        match = next((c["name"] for c in registry.list() if c["name"].lower() == name), None)
        if match is None:
            return _err(f"no integration named '{name}'")
        result = registry.update(match, fields)
        return _ok(json.dumps(result, default=str), data=result)

    @tool(
        name="add_custom_integration",
        description="Register a new custom HTTP integration with a name and URL to probe.",
        schema={"name": str, "url": str},
    )
    async def add_custom_integration(args: dict) -> dict:
        name = str(args.get("name", "")).strip()
        url = str(args.get("url", "")).strip()
        if not name or not url:
            return _err("both 'name' and 'url' are required")
        try:
            result = registry.add_custom(name, url)
            return _ok(json.dumps(result, default=str), data=result)
        except ValueError as exc:
            return _err(str(exc))

    return [list_integrations, test_integration, update_integration, add_custom_integration]


class SystemCopilot:
    """One-turn interface — caller posts a user message, gets the assistant text."""

    def __init__(self, config: Config, registry: IntegrationRegistry) -> None:
        self.config = config
        self.registry = registry
        # Per-session conversation history is held client-side (passed in via `history`).

    async def respond(self, user_message: str, history: list[dict] | None = None) -> dict:
        client = OllamaClient(
            url=self.config.ollama.url,
            model=self.config.ollama.model,
            timeout_s=self.config.ollama.timeout_s,
            keep_alive=self.config.ollama.keep_alive,
        )
        tools = build_tools(self.registry)

        tool_log: list[dict] = []

        async def _on_tool_call(tool_id: str, name: str, args: dict) -> None:
            tool_log.append({"phase": "call", "tool_id": tool_id, "name": name, "args": args})

        async def _on_tool_result(tool_id: str, result: Any, is_error: bool) -> None:
            # Strip the verbose data payload — only keep the human-readable summary.
            summary = ""
            if isinstance(result, dict):
                content = result.get("content") or []
                if content and isinstance(content, list):
                    summary = str(content[0].get("text", ""))[:280]
            tool_log.append({
                "phase": "result", "tool_id": tool_id,
                "is_error": is_error, "summary": summary,
            })

        # Stitch the prior conversation into the user prompt so the LLM has context
        # without us having to thread a multi-turn history through run_tool_loop.
        prompt = user_message
        if history:
            recent = history[-6:]
            transcript = []
            for m in recent:
                role = m.get("role", "user")
                txt = (m.get("text") or "").strip()
                if not txt:
                    continue
                transcript.append(f"[{role}] {txt}")
            if transcript:
                prompt = (
                    "Recent conversation (for context):\n"
                    + "\n".join(transcript)
                    + f"\n\nLatest user message:\n{user_message}"
                )

        text = await run_tool_loop(
            client=client,
            system_prompt=SYSTEM_PROMPT,
            user_prompt=prompt,
            tools=tools,
            on_tool_call=_on_tool_call,
            on_tool_result=_on_tool_result,
            max_turns=8,
        )
        return {"text": text.strip(), "tool_log": tool_log}
