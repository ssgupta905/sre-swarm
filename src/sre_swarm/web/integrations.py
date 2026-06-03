"""Integration registry: enumerate, test, and edit system component connections.

Each integration has a `name`, a `kind` (built-in vs custom HTTP), a `status`
(reachable | unreachable | unconfigured | unknown), and a small connection
descriptor (URL, namespace, token-presence, etc).

`test()` performs a lightweight reachability probe (no side effects). It must
not require credentials beyond what's already in config.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from ..config import Config, save_config


@dataclass(slots=True)
class IntegrationCard:
    name: str
    kind: str                 # "prometheus" | "kubernetes" | "splunk" | "github" | "custom"
    status: str = "unknown"   # "reachable" | "unreachable" | "unconfigured" | "unknown"
    summary: str = ""
    detail: dict = field(default_factory=dict)
    last_tested_at: Optional[str] = None
    last_error: str = ""
    editable_fields: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "status": self.status,
            "summary": self.summary,
            "detail": self.detail,
            "last_tested_at": self.last_tested_at,
            "last_error": self.last_error,
            "editable_fields": self.editable_fields,
        }


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---- per-integration probes ------------------------------------------------

async def _probe_prometheus(cfg: Config) -> tuple[str, str, dict, str]:
    url = cfg.prometheus.url.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=cfg.prometheus.timeout_s) as c:
            r = await c.get(f"{url}/-/healthy")
        if r.status_code == 200:
            return ("reachable", f"healthy at {url}", {"url": url}, "")
        return ("unreachable", f"HTTP {r.status_code}", {"url": url}, f"prometheus returned {r.status_code}")
    except Exception as exc:  # noqa: BLE001
        return ("unreachable", "connection refused", {"url": url}, f"{exc.__class__.__name__}: {exc}")


async def _probe_kubernetes(cfg: Config) -> tuple[str, str, dict, str]:
    ctx = cfg.cluster.context
    ns = cfg.cluster.namespace
    try:
        # Run kubectl synchronously in a thread to avoid blocking the loop.
        def _check():
            import subprocess
            r = subprocess.run(
                ["kubectl", "--context", ctx, "get", "ns", ns, "-o", "name"],
                capture_output=True, text=True, timeout=5,
            )
            return r.returncode, r.stdout.strip(), r.stderr.strip()
        rc, out, err = await asyncio.to_thread(_check)
        if rc == 0 and ns in out:
            return ("reachable", f"context={ctx} ns={ns}", {"context": ctx, "namespace": ns}, "")
        msg = err or out or "kubectl failed"
        return ("unreachable", f"kubectl: {msg[:60]}", {"context": ctx, "namespace": ns}, msg)
    except FileNotFoundError:
        return ("unconfigured", "kubectl not installed", {"context": ctx, "namespace": ns}, "kubectl missing on PATH")
    except Exception as exc:  # noqa: BLE001
        return ("unreachable", f"{exc.__class__.__name__}", {"context": ctx, "namespace": ns}, str(exc))


async def _probe_splunk(cfg: Config) -> tuple[str, str, dict, str]:
    url = cfg.splunk.url.rstrip("/")
    detail = {"url": url, "token_set": bool(cfg.splunk.token)}
    # The demo splunk-mock exposes /health; fall back to root if missing.
    try:
        async with httpx.AsyncClient(timeout=cfg.splunk.timeout_s) as c:
            r = await c.get(f"{url}/health")
            if r.status_code == 404:
                r = await c.get(f"{url}/")
        if r.status_code == 200:
            return ("reachable", f"HEC up at {url}", detail, "")
        return ("unreachable", f"HTTP {r.status_code}", detail, f"splunk returned {r.status_code}")
    except Exception as exc:  # noqa: BLE001
        return ("unreachable", "connection refused", detail, f"{exc.__class__.__name__}: {exc}")


async def _probe_github(cfg: Config) -> tuple[str, str, dict, str]:
    token = (cfg.github.token or "").strip()
    api = cfg.github.api_url.rstrip("/")
    detail = {"api_url": api, "token_set": bool(token)}
    if not token:
        return ("unconfigured", "no GitHub PAT configured", detail, "")
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        async with httpx.AsyncClient(timeout=cfg.github.timeout_s) as c:
            r = await c.get(f"{api}/user", headers=headers)
        if r.status_code == 200:
            login = r.json().get("login", "?")
            scopes = r.headers.get("x-oauth-scopes", "").strip() or "(fine-grained)"
            detail["user"] = login
            detail["scopes"] = scopes
            return ("reachable", f"authenticated as {login}", detail, "")
        if r.status_code in (401, 403):
            return ("unreachable", f"auth failed (HTTP {r.status_code})", detail, r.text[:200])
        return ("unreachable", f"HTTP {r.status_code}", detail, r.text[:200])
    except Exception as exc:  # noqa: BLE001
        return ("unreachable", "connection refused", detail, f"{exc.__class__.__name__}: {exc}")


async def _probe_ollama(cfg: Config) -> tuple[str, str, dict, str]:
    url = cfg.ollama.url.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.get(f"{url}/api/tags")
        if r.status_code == 200:
            models = [m.get("name") for m in r.json().get("models", [])]
            return ("reachable", f"{cfg.ollama.model} @ {url}", {"url": url, "model": cfg.ollama.model, "available": models[:6]}, "")
        return ("unreachable", f"HTTP {r.status_code}", {"url": url, "model": cfg.ollama.model}, f"ollama returned {r.status_code}")
    except Exception as exc:  # noqa: BLE001
        return ("unreachable", "connection refused", {"url": url, "model": cfg.ollama.model}, f"{exc.__class__.__name__}: {exc}")


# ---- registry --------------------------------------------------------------

class IntegrationRegistry:
    """In-memory registry. Built-in integrations are derived from Config; custom
    integrations are persisted to a tiny JSON file alongside config."""

    BUILTINS: list[tuple[str, str, list[str]]] = [
        # (name, kind, editable_fields)
        ("prometheus", "prometheus", ["url", "timeout_s"]),
        ("kubernetes", "kubernetes", ["context", "namespace"]),
        ("splunk", "splunk", ["url", "token"]),
        ("github", "github", ["token", "api_url"]),
        ("ollama", "ollama", ["url", "model"]),
    ]

    def __init__(self, config: Config) -> None:
        self.config = config
        self._cards: dict[str, IntegrationCard] = {}
        for name, kind, editable in self.BUILTINS:
            self._cards[name] = IntegrationCard(
                name=name, kind=kind, editable_fields=editable,
            )
            self._refresh_summary(name)

    # ---- read ----
    def list(self) -> list[dict]:
        return [c.to_json() for c in self._cards.values()]

    def get(self, name: str) -> Optional[IntegrationCard]:
        return self._cards.get(name)

    # ---- test ----
    async def test(self, name: str) -> Optional[dict]:
        card = self._cards.get(name)
        if card is None:
            return None
        probe = {
            "prometheus": _probe_prometheus,
            "kubernetes": _probe_kubernetes,
            "splunk": _probe_splunk,
            "github": _probe_github,
            "ollama": _probe_ollama,
        }.get(card.kind)
        if probe is None:
            # Custom HTTP: simple GET on configured URL.
            url = card.detail.get("url", "")
            if not url:
                card.status = "unconfigured"
                card.last_error = "no URL"
            else:
                try:
                    async with httpx.AsyncClient(timeout=5.0) as c:
                        r = await c.get(url)
                    card.status = "reachable" if r.status_code < 500 else "unreachable"
                    card.summary = f"HTTP {r.status_code}"
                    card.last_error = "" if card.status == "reachable" else f"HTTP {r.status_code}"
                except Exception as exc:  # noqa: BLE001
                    card.status = "unreachable"
                    card.summary = "connection refused"
                    card.last_error = f"{exc.__class__.__name__}: {exc}"
        else:
            status, summary, detail, err = await probe(self.config)
            card.status = status
            card.summary = summary
            card.detail = detail
            card.last_error = err
        card.last_tested_at = _iso_now()
        return card.to_json()

    async def test_all(self) -> list[dict]:
        results = await asyncio.gather(*(self.test(name) for name in self._cards), return_exceptions=False)
        return [r for r in results if r is not None]

    # ---- edit ----
    def update(self, name: str, fields: dict[str, Any]) -> Optional[dict]:
        card = self._cards.get(name)
        if card is None:
            return None
        applied = self._apply_update(card, fields)
        if applied:
            # Persist config so it survives a restart.
            try:
                save_config(self.config)
            except Exception:  # noqa: BLE001
                pass
        self._refresh_summary(name)
        return card.to_json()

    def _apply_update(self, card: IntegrationCard, fields: dict[str, Any]) -> bool:
        cfg = self.config
        changed = False
        if card.kind == "prometheus":
            if "url" in fields and fields["url"]:
                cfg.prometheus.url = str(fields["url"]).strip()
                changed = True
            if "timeout_s" in fields and fields["timeout_s"]:
                try:
                    cfg.prometheus.timeout_s = int(fields["timeout_s"])
                    changed = True
                except (TypeError, ValueError):
                    pass
        elif card.kind == "kubernetes":
            if "context" in fields and fields["context"]:
                cfg.cluster.context = str(fields["context"]).strip()
                changed = True
            if "namespace" in fields and fields["namespace"]:
                cfg.cluster.namespace = str(fields["namespace"]).strip()
                changed = True
        elif card.kind == "splunk":
            if "url" in fields and fields["url"]:
                cfg.splunk.url = str(fields["url"]).strip()
                changed = True
            if "token" in fields and fields["token"]:
                cfg.splunk.token = str(fields["token"]).strip()
                changed = True
        elif card.kind == "github":
            if "token" in fields and fields["token"]:
                cfg.github.token = str(fields["token"]).strip()
                changed = True
            if "api_url" in fields and fields["api_url"]:
                cfg.github.api_url = str(fields["api_url"]).strip()
                changed = True
        elif card.kind == "ollama":
            if "url" in fields and fields["url"]:
                cfg.ollama.url = str(fields["url"]).strip()
                changed = True
            if "model" in fields and fields["model"]:
                cfg.ollama.model = str(fields["model"]).strip()
                changed = True
        elif card.kind == "custom":
            if "url" in fields:
                card.detail["url"] = str(fields["url"]).strip()
                changed = True
        return changed

    def add_custom(self, name: str, url: str) -> dict:
        name = name.strip()
        url = url.strip()
        if name in self._cards:
            raise ValueError(f"integration '{name}' already exists")
        card = IntegrationCard(
            name=name, kind="custom",
            detail={"url": url}, summary=url,
            status="unknown", editable_fields=["url"],
        )
        self._cards[name] = card
        return card.to_json()

    # ---- helpers ----
    def _refresh_summary(self, name: str) -> None:
        card = self._cards.get(name)
        if card is None:
            return
        cfg = self.config
        if card.kind == "prometheus":
            card.detail = {"url": cfg.prometheus.url, "timeout_s": cfg.prometheus.timeout_s}
            card.summary = card.summary or cfg.prometheus.url
        elif card.kind == "kubernetes":
            card.detail = {"context": cfg.cluster.context, "namespace": cfg.cluster.namespace}
            card.summary = card.summary or f"{cfg.cluster.context} / {cfg.cluster.namespace}"
        elif card.kind == "splunk":
            card.detail = {"url": cfg.splunk.url, "token_set": bool(cfg.splunk.token)}
            card.summary = card.summary or cfg.splunk.url
        elif card.kind == "github":
            token_set = bool((cfg.github.token or "").strip())
            card.detail = {"api_url": cfg.github.api_url, "token_set": token_set}
            card.summary = card.summary or (cfg.github.api_url if token_set else "not configured")
            if not card.status or card.status == "unknown":
                card.status = "unconfigured" if not token_set else "unknown"
        elif card.kind == "ollama":
            card.detail = {"url": cfg.ollama.url, "model": cfg.ollama.model}
            card.summary = card.summary or f"{cfg.ollama.model} @ {cfg.ollama.url}"
