"""Source-code and git MCP tools.

Lets the RCA agent grep the application repo for error strings, read the
surrounding code, look up recent commits, and blame a line. The repo root is
configured at startup via `configure_code(repo_root)`. All tools are read-only.

Designed to be offline-friendly: if `repo_root` is unset or doesn't exist, every
tool returns an explicit "no source repo configured" note rather than erroring,
so the agent can still produce a result.
"""
from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Optional

from . import tool


_REPO_ROOT: Optional[Path] = None
_MAX_BYTES = 64 * 1024  # cap any file slice we return to the LLM


def configure_code(repo_root: Optional[str]) -> None:
    """Point code tools at a local checkout of the application repo."""
    global _REPO_ROOT
    if repo_root and Path(repo_root).expanduser().exists():
        _REPO_ROOT = Path(repo_root).expanduser().resolve()
    else:
        _REPO_ROOT = None


def _err(message: str) -> dict:
    return {"isError": True, "content": [{"type": "text", "text": message}]}


def _ok(payload) -> dict:
    text = json.dumps(payload) if isinstance(payload, (dict, list)) else str(payload)
    return {"content": [{"type": "text", "text": text}], "data": payload}


def _no_repo(name: str) -> dict:
    return _ok({"results": [], "note": f"{name}: no source repo configured"})


def _safe_path(rel: str) -> Optional[Path]:
    """Resolve `rel` under the repo root, refusing escapes via `..`."""
    if _REPO_ROOT is None:
        return None
    try:
        full = (_REPO_ROOT / rel).resolve()
    except (OSError, ValueError):
        return None
    if _REPO_ROOT not in full.parents and full != _REPO_ROOT:
        return None
    return full


async def _run(cmd: list[str], cwd: Optional[Path] = None) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, errb = await proc.communicate()
    return proc.returncode or 0, out.decode("utf-8", "replace"), errb.decode("utf-8", "replace")


@tool(
    "code_search",
    "Grep the application source repo for a literal string or regex. Returns "
    "matching file:line:text rows. Use to find where an error message, "
    "exception type, or symbol is raised in code.",
    {
        "query": str,
        "path_glob": Optional[str],
        "max_results": Optional[int],
        "regex": Optional[bool],
    },
)
async def code_search(args: dict) -> dict:
    if _REPO_ROOT is None:
        return _no_repo("code_search")
    query = args.get("query")
    if not query:
        return _err("code_search: 'query' is required")
    rg = shutil.which("rg")
    max_results = int(args.get("max_results") or 30)
    if rg:
        cmd = [rg, "--no-heading", "--line-number", "--max-count", "5",
               "--max-columns", "240", "-S"]
        if not args.get("regex"):
            cmd.append("-F")
        if args.get("path_glob"):
            cmd.extend(["-g", str(args["path_glob"])])
        cmd.append(str(query))
        code, out, err = await _run(cmd, cwd=_REPO_ROOT)
        if code not in (0, 1):  # 1 = no matches, that's fine
            return _err(f"code_search failed: {err.strip() or out.strip()}")
    else:
        # grep -R fallback
        cmd = ["grep", "-RIn", "--max-count=5", str(query), "."]
        code, out, err = await _run(cmd, cwd=_REPO_ROOT)
        if code not in (0, 1):
            return _err(f"code_search failed: {err.strip() or out.strip()}")
    results = []
    for line in out.splitlines()[:max_results]:
        # rg/grep format: path:line:text
        parts = line.split(":", 2)
        if len(parts) == 3:
            results.append({"file": parts[0], "line": int(parts[1]) if parts[1].isdigit() else 0, "text": parts[2]})
    return _ok({"query": query, "count": len(results), "results": results})


