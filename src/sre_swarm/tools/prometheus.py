"""Prometheus tools.

Each function is decorated with the local `@tool` shim (see tools/__init__.py)
so the Ollama tool-calling loop can discover its name/description/schema and
invoke it directly. They share a module-level client configured by
`configure_prometheus()` before the orchestrator boots.
"""
from __future__ import annotations

from typing import Optional

import httpx

from . import tool


_PROM_URL: str = "http://localhost:9090"
_PROM_TIMEOUT: float = 10.0


def configure_prometheus(url: str, timeout_s: float = 10.0) -> None:
    """Set the Prometheus URL and timeout used by every prometheus_* tool."""
    global _PROM_URL, _PROM_TIMEOUT
    _PROM_URL = url.rstrip("/")
    _PROM_TIMEOUT = float(timeout_s)


def _err(message: str) -> dict:
    return {"isError": True, "content": [{"type": "text", "text": message}]}


def _ok(data: dict) -> dict:
    return {"content": [{"type": "text", "text": str(data)}], "data": data}


async def _get(path: str, params: dict) -> dict:
    url = f"{_PROM_URL}{path}"
    async with httpx.AsyncClient(timeout=_PROM_TIMEOUT) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        payload = resp.json()
    if payload.get("status") != "success":
        raise RuntimeError(payload.get("error", "prometheus query failed"))
    return payload.get("data", {})


@tool(
    "prometheus_query",
    "Run an instant PromQL query. Returns {resultType, result}.",
    {"query": str, "time": Optional[str]},
)
async def prometheus_query(args: dict) -> dict:
    try:
        params = {"query": args["query"]}
        if args.get("time"):
            params["time"] = args["time"]
        data = await _get("/api/v1/query", params)
        return _ok(data)
    except Exception as exc:  # noqa: BLE001
        return _err(f"prometheus_query failed: {exc}")


@tool(
    "prometheus_range_query",
    "Run a PromQL range query. Returns time-series data.",
    {"query": str, "start": str, "end": str, "step": str},
)
async def prometheus_range_query(args: dict) -> dict:
    try:
        params = {
            "query": args["query"],
            "start": args["start"],
            "end": args["end"],
            "step": args["step"],
        }
        data = await _get("/api/v1/query_range", params)
        return _ok(data)
    except Exception as exc:  # noqa: BLE001
        return _err(f"prometheus_range_query failed: {exc}")


@tool("list_alerts", "List currently firing Prometheus alerts.", {})
async def list_alerts(args: dict) -> dict:
    try:
        data = await _get("/api/v1/alerts", {})
        return _ok(data)
    except Exception as exc:  # noqa: BLE001
        return _err(f"list_alerts failed: {exc}")


async def prometheus_instant_value(query: str) -> Optional[float]:
    """Helper used by the Observer to read a single scalar value from PromQL."""
    try:
        data = await _get("/api/v1/query", {"query": query})
    except Exception:  # noqa: BLE001
        return None
    result = data.get("result") or []
    if not result:
        return None
    value = result[0].get("value")
    if not value or len(value) < 2:
        return None
    try:
        return float(value[1])
    except (TypeError, ValueError):
        return None
