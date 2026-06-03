"""Tool registry exposed to every agent.

Read tools are always available; write tools (heal actions, chaos injection)
are gated behind the `include_write` flag and only handed to the Heal agent
after the HITL gate approves the step.

The Ollama tool-calling loop in agents/ollama_client.py reads from the flat
registry returned by `get_tool_registry`. `build_mcp_server` is kept for
optional MCP-based integrations and is a no-op when claude-agent-sdk is absent.
"""
from __future__ import annotations

from typing import Any

try:
    from claude_agent_sdk import create_sdk_mcp_server
except ImportError:  # claude-agent-sdk is optional; MCP server becomes a stub.
    def create_sdk_mcp_server(name: str, version: str, tools: list) -> Any:  # type: ignore[no-redef]
        return {"name": name, "version": version, "tools": tools}

from .code import code_read, code_search, git_blame, git_recent_changes
from .db import db_locks, db_pool_stats, db_slow_queries
from .kubectl import (
    kubectl_describe,
    kubectl_get,
    kubectl_get_events,
    get_deployment_status,
    get_pod_logs,
    get_resource_usage,
)
from .logs import get_recent_log_errors
from .prometheus import list_alerts, prometheus_query, prometheus_range_query
from .splunk import splunk_recent_errors, splunk_search, splunk_stats, splunk_trace

READ_TOOLS = [
    prometheus_query,
    prometheus_range_query,
    list_alerts,
    kubectl_get,
    kubectl_describe,
    kubectl_get_events,
    get_pod_logs,
    get_resource_usage,
    get_deployment_status,
    get_recent_log_errors,
    splunk_search,
    splunk_stats,
    splunk_recent_errors,
    splunk_trace,
    code_search,
    code_read,
    git_recent_changes,
    git_blame,
    db_slow_queries,
    db_locks,
    db_pool_stats,
]


def _load_write_tools() -> list:
    """Write tools are imported lazily so Phase 1 boots without their module."""
    try:
        from .kubectl_write import (
            kubectl_apply_patch,
            kubectl_rollout_restart,
            kubectl_scale,
        )
        from .chaos import (
            chaos_inject,
            chaos_replay_apply,
            chaos_replay_delete,
            chaos_reset,
        )
    except ImportError:
        return []
    return [
        kubectl_rollout_restart,
        kubectl_scale,
        kubectl_apply_patch,
        chaos_inject,
        chaos_reset,
        chaos_replay_apply,
        chaos_replay_delete,
    ]


def build_mcp_server(include_write: bool = False):
    tools = list(READ_TOOLS)
    if include_write:
        tools.extend(_load_write_tools())
    return create_sdk_mcp_server(name="sre-swarm-tools", version="1.0.0", tools=tools)


def get_tool_registry(include_write: bool = False) -> dict:
    """Flat name → callable map for the Ollama tool-calling loop."""
    tools = list(READ_TOOLS)
    if include_write:
        tools.extend(_load_write_tools())
    registry: dict = {}
    for fn in tools:
        name = getattr(fn, "tool_name", fn.__name__)
        registry[name] = fn
    return registry
