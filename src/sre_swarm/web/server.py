"""FastAPI server that exposes the SRE swarm over HTTP + WebSocket.

Architecture:
  - One background task runs the ObserverAgent and pushes IncidentEvents onto
    the shared event_bus.
  - When an IncidentEvent appears (or POST /api/investigate is called), a
    SwarmOrchestrator pipeline is kicked off — its AgentPhaseChange / AgentToken
    events flow through the same bus.
  - A fan-out task multiplexes the bus to every connected WebSocket client.
  - The web UI calls POST /api/approval/{incident_id} to satisfy the HITL gate.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..agents.events import (
    AgentMessage,
    AgentPhaseChange,
    AgentToken,
    AgentToolCall,
    AgentToolResult,
    HitlRequest,
    PipelineDone,
    RemediationPlanReady,
    UserMessage as UserChatMessage,
    VulnerabilityReport,
)
from ..agents.heal import ApprovalDecision
from ..agents.observer import ObserverAgent, new_incident_id
from ..agents.orchestrator import SwarmOrchestrator
from ..agents.replicate import build_replication_recipe
from ..audit import AuditLog
from ..config import Config
from ..models import BlastRadius, Incident, IncidentEvent, IncidentStatus, RemediationPlan
from ..tools.prometheus import prometheus_instant_value
from .integrations import IntegrationRegistry
from .system_copilot import SystemCopilot

import httpx

STATIC_DIR = Path(__file__).parent / "static"

# Hardcoded demo flow: orders is the front door; payments and notifications
# sit downstream. Edges with `kind=critical` are highlighted in the topology
# graph when their target turns red.
_DEFAULT_EDGES: list[tuple[str, str, str]] = [
    ("orders-service", "inventory-service", "read"),
    ("orders-service", "payments-service", "critical"),
    ("payments-service", "notifications-service", "fanout"),
    ("orders-service", "notifications-service", "fanout"),
]


def _derive_edges(service_names: list[str]) -> list[dict]:
    present = set(service_names)
    out = []
    for src, dst, kind in _DEFAULT_EDGES:
        if src in present and dst in present:
            out.append({"source": src, "target": dst, "kind": kind})
    return out


async def _safe_metric(service: str, kind: str) -> Optional[float]:
    """Read one of the canonical Observer metrics for a service. Returns None on miss."""
    prefix = service.removesuffix("-service").replace("-", "_")
    if kind == "error_rate":
        q = (
            f'sum(rate({prefix}_requests_total{{status=~"5.."}}[1m])) '
            f'/ sum(rate({prefix}_requests_total[1m])) * 100'
        )
    elif kind == "p99_latency":
        q = (
            f'histogram_quantile(0.99, sum(rate({prefix}_request_duration_seconds_bucket[5m])) by (le)) * 1000'
        )
    else:
        return None
    try:
        return await prometheus_instant_value(q)
    except Exception:  # noqa: BLE001
        return None


def _health_label(err: Optional[float], lat: Optional[float], thresholds) -> str:
    if err is not None and err >= thresholds.error_rate_pct * 2:
        return "critical"
    if err is not None and err >= thresholds.error_rate_pct:
        return "degraded"
    if lat is not None and lat >= thresholds.p99_latency_ms * 1.5:
        return "critical"
    if lat is not None and lat >= thresholds.p99_latency_ms:
        return "degraded"
    if err is None and lat is None:
        return "unknown"
    return "healthy"


def _safe_float(v) -> Optional[float]:
    try:
        f = float(v)
        if f != f:  # NaN
            return None
        return f
    except (TypeError, ValueError):
        return None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _event_to_json(event: Any) -> dict:
    """Serialize bus events to JSON-friendly dicts."""
    if isinstance(event, AgentPhaseChange):
        return {
            "type": "agent_phase",
            "agent": event.agent,
            "phase": event.phase,
            "detail": _detail_summary(event.detail),
            "incident_id": event.incident_id,
        }
    if isinstance(event, AgentToken):
        return {
            "type": "agent_token",
            "agent": event.agent,
            "token": event.token,
            "incident_id": event.incident_id,
        }
    if isinstance(event, AgentMessage):
        return {
            "type": "agent_message",
            "agent": event.agent,
            "text": event.text,
            "incident_id": event.incident_id,
        }
    if isinstance(event, AgentToolCall):
        return {
            "type": "tool_call",
            "agent": event.agent,
            "tool_id": event.tool_id,
            "tool_name": event.tool_name,
            "input": event.input,
            "incident_id": event.incident_id,
        }
    if isinstance(event, AgentToolResult):
        return {
            "type": "tool_result",
            "agent": event.agent,
            "tool_id": event.tool_id,
            "content": event.content,
            "is_error": event.is_error,
            "incident_id": event.incident_id,
        }
    if isinstance(event, HitlRequest):
        return {
            "type": "hitl_request",
            "incident_id": event.incident_id,
            "kind": event.kind,
            "summary": event.summary,
            "plan": event.plan.model_dump(mode="json") if hasattr(event.plan, "model_dump") else event.plan,
        }
    if isinstance(event, UserChatMessage):
        return {
            "type": "user_message",
            "incident_id": event.incident_id,
            "text": event.text,
        }
    if isinstance(event, RemediationPlanReady):
        return {
            "type": "plan_ready",
            "incident_id": event.incident_id,
            "plan": event.plan.model_dump(mode="json") if hasattr(event.plan, "model_dump") else event.plan,
        }
    if isinstance(event, PipelineDone):
        return {"type": "pipeline_done", "incident_id": event.incident_id}
    if isinstance(event, VulnerabilityReport):
        return {
            "type": "vulnerability_report",
            "generated_at": event.generated_at,
            "findings": [asdict(f) for f in event.findings],
        }
    if isinstance(event, IncidentEvent):
        return {"type": "observer_incident", "event": event.model_dump(mode="json")}
    if isinstance(event, dict):
        return {"type": event.get("type", "log"), **event}
    if is_dataclass(event):
        return {"type": type(event).__name__, **asdict(event)}
    return {"type": "unknown", "repr": repr(event)}


def _detail_summary(detail: Any) -> Optional[dict]:
    if detail is None:
        return None
    if hasattr(detail, "model_dump"):
        return detail.model_dump(mode="json")
    if isinstance(detail, (dict, list, str, int, float, bool)):
        return detail
    return {"repr": repr(detail)}


class WebSwarmServer:
    """Holds long-lived state (bus, orchestrator, observer, sockets)."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.bus: asyncio.Queue = asyncio.Queue()
        self.audit = AuditLog(config.audit_log_path)
        # Pending HITL futures keyed by (incident_id, kind)
        self._pending_approvals: dict[tuple[str, str], asyncio.Future] = {}
        # Operator chat injections per incident
        self.operator_guidance: dict[str, list[str]] = {}
        self.observer = ObserverAgent(config, self.bus)
        self.integrations = IntegrationRegistry(config)
        self.system_copilot = SystemCopilot(config, self.integrations)
        self.orchestrator = SwarmOrchestrator(
            config,
            hitl_callback=self._hitl_callback,
            event_bus=self.bus,
            guidance_provider=lambda incident_id: self.operator_guidance.get(incident_id, []),
        )
        # WebSocket connections + per-connection queues
        self._sockets: set[asyncio.Queue] = set()
        # Track active + recent incidents so the UI can recover after refresh
        self.incidents: dict[str, Incident] = {}
        # Observer-detected anomalies awaiting human "Investigate" click
        self._pending_incidents: dict[str, IncidentEvent] = {}
        self._pipeline_tasks: set[asyncio.Task] = set()

    async def start_background(self) -> None:
        asyncio.create_task(self.observer.run(), name="observer")
        asyncio.create_task(self._dispatch_loop(), name="bus_dispatch")

    async def _dispatch_loop(self) -> None:
        while True:
            event = await self.bus.get()
            # Observer-detected anomalies are queued for human review — the
            # pipeline only fires when the user clicks "Investigate" in the UI.
            if isinstance(event, IncidentEvent):
                self._pending_incidents[event.id] = event
            elif isinstance(event, PipelineDone):
                self._close_observer_incident(event.incident_id)
            payload = _event_to_json(event)
            await self._broadcast(payload)

    def _close_observer_incident(self, incident_id: str) -> None:
        """Free the observer's (service, trigger) lock so re-breaches can re-fire."""
        incident = self.incidents.get(incident_id)
        if incident is None or incident.event is None:
            return
        ev = incident.event
        if ev.trigger and ev.trigger != "manual":
            self.observer.close_incident(ev.service, ev.trigger)

    async def _broadcast(self, msg: dict) -> None:
        for q in list(self._sockets):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:  # pragma: no cover
                pass

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1024)
        self._sockets.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._sockets.discard(q)

    async def _start_pipeline(self, ev: IncidentEvent, description: Optional[str] = None) -> Incident:
        now = _now()
        incident = Incident(
            id=ev.id,
            event=ev,
            status=IncidentStatus.INVESTIGATING,
            created_at=now,
            updated_at=now,
        )
        self.incidents[incident.id] = incident
        task = asyncio.create_task(
            self.orchestrator.run(incident, description=description),
            name=f"pipeline-{incident.id}",
        )
        self._pipeline_tasks.add(task)
        task.add_done_callback(self._pipeline_tasks.discard)
        return incident

    async def manual_investigate(self, target: str) -> Incident:
        """Trigger a pipeline from free-text or an INC-ID."""
        now = _now()
        ev = IncidentEvent(
            id=new_incident_id(),
            service=self._guess_service(target),
            namespace=self.config.cluster.namespace,
            trigger="manual",
            value=0.0,
            threshold=0.0,
            first_seen=now,
            confirmed_at=now,
        )
        return await self._start_pipeline(ev, description=target)

    def _guess_service(self, text: str) -> str:
        lower = text.lower()
        for svc in self.config.services:
            if svc.name.lower() in lower:
                return svc.name
        return self.config.services[0].name if self.config.services else "unknown"

    async def _hitl_callback(self, req: HitlRequest) -> ApprovalDecision:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        key = (req.incident_id, req.kind)
        self._pending_approvals[key] = fut
        try:
            decision = await asyncio.wait_for(fut, timeout=1800.0)
            self.audit.write(
                "hitl_approval_web",
                incident=req.incident_id,
                kind=req.kind,
                approved=decision.approved_steps,
                rejected=decision.rejected,
            )
            return decision
        except asyncio.TimeoutError:
            self.audit.write(
                "hitl_timeout",
                incident=req.incident_id,
                kind=req.kind,
                note="no web operator response in 30min",
            )
            return ApprovalDecision(rejected=True, rejection_reason="timeout")
        finally:
            self._pending_approvals.pop(key, None)

    def resolve_approval(
        self, incident_id: str, decision: ApprovalDecision, kind: str = "heal",
    ) -> bool:
        fut = self._pending_approvals.get((incident_id, kind))
        if fut is None or fut.done():
            return False
        fut.set_result(decision)
        return True

    async def inject_user_message(self, incident_id: str, text: str) -> bool:
        if incident_id not in self.incidents:
            return False
        self.operator_guidance.setdefault(incident_id, []).append(text)
        await self.bus.put(UserChatMessage(incident_id=incident_id, text=text))
        return True


