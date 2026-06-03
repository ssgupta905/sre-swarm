"""kubectl-style read-only MCP tools.

Implemented via subprocess invocation of the local `kubectl` binary to avoid
forcing in-cluster credentials in the agent runtime. Output is JSON-parsed when
the caller requests `-o json`.
"""
from __future__ import annotations

import asyncio
import json
import shutil
from typing import Optional

from . import tool


_KUBECTL_CONTEXT: Optional[str] = None
_KUBECTL_DEFAULT_NAMESPACE: Optional[str] = None


def configure_kubectl(context: Optional[str] = None, namespace: Optional[str] = None) -> None:
    """Pin every kubectl invocation to a specific context (and a default namespace
    used when the LLM omits one or passes a placeholder like 'default')."""
    global _KUBECTL_CONTEXT, _KUBECTL_DEFAULT_NAMESPACE
    _KUBECTL_CONTEXT = context
    _KUBECTL_DEFAULT_NAMESPACE = namespace


def _resolve_namespace(ns: Optional[str]) -> str:
    """Treat empty / missing / placeholder values as the configured default."""
    if not ns or not str(ns).strip() or str(ns).strip().lower() in {"default", "none", "null"}:
        return _KUBECTL_DEFAULT_NAMESPACE or "default"
    return str(ns).strip()


def _err(message: str) -> dict:
    return {"isError": True, "content": [{"type": "text", "text": message}]}


def _ok(payload) -> dict:
    if isinstance(payload, (dict, list)):
        text = json.dumps(payload)
    else:
        text = str(payload)
    return {"content": [{"type": "text", "text": text}], "data": payload}


async def _run_kubectl(args: list[str], parse_json: bool = False) -> tuple[int, str, str]:
    binary = shutil.which("kubectl") or "kubectl"
    full = [binary]
    if _KUBECTL_CONTEXT:
        full.extend(["--context", _KUBECTL_CONTEXT])
    full.extend(args)
    proc = await asyncio.create_subprocess_exec(
        *full,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, errb = await proc.communicate()
    return proc.returncode or 0, out.decode("utf-8", "replace"), errb.decode("utf-8", "replace")


@tool(
    "kubectl_get",
    "Run kubectl get <resource> [name] -n <namespace> -o json",
    {
        "resource": str,
        "namespace": str,
        "name": Optional[str],
        "label_selector": Optional[str],
    },
)
async def kubectl_get(args: dict) -> dict:
    resource = args.get("resource") or args.get("kind") or args.get("type")
    if not resource:
        return _err("kubectl_get: missing required arg 'resource' (e.g. 'pods', 'deployments')")
    namespace = _resolve_namespace(args.get("namespace"))
    cmd = ["get", str(resource)]
    if args.get("name"):
        cmd.append(str(args["name"]))
    cmd.extend(["-n", namespace, "-o", "json"])
    if args.get("label_selector"):
        cmd.extend(["-l", str(args["label_selector"])])
    code, out, err = await _run_kubectl(cmd)
    if code != 0:
        return _err(f"kubectl_get failed: {err.strip() or out.strip()}")
    try:
        return _ok(json.loads(out))
    except json.JSONDecodeError as exc:
        return _err(f"kubectl_get returned invalid JSON: {exc}")


@tool(
    "kubectl_describe",
    "Run kubectl describe <resource> <name> -n <namespace>",
    {"resource": str, "name": str, "namespace": str},
)
async def kubectl_describe(args: dict) -> dict:
    if not args.get("resource") or not args.get("name"):
        return _err("kubectl_describe: 'resource' and 'name' are required")
    namespace = _resolve_namespace(args.get("namespace"))
    code, out, err = await _run_kubectl(
        ["describe", str(args["resource"]), str(args["name"]), "-n", namespace]
    )
    if code != 0:
        return _err(f"kubectl_describe failed: {err.strip() or out.strip()}")
    return _ok(out)


@tool(
    "kubectl_get_events",
    "Get Warning events sorted by time for a namespace.",
    {"namespace": str, "field_selector": Optional[str]},
)
async def kubectl_get_events(args: dict) -> dict:
    cmd = [
        "get",
        "events",
        "-n",
        _resolve_namespace(args.get("namespace")),
        "--sort-by=.lastTimestamp",
        "-o",
        "json",
    ]
    selector = args.get("field_selector") or "type=Warning"
    cmd.extend(["--field-selector", selector])
    code, out, err = await _run_kubectl(cmd)
    if code != 0:
        return _err(f"kubectl_get_events failed: {err.strip() or out.strip()}")
    try:
        return _ok(json.loads(out))
    except json.JSONDecodeError as exc:
        return _err(f"kubectl_get_events returned invalid JSON: {exc}")


@tool(
    "get_pod_logs",
    "Fetch pod logs. Returns last N lines or since duration.",
    {
        "pod": str,
        "namespace": str,
        "container": Optional[str],
        "tail": Optional[int],
        "since": Optional[str],
        "previous": Optional[bool],
    },
)
async def get_pod_logs(args: dict) -> dict:
    if not args.get("pod"):
        return _err("get_pod_logs: 'pod' is required")
    cmd = ["logs", str(args["pod"]), "-n", _resolve_namespace(args.get("namespace"))]
    if args.get("container"):
        cmd.extend(["-c", args["container"]])
    if args.get("tail") is not None:
        cmd.extend(["--tail", str(args["tail"])])
    if args.get("since"):
        cmd.extend(["--since", args["since"]])
    if args.get("previous"):
        cmd.append("--previous")
    code, out, err = await _run_kubectl(cmd)
    if code != 0:
        return _err(f"get_pod_logs failed: {err.strip() or out.strip()}")
    return _ok(out)


@tool(
    "get_resource_usage",
    "Get CPU/memory usage for pods via kubectl top.",
    {"namespace": str, "label_selector": Optional[str]},
)
async def get_resource_usage(args: dict) -> dict:
    cmd = ["top", "pods", "-n", _resolve_namespace(args.get("namespace")), "--no-headers"]
    if args.get("label_selector"):
        cmd.extend(["-l", args["label_selector"]])
    code, out, err = await _run_kubectl(cmd)
    if code != 0:
        return _err(f"get_resource_usage failed: {err.strip() or out.strip()}")
    rows = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3:
            rows.append({"pod": parts[0], "cpu": parts[1], "memory": parts[2]})
    return _ok(rows)


@tool(
    "get_deployment_status",
    "Get deployment rollout status and replica counts.",
    {"name": str, "namespace": str},
)
async def get_deployment_status(args: dict) -> dict:
    if not args.get("name"):
        return _err("get_deployment_status: 'name' is required")
    namespace = _resolve_namespace(args.get("namespace"))
    code, out, err = await _run_kubectl(
        ["get", "deployment", str(args["name"]), "-n", namespace, "-o", "json"]
    )
    if code != 0:
        return _err(f"get_deployment_status failed: {err.strip() or out.strip()}")
    try:
        obj = json.loads(out)
    except json.JSONDecodeError as exc:
        return _err(f"get_deployment_status invalid JSON: {exc}")
    status = obj.get("status", {})
    spec = obj.get("spec", {})
    return _ok(
        {
            "name": args["name"],
            "namespace": args["namespace"],
            "replicas_desired": spec.get("replicas"),
            "replicas_ready": status.get("readyReplicas"),
            "replicas_available": status.get("availableReplicas"),
            "replicas_updated": status.get("updatedReplicas"),
            "conditions": status.get("conditions", []),
        }
    )
