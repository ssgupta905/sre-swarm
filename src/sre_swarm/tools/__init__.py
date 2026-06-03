"""Shared tool decorator used by every MCP tool module.

This used to be sourced from `claude_agent_sdk.tool`, but the swarm now drives
agents through a local Ollama backend that calls these functions directly. The
decorator simply attaches metadata (name, description, schema) that the Ollama
tool-calling loop converts into an OpenAI-style function spec.
"""
from __future__ import annotations

from typing import Any, Callable


def tool(name: str, description: str, schema: dict[str, Any]) -> Callable:
    """Attach LLM-facing metadata to an async tool function."""

    def decorator(fn: Callable) -> Callable:
        fn.tool_name = name  # type: ignore[attr-defined]
        fn.tool_description = description  # type: ignore[attr-defined]
        fn.tool_schema = schema  # type: ignore[attr-defined]
        return fn

    return decorator
