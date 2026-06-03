"""Chaos MCP tools.

`chaos_inject` / `chaos_reset` talk to per-service chaos APIs exposed by the
demo app (config: services[].chaos_api). The replay variants apply Chaos Mesh
CRDs strictly to the sandbox namespace per FR-034.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from pathlib import Path
from typing import Optional

import httpx

from . import tool


_CHAOS_TARGETS: dict[str, str] = {}
_SANDBOX_NS: str = "sre-sandbox"
_DEMO_NS: str = "sre-demo"
_TIMEOUT: float = 10.0


def configure_chaos(
    service_endpoints: dict[str, str],
    sandbox_namespace: str = "sre-sandbox",
    demo_namespace: str = "sre-demo",
    timeout_s: float = 10.0,
) -> None:
    """Configure which chaos API endpoints belong to which service."""
    global _CHAOS_TARGETS, _SANDBOX_NS, _DEMO_NS, _TIMEOUT
    _CHAOS_TARGETS = dict(service_endpoints)
    _SANDBOX_NS = sandbox_namespace
    _DEMO_NS = demo_namespace
    _TIMEOUT = float(timeout_s)


def _err(message: str) -> dict:
    return {"isError": True, "content": [{"type": "text", "text": message}]}


def _ok(payload) -> dict:
    text = json.dumps(payload) if isinstance(payload, (dict, list)) else str(payload)
    return {"content": [{"type": "text", "text": text}], "data": payload}


@tool(
    "chaos_inject",
    "Inject chaos into a service via its /chaos HTTP API. "
    "Set error_rate (0.0-1.0) and/or latency_ms; the tool POSTs them to the "
    "service's /chaos/error_rate?value= and /chaos/latency?ms= endpoints. "
    "Service may be the short form ('payments') or the full name ('payments-service').",
    {
        "service": Optional[str],
        "error_rate": Optional[float],
        "latency_ms": Optional[int],
    },
)
async def chaos_inject(args: dict) -> dict:
    service = args.get("service")
    base = _resolve_target(service)
    if base is None:
        return _err(
            f"no chaos_api endpoint configured for service '{service}' "
            f"(known: {sorted(_CHAOS_TARGETS)})"
        )
    actions: list[dict] = []
    error_rate = args.get("error_rate")
    latency_ms = args.get("latency_ms")
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            if error_rate is not None:
                resp = await client.post(
                    f"{base.rstrip('/')}/chaos/error_rate",
                    params={"value": float(error_rate)},
                )
                resp.raise_for_status()
                actions.append({"error_rate": resp.json()})
            if latency_ms is not None:
                resp = await client.post(
                    f"{base.rstrip('/')}/chaos/latency",
                    params={"ms": int(latency_ms)},
                )
                resp.raise_for_status()
                actions.append({"latency": resp.json()})
        if not actions:
            return _err("chaos_inject: nothing to do (provide error_rate or latency_ms)")
        return _ok({"service": service, "endpoint": base, "applied": actions})
    except Exception as exc:  # noqa: BLE001
        return _err(f"chaos_inject failed: {exc}")


@tool("chaos_reset", "Reset chaos on a specific service or all services. POSTs /chaos/clear.", {"service": Optional[str]})
async def chaos_reset(args: dict) -> dict:
    if not _CHAOS_TARGETS:
        return _err("no chaos endpoints configured")
    requested = args.get("service")
    resolved_key = _resolve_key(requested)
    targets = (
        {resolved_key: _CHAOS_TARGETS[resolved_key]}
        if resolved_key
        else dict(_CHAOS_TARGETS)
    )
    results: dict[str, str] = {}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        for svc, base in targets.items():
            try:
                resp = await client.post(f"{base.rstrip('/')}/chaos/clear")
                resp.raise_for_status()
                results[svc] = "cleared"
            except Exception as exc:  # noqa: BLE001
                results[svc] = f"error: {exc}"
    return _ok(results)


@tool(
    "chaos_replay_apply",
    "Apply a Chaos Mesh CRD to sre-sandbox namespace for hypothesis testing.",
    {"kind": str, "spec": dict, "duration_s": int, "name": Optional[str]},
)
async def chaos_replay_apply(args: dict) -> dict:
    name = args.get("name") or f"replay-{args['kind'].lower()}"
    spec = dict(args["spec"])
    if "namespace" not in spec:
        spec["namespace"] = _SANDBOX_NS
    if spec["namespace"] != _SANDBOX_NS:
        return _err(
            f"refusing to apply chaos to namespace '{spec['namespace']}' — "
            f"chaos_replay_apply is sandbox-only ({_SANDBOX_NS})"
        )

    crd = {
        "apiVersion": "chaos-mesh.org/v1alpha1",
        "kind": args["kind"],
        "metadata": {"name": name, "namespace": _SANDBOX_NS},
        "spec": spec,
    }
    duration_s = int(args.get("duration_s", 60))

    binary = shutil.which("kubectl") or "kubectl"
    with tempfile.NamedTemporaryFile(
        "w", suffix=".json", delete=False, dir=tempfile.gettempdir()
    ) as fp:
        json.dump(crd, fp)
        crd_path = Path(fp.name)
    try:
        code, out, err = await _run([binary, "apply", "-n", _SANDBOX_NS, "-f", str(crd_path)])
        if code != 0:
            return _err(f"chaos_replay_apply failed: {err or out}")
        return _ok(
            {
                "applied": name,
                "kind": args["kind"],
                "namespace": _SANDBOX_NS,
                "duration_s": duration_s,
                "kubectl_stdout": out.strip(),
            }
        )
    finally:
        try:
            crd_path.unlink()
        except OSError:
            pass


@tool(
    "chaos_replay_delete",
    "Delete a Chaos Mesh experiment from sre-sandbox.",
    {"name": str, "kind": str},
)
async def chaos_replay_delete(args: dict) -> dict:
    binary = shutil.which("kubectl") or "kubectl"
    code, out, err = await _run(
        [
            binary,
            "delete",
            args["kind"].lower(),
            args["name"],
            "-n",
            _SANDBOX_NS,
            "--ignore-not-found",
        ]
    )
    if code != 0:
        return _err(f"chaos_replay_delete failed: {err or out}")
    return _ok({"deleted": args["name"], "stdout": out.strip()})


def _resolve_key(service: Optional[str]) -> Optional[str]:
    """Map a user-supplied service token to a configured chaos target key.

    Accepts the full name ('payments-service'), the short form ('payments'),
    or a substring match — picks the unique target if exactly one matches.
    """
    if not service:
        return None
    if service in _CHAOS_TARGETS:
        return service
    s = service.lower()
    # exact short-form match (strip trailing "-service")
    candidates = [k for k in _CHAOS_TARGETS if k.lower().split("-service")[0] == s]
    if len(candidates) == 1:
        return candidates[0]
    # substring fallback — single unambiguous hit only
    candidates = [k for k in _CHAOS_TARGETS if s in k.lower()]
    if len(candidates) == 1:
        return candidates[0]
    return None


def _resolve_target(service: Optional[str]) -> Optional[str]:
    key = _resolve_key(service)
    if key:
        return _CHAOS_TARGETS[key]
    if service is None and len(_CHAOS_TARGETS) == 1:
        return next(iter(_CHAOS_TARGETS.values()))
    return None


async def _run(cmd: list[str]) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, errb = await proc.communicate()
    return (
        proc.returncode or 0,
        out.decode("utf-8", "replace"),
        errb.decode("utf-8", "replace"),
    )