# ---- API models ------------------------------------------------------------

class InvestigateBody(BaseModel):
    target: str


class ApprovalBody(BaseModel):
    decision: str  # approve_all | reject | skip
    kind: str = "heal"  # heal | chaos_replay
    approved_steps: list[int] = []
    rejection_reason: Optional[str] = None


class InjectBody(BaseModel):
    text: str


class IntegrationUpdateBody(BaseModel):
    fields: dict[str, Any] = {}


class CustomIntegrationBody(BaseModel):
    name: str
    url: str


class CopilotMessage(BaseModel):
    role: str = "user"
    text: str = ""


class CopilotBody(BaseModel):
    message: str
    history: list[CopilotMessage] = []


def create_app(config: Config) -> FastAPI:
    server = WebSwarmServer(config)
    app = FastAPI(title="SRE Swarm")
    app.state.swarm = server

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.on_event("startup")
    async def _startup():
        await server.start_background()

    @app.get("/")
    async def root():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/system")
    async def system_page():
        return FileResponse(STATIC_DIR / "system.html")

    @app.get("/api/state")
    async def get_state():
        return {
            "cluster": config.cluster.model_dump(mode="json"),
            "services": [s.model_dump(mode="json") for s in config.services],
            "thresholds": config.thresholds.model_dump(mode="json"),
            "incidents": [i.model_dump(mode="json") for i in server.incidents.values()],
            "pending_incidents": [
                ev.model_dump(mode="json") for ev in server._pending_incidents.values()
            ],
        }

    @app.post("/api/investigate")
    async def investigate(body: InvestigateBody):
        if not body.target.strip():
            raise HTTPException(400, "target required")
        incident = await server.manual_investigate(body.target.strip())
        return {"incident_id": incident.id}

    @app.post("/api/incidents/{incident_id}/investigate")
    async def investigate_pending(incident_id: str):
        ev = server._pending_incidents.pop(incident_id, None)
        if ev is None:
            raise HTTPException(404, f"no pending incident {incident_id}")
        incident = await server._start_pipeline(ev)
        return {"incident_id": incident.id}

    @app.post("/api/incidents/{incident_id}/dismiss")
    async def dismiss_pending(incident_id: str):
        ev = server._pending_incidents.pop(incident_id, None)
        if ev is None:
            raise HTTPException(404, f"no pending incident {incident_id}")
        server.observer.close_incident(ev.service, ev.trigger)
        return {"ok": True}

    @app.post("/api/approval/{incident_id}")
    async def approve(incident_id: str, body: ApprovalBody):
        if body.decision == "approve_all":
            decision = ApprovalDecision(approved_steps=body.approved_steps)
        elif body.decision == "reject":
            decision = ApprovalDecision(
                rejected=True, rejection_reason=body.rejection_reason or "unspecified"
            )
        else:
            decision = ApprovalDecision()
        ok = server.resolve_approval(incident_id, decision, kind=body.kind)
        if not ok:
            raise HTTPException(404, f"no pending '{body.kind}' approval for this incident")
        return {"ok": True}

    @app.post("/api/incident/{incident_id}/inject")
    async def inject(incident_id: str, body: InjectBody):
        text = body.text.strip()
        if not text:
            raise HTTPException(400, "text required")
        ok = await server.inject_user_message(incident_id, text)
        if not ok:
            raise HTTPException(404, "unknown incident")
        return {"ok": True}

    @app.get("/api/incident/{incident_id}/replicate")
    async def replicate(incident_id: str):
        incident = server.incidents.get(incident_id)
        if incident is None:
            raise HTTPException(404, "unknown incident")
        return build_replication_recipe(incident, config)

    @app.get("/api/topology")
    async def topology():
        """Service dependency graph + live health, used by the topology widget.

        Edges are derived from the demo e-commerce flow. Health for each node is
        the same metric set the Observer watches, queried fresh via Prometheus.
        """
        services = list(config.services)
        edges = _derive_edges([s.name for s in services])
        nodes = []
        for svc in services:
            err = await _safe_metric(svc.name, "error_rate")
            lat = await _safe_metric(svc.name, "p99_latency")
            health = _health_label(err, lat, config.thresholds)
            nodes.append({
                "id": svc.name,
                "label": svc.name.removesuffix("-service") or svc.name,
                "port": svc.port,
                "health": health,
                "metrics": {
                    "error_rate_pct": err,
                    "p99_latency_ms": lat,
                },
            })
        return {"nodes": nodes, "edges": edges}

    @app.get("/api/metrics/{service}")
    async def metrics(service: str, window_s: int = 300, step_s: int = 15):
        prefix = service.removesuffix("-service").replace("-", "_")
        end = datetime.now(timezone.utc).timestamp()
        start = end - max(60, window_s)
        queries = {
            "error_rate_pct": (
                f'sum(rate({prefix}_requests_total{{status=~"5.."}}[1m])) '
                f'/ sum(rate({prefix}_requests_total[1m])) * 100'
            ),
            "p99_latency_ms": (
                f'histogram_quantile(0.99, sum(rate({prefix}_request_duration_seconds_bucket[5m])) by (le)) * 1000'
            ),
        }
        out: dict[str, list] = {}
        async with httpx.AsyncClient(timeout=config.prometheus.timeout_s) as client:
            for key, q in queries.items():
                try:
                    r = await client.get(
                        f"{config.prometheus.url.rstrip('/')}/api/v1/query_range",
                        params={"query": q, "start": start, "end": end, "step": step_s},
                    )
                    r.raise_for_status()
                    payload = r.json().get("data", {})
                    result = payload.get("result") or []
                    samples = result[0].get("values", []) if result else []
                    out[key] = [
                        {"t": float(t), "v": _safe_float(v)}
                        for (t, v) in samples
                    ]
                except Exception:  # noqa: BLE001
                    out[key] = []
        return {"service": service, "start": start, "end": end, "step_s": step_s, "series": out}

    @app.get("/api/sidebar/logs/{service}")
    async def sidebar_logs(service: str, minutes: int = 15, limit: int = 30):
        """Recent ERROR/CRITICAL log events for one service, for the Logs side tab."""
        prefix = service.removesuffix("-service").replace("-", "_")
        params = {
            "service": prefix,
            "minutes": int(minutes),
            "limit": int(limit),
            "q": "level:error OR level:critical OR error OR exception OR timeout",
        }
        url = f"{config.splunk.url.rstrip('/')}/api/search"
        try:
            async with httpx.AsyncClient(timeout=config.splunk.timeout_s) as c:
                r = await c.get(url, params=params)
            if r.status_code != 200:
                return {"service": service, "events": [], "count": 0, "note": f"splunk HTTP {r.status_code}"}
            payload = r.json()
            events = payload.get("events", [])
            return {
                "service": service,
                "events": events,
                "count": payload.get("count", len(events)),
            }
        except httpx.HTTPError as exc:
            return {"service": service, "events": [], "count": 0, "note": f"splunk offline: {exc.__class__.__name__}"}

    @app.get("/api/sidebar/github/{service}")
    async def sidebar_github(service: str):
        """Recent commits + PRs touching one service.

        Currently returns an empty result with a note pointing at the demo-apps
        repo; the real integration will come once we wire a gh client.
        """
        return {
            "service": service,
            "commits": [],
            "prs": [],
            "note": "GitHub integration not configured — see github.com/ssgupta905/sre-swarm-demo-apps",
        }

    @app.get("/api/integrations")
    async def list_integrations():
        return {"integrations": server.integrations.list()}

    @app.post("/api/integrations/{name}/test")
    async def test_integration(name: str):
        result = await server.integrations.test(name)
        if result is None:
            raise HTTPException(404, f"unknown integration {name}")
        return result

    @app.post("/api/integrations/test-all")
    async def test_all_integrations():
        return {"integrations": await server.integrations.test_all()}

    @app.put("/api/integrations/{name}")
    async def update_integration(name: str, body: IntegrationUpdateBody):
        result = server.integrations.update(name, body.fields)
        if result is None:
            raise HTTPException(404, f"unknown integration {name}")
        return result

    @app.post("/api/integrations")
    async def add_custom_integration(body: CustomIntegrationBody):
        try:
            return server.integrations.add_custom(body.name, body.url)
        except ValueError as exc:
            raise HTTPException(409, str(exc))

    @app.post("/api/system/copilot")
    async def system_copilot(body: CopilotBody):
        text = (body.message or "").strip()
        if not text:
            raise HTTPException(400, "empty message")
        history = [m.model_dump() for m in body.history]
        try:
            result = await server.system_copilot.respond(text, history=history)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, f"copilot error: {exc}")
        return result

    @app.websocket("/ws")
    async def ws(socket: WebSocket):
        await socket.accept()
        q = server.subscribe()
        # Send a snapshot so a freshly connected client can rebuild state.
        await socket.send_text(json.dumps({
            "type": "snapshot",
            "incidents": [i.model_dump(mode="json") for i in server.incidents.values()],
        }))
        try:
            while True:
                msg = await q.get()
                await socket.send_text(json.dumps(msg, default=str))
        except WebSocketDisconnect:
            pass
        finally:
            server.unsubscribe(q)

    return app


def run(config: Config, host: str = "127.0.0.1", port: int = 8080) -> None:
    import uvicorn

    uvicorn.run(create_app(config), host=host, port=port, log_level="info")
