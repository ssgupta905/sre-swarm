"""Database introspection MCP tools.

Each demo service can expose a tiny read-only `/db/*` endpoint set
(slow_queries / locks / pool) that mirrors what an SRE would normally pull
from pg_stat_activity / pg_locks / a connection pool gauge. The agent calls
these tools when triage classifies the incident as `database` or when an
application error trail points at the data layer.

Endpoints are configured at startup via `configure_db_endpoints({service: base_url})`,
following the same pattern as chaos / splunk. If a service has no configured
endpoint or the endpoint is unreachable, tools return an explicit
"no db backend configured" note so the LLM can mark this as missing evidence
rather than hallucinate a database failure.
"""
from __future__ import annotations

import json
from typing import Optional

import httpx

from . import tool


_DB_ENDPOINTS: dict[str, str] = {}
_DB_TIMEOUT: float = 5.0


def configure_db_endpoints(endpoints: dict[str, str], timeout_s: float = 5.0) -> None:
    """Map full service name (e.g. 'payments-service') → base URL of /db API."""
    global _DB_ENDPOINTS, _DB_TIMEOUT
    _DB_ENDPOINTS = {k: v.rstrip("/") for k, v in endpoints.items() if v}
    _DB_TIMEOUT = float(timeout_s)


def _err(message: str) -> dict:
    return {"isError": True, "content": [{"type": "text", "text": message}]}


def _ok(payload) -> dict:
    text = json.dumps(payload) if isinstance(payload, (dict, list)) else str(payload)
    return {"content": [{"type": "text", "text": text}], "data": payload}


def _resolve_base(service: Optional[str]) -> Optional[str]:
    """Accept short ('payments') or full ('payments-service') keys."""
    if not service:
        return None
    s = str(service).strip()
    if s in _DB_ENDPOINTS:
        return _DB_ENDPOINTS[s]
    full = s if s.endswith("-service") else f"{s}-service"
    return _DB_ENDPOINTS.get(full)


def _no_backend(name: str, service: Optional[str]) -> dict:
    return _ok({
        "service": service,
        "results": [],
        "note": f"{name}: no db backend configured for service={service}",
    })


async def _get(base: str, path: str, params: dict) -> dict:
    async with httpx.AsyncClient(timeout=_DB_TIMEOUT) as client:
        resp = await client.get(f"{base}{path}", params=params)
        resp.raise_for_status()
        return resp.json()


@tool(
    "db_slow_queries",
    "Top-N slowest queries for a service's database in the last N minutes. "
    "Returns query text, mean/p99 duration, and call count. Use when triage "
    "category is 'database' or RCA suspects ORM/N+1/missing index.",
    {"service": str, "minutes": Optional[int], "limit": Optional[int]},
)
async def db_slow_queries(args: dict) -> dict:
    service = args.get("service")
    base = _resolve_base(service)
    if base is None:
        return _no_backend("db_slow_queries", service)
    params = {
        "minutes": int(args.get("minutes") or 15),
        "limit": int(args.get("limit") or 10),
    }
    try:
        data = await _get(base, "/db/slow_queries", params)
        return _ok(data)
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout):
        return _no_backend("db_slow_queries", service)
    except Exception as exc:  # noqa: BLE001
        return _err(f"db_slow_queries failed: {exc}")


@tool(
    "db_locks",
    "Currently-blocking locks on a service's database. Returns rows of "
    "{blocked_pid, blocking_pid, query, wait_seconds}. Use to confirm "
    "lock contention / deadlock hypothesis from RCA.",
    {"service": str},
)
async def db_locks(args: dict) -> dict:
    service = args.get("service")
    base = _resolve_base(service)
    if base is None:
        return _no_backend("db_locks", service)
    try:
        data = await _get(base, "/db/locks", {})
        return _ok(data)
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout):
        return _no_backend("db_locks", service)
    except Exception as exc:  # noqa: BLE001
        return _err(f"db_locks failed: {exc}")


@tool(
    "db_pool_stats",
    "Connection-pool stats for a service's database: size, active, idle, "
    "waiting, recent acquire timeouts. Use to confirm pool exhaustion as the "
    "cause of timeouts/5xx.",
    {"service": str},
)
async def db_pool_stats(args: dict) -> dict:
    service = args.get("service")
    base = _resolve_base(service)
    if base is None:
        return _no_backend("db_pool_stats", service)
    try:
        data = await _get(base, "/db/pool", {})
        return _ok(data)
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout):
        return _no_backend("db_pool_stats", service)
    except Exception as exc:  # noqa: BLE001
        return _err(f"db_pool_stats failed: {exc}")