@tool(
    "code_read",
    "Read a slice of a source file by line range. Use after code_search to see "
    "context around a match. Paths must be relative to the repo root.",
    {"path": str, "line_start": Optional[int], "line_end": Optional[int]},
)
async def code_read(args: dict) -> dict:
    if _REPO_ROOT is None:
        return _no_repo("code_read")
    rel = args.get("path")
    if not rel:
        return _err("code_read: 'path' is required")
    full = _safe_path(str(rel))
    if full is None or not full.is_file():
        return _err(f"code_read: path not found in repo: {rel}")
    try:
        text = full.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return _err(f"code_read failed: {exc}")
    lines = text.splitlines()
    start = max(1, int(args.get("line_start") or 1))
    end = int(args.get("line_end") or min(start + 100, len(lines)))
    end = min(end, len(lines))
    slice_text = "\n".join(f"{i:>5}: {lines[i - 1]}" for i in range(start, end + 1))
    if len(slice_text) > _MAX_BYTES:
        slice_text = slice_text[:_MAX_BYTES] + "\n... (truncated)"
    return _ok({"path": str(rel), "line_start": start, "line_end": end, "text": slice_text})


@tool(
    "git_recent_changes",
    "List recent commits in the application repo, optionally filtered to files "
    "matching a path glob (e.g. 'src/payments/**'). Returns sha, author, "
    "subject, and changed files. Use to spot a deploy that correlates with the "
    "incident start time.",
    {"since": Optional[str], "path_glob": Optional[str], "limit": Optional[int]},
)
async def git_recent_changes(args: dict) -> dict:
    if _REPO_ROOT is None:
        return _no_repo("git_recent_changes")
    since = args.get("since") or "2.days.ago"
    limit = int(args.get("limit") or 20)
    cmd = ["git", "log", f"--since={since}", f"-n{limit}",
           "--pretty=format:%h%x09%an%x09%aI%x09%s", "--name-only"]
    if args.get("path_glob"):
        cmd.extend(["--", str(args["path_glob"])])
    code, out, err = await _run(cmd, cwd=_REPO_ROOT)
    if code != 0:
        return _err(f"git_recent_changes failed: {err.strip() or out.strip()}")
    commits: list[dict] = []
    current: Optional[dict] = None
    for line in out.splitlines():
        if "\t" in line and len(line.split("\t")) >= 4:
            if current:
                commits.append(current)
            sha, author, ts, subject = line.split("\t", 3)
            current = {"sha": sha, "author": author, "ts": ts, "subject": subject, "files": []}
        elif line.strip() and current is not None:
            current["files"].append(line.strip())
    if current:
        commits.append(current)
    return _ok({"count": len(commits), "commits": commits})


@tool(
    "git_blame",
    "Show the commit that last touched a specific line in a file. Use to "
    "identify who introduced a suspicious code path.",
    {"path": str, "line": int},
)
async def git_blame(args: dict) -> dict:
    if _REPO_ROOT is None:
        return _no_repo("git_blame")
    rel = args.get("path")
    line = args.get("line")
    if not rel or not line:
        return _err("git_blame: 'path' and 'line' are required")
    full = _safe_path(str(rel))
    if full is None or not full.is_file():
        return _err(f"git_blame: path not found in repo: {rel}")
    cmd = ["git", "blame", "-L", f"{int(line)},{int(line)}",
           "--porcelain", str(rel)]
    code, out, err = await _run(cmd, cwd=_REPO_ROOT)
    if code != 0:
        return _err(f"git_blame failed: {err.strip() or out.strip()}")
    # porcelain first line: <sha> <orig_line> <final_line> [<num_lines>]
    sha = author = ts = subject = ""
    for ln in out.splitlines():
        if not sha and ln and not ln.startswith(("author", "committer", "summary", "previous", "filename", "boundary", "\t")):
            parts = ln.split(" ")
            sha = parts[0]
        elif ln.startswith("author "):
            author = ln[len("author "):]
        elif ln.startswith("author-time "):
            ts = ln[len("author-time "):]
        elif ln.startswith("summary "):
            subject = ln[len("summary "):]
    return _ok({"path": str(rel), "line": int(line), "sha": sha,
                "author": author, "author_time_unix": ts, "summary": subject})
