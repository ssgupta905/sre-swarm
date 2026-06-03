"""Log-oriented MCP tools."""
from __future__ import annotations

import asyncio
import json
import shutil

from . import tool


def _err(message: str) -> dict:
    return {"isError": True, "content": [{"type": "text", "text": message}]}


def _ok(payload) -> dict:
    text = json.dumps(payload) if isinstance(payload, (dict, list)) else str(payload)
    return {"content": [{"type": "text", "text": text}], "data": payload}


@tool(
    "get_recent_log_errors",
    "Count ERROR/WARN lines per pod in the last N minutes.",
    {"namespace": str, "since_minutes": int},
)
async def get_recent_log_errors(args: dict) -> dict:
    binary = shutil.which("kubectl") or "kubectl"
    namespace = args["namespace"]
    since = f"{int(args.get('since_minutes', 5))}m"

    proc = await asyncio.create_subprocess_exec(
        binary,
        "get",
        "pods",
        "-n",
        namespace,
        "-o",
        "jsonpath={.items[*].metadata.name}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, errb = await proc.communicate()
    if proc.returncode != 0:
        return _err(f"could not list pods: {errb.decode('utf-8', 'replace')}")

    pods = out.decode("utf-8", "replace").split()
    summary: list[dict] = []
    for pod in pods:
        proc = await asyncio.create_subprocess_exec(
            binary,
            "logs",
            pod,
            "-n",
            namespace,
            "--since",
            since,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        logs, _ = await proc.communicate()
        text = logs.decode("utf-8", "replace") if logs else ""
        errors = sum(1 for line in text.splitlines() if "ERROR" in line)
        warns = sum(1 for line in text.splitlines() if "WARN" in line)
        summary.append({"pod": pod, "errors": errors, "warnings": warns})
    return _ok(summary)
