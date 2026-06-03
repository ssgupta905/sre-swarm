"""Splunk MCP tools.

Talks to either real Splunk Enterprise/Cloud or the demo `log_collector`
service — both speak the same `Authorization: Splunk <token>` REST flow. The
agent gets a small, opinionated surface: free-text search, group-by-stats, and
a recent-errors shortcut.

Configured at startup via `configure_splunk(url, token, timeout_s)`. All tools
share a module-level client so the orchestrator can swap targets per run.
"""
from __future__ import annotations

import json
from typing import Optional

import httpx

from . import tool


_SPLUNK_URL: str = "http://127.0.0.1:8088"
_SPLUNK_TOKEN: str = "demo-hec-token"
_SPLUNK_TIMEOUT: float = 5.0


def configure_splunk(url: str, token: str, timeout_s: float = 5.0) -> None:
    """Set the Splunk base URL, HEC token, and timeout."""
    global _SPLUNK_URL, _SPLUNK_TOKEN, _SPLUNK_TIMEOUT
    _SPLUNK_URL = url.rstrip("/")
    _SPLUNK_TOKEN = token
    _SPLUNK_TIMEOUT = float(timeout_s)


def _err(message: str) -> dict:
    return {"isError": True, "content": [{"type": "text", "text": message}]}


def _ok(payload) -> dict:
    text = json.dumps(payload) if isinstance(payload, (dict, list)) else str(payload)
    return {"content": [{"type": "text", "text": text}], "data": payload}


class _SplunkOffline(Exception):
    """Splunk backend is unreachable — caller should return an empty result
    instead of surfacing a noisy connection error to the LLM."""


async def _get(path: str, params: dict) -> dict:
    url = f"{_SPLUNK_URL}{path}"
    headers = {"Authorization": f"Splunk {_SPLUNK_TOKEN}"}
    try:
        async with httpx.AsyncClient(timeout=_SPLUNK_TIMEOUT) as client:
            resp = await client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            return resp.json()
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
        raise _SplunkOffline(str(exc)) from exc


def _empty(kind: str) -> dict:
    return _ok({"events": [], "facets": {}, "note": f"{kind}: no log backend available"})


def _normalize_service(name: Optional[str]) -> Optional[str]:
    """Logs are emitted with the short service name ("payments") but agents see
    the long form ("payments-service") in the incident event. Strip the suffix
    so a `service=...` filter actually matches what the collector stored."""
    if not name:
        return name
    return name[:-len("-service")] if name.endswith("-service") else name


@tool(
    "splunk_search",
    "Free-text search across service logs (FTS5 MATCH). "
    "Returns most-recent events matching the query and facets.",
    {
        "q": Optional[str],
        "service": Optional[str],
        "level": Optional[str],
        "correlation_id": Optional[str],
        "minutes": Optional[int],
        "limit": Optional[int],
    },
)
async def splunk_search(args: dict) -> dict:
    try:
        params: dict = {
            "minutes": int(args.get("minutes") or 15),
            "limit": int(args.get("limit") or 50),
        }
        if args.get("q"):
            params["q"] = args["q"]
        if args.get("service"):
            params["service"] = _normalize_service(args["service"])
        if args.get("level"):
            params["level"] = args["level"]
        if args.get("correlation_id"):
            params["correlation_id"] = args["correlation_id"]
        data = await _get("/api/search", params)
        return _ok(data)
    except _SplunkOffline:
        return _empty("splunk_search")
    except Exception as exc:  # noqa: BLE001
        return _err(f"splunk_search failed: {exc}")


@tool(
    "splunk_trace",
    "Return every log event for a single correlation_id, ordered "
    "chronologically. Use this to read one request's path across services.",
    {"correlation_id": str, "minutes": Optional[int]},
)
async def splunk_trace(args: dict) -> dict:
    try:
        params = {
            "correlation_id": args["correlation_id"],
            "minutes": int(args.get("minutes") or 30),
            "limit": 200,
        }
        data = await _get("/api/search", params)
        return _ok(data)
    except _SplunkOffline:
        return _empty("splunk_trace")
    except Exception as exc:  # noqa: BLE001
        return _err(f"splunk_trace failed: {exc}")


@tool(
    "splunk_stats",
    "Aggregate log counts grouped by a field (level, service, persona, status, "
    "correlation_id, …). Use to spot bursts and outliers fast.",
    {
        "group_by": str,
        "service": Optional[str],
        "minutes": Optional[int],
    },
)
async def splunk_stats(args: dict) -> dict:
    try:
        params: dict = {
            "group_by": args["group_by"],
            "minutes": int(args.get("minutes") or 15),
        }
        if args.get("service"):
            params["service"] = _normalize_service(args["service"])
        data = await _get("/api/stats", params)
        return _ok(data)
    except _SplunkOffline:
        return _empty("splunk_stats")
    except Exception as exc:  # noqa: BLE001
        return _err(f"splunk_stats failed: {exc}")


@tool(
    "splunk_recent_errors",
    "Shortcut: most-recent error+critical log lines for a service in the last N "
    "minutes. Use as the first lookup during RCA.",
    {
        "service": Optional[str],
        "minutes": Optional[int],
        "limit": Optional[int],
    },
)
async def splunk_recent_errors(args: dict) -> dict:
    try:
        params: dict = {
            "minutes": int(args.get("minutes") or 15),
            "limit": int(args.get("limit") or 50),
            "q": "level:error OR level:critical OR error OR exception OR timeout OR 5*",
        }
        if args.get("service"):
            params["service"] = _normalize_service(args["service"])
        # The collector's FTS5 matches on `message`; fall back to a level facet
        # if the MATCH expression yields nothing.
        data = await _get("/api/search", params)
        if data.get("count", 0) == 0:
            params.pop("q", None)
            params["level"] = "error"
            data = await _get("/api/search", params)
        return _ok(data)
    except _SplunkOffline:
        return _empty("splunk_recent_errors")
    except Exception as exc:  # noqa: BLE001
        return _err(f"splunk_recent_errors failed: {exc}")
