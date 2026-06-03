"""Write-side kubectl MCP tools (Heal agent only, post HITL approval)."""
from __future__ import annotations

import asyncio
import json
import shutil

from . import tool
from .kubectl import _err, _ok, _resolve_namespace, _run_kubectl  # reuse shared helpers


@tool(
    "kubectl_rollout_restart",
    "Restart a deployment. WRITE operation — requires HITL approval.",
    {"deployment": str, "namespace": str},
)
async def kubectl_rollout_restart(args: dict) -> dict:
    deployment = args.get("deployment") or args.get("name")
    if not deployment:
        return _err("kubectl_rollout_restart: 'deployment' is required")
    namespace = _resolve_namespace(args.get("namespace"))
    code, out, err = await _run_kubectl(
        ["rollout", "restart", f"deployment/{deployment}", "-n", namespace]
    )
    if code != 0:
        return _err(f"kubectl_rollout_restart failed: {err.strip() or out.strip()}")
    return _ok({"deployment": deployment, "namespace": namespace, "stdout": out.strip()})


@tool(
    "kubectl_scale",
    "Scale a deployment replica count. WRITE operation.",
    {"deployment": str, "namespace": str, "replicas": int},
)
async def kubectl_scale(args: dict) -> dict:
    deployment = args.get("deployment") or args.get("name")
    if not deployment:
        return _err("kubectl_scale: 'deployment' is required")
    if "replicas" not in args:
        return _err("kubectl_scale: 'replicas' is required")
    namespace = _resolve_namespace(args.get("namespace"))
    try:
        replicas = int(args["replicas"])
    except (TypeError, ValueError):
        return _err(f"kubectl_scale: 'replicas' must be an integer, got {args['replicas']!r}")
    code, out, err = await _run_kubectl(
        ["scale", f"deployment/{deployment}", f"--replicas={replicas}", "-n", namespace]
    )
    if code != 0:
        return _err(f"kubectl_scale failed: {err.strip() or out.strip()}")
    return _ok({"deployment": deployment, "namespace": namespace, "replicas": replicas, "stdout": out.strip()})


@tool(
    "kubectl_apply_patch",
    "Apply a strategic merge patch to a resource. WRITE operation.",
    {"resource": str, "name": str, "namespace": str, "patch": dict},
)
async def kubectl_apply_patch(args: dict) -> dict:
    resource = args.get("resource") or args.get("kind")
    name = args.get("name")
    patch = args.get("patch")
    if not resource or not name:
        return _err("kubectl_apply_patch: 'resource' and 'name' are required")
    if not isinstance(patch, (dict, list)):
        return _err("kubectl_apply_patch: 'patch' must be a dict or list (strategic merge patch)")
    namespace = _resolve_namespace(args.get("namespace"))
    code, out, err = await _run_kubectl(
        ["patch", str(resource), str(name), "-n", namespace, "-p", json.dumps(patch), "--type", "merge"]
    )
    if code != 0:
        return _err(f"kubectl_apply_patch failed: {err.strip() or out.strip()}")
    return _ok({"resource": resource, "name": name, "namespace": namespace, "stdout": out.strip()})
