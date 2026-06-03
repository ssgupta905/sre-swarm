"""Textual app entry point — wires Observer, Orchestrator, and screens."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional

from textual.app import App
from textual.binding import Binding

from ..agents.events import (
    AgentPhaseChange,
    AgentToken,
    PipelineDone,
    RemediationPlanReady,
)
from ..agents.heal import ApprovalDecision
from ..agents.observer import ObserverAgent, new_incident_id
from ..agents.orchestrator import SwarmOrchestrator
from ..audit import AuditLog
from ..config import Config
from ..models import (
    BlastRadius,
    Incident,
    IncidentEvent,
    IncidentStatus,
    RemediationPlan,
)
from .screens.incident import IncidentScreen
from .screens.monitor import MonitorScreen


class SRESwarmApp(App):
    CSS_PATH = "theme.tcss"
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("?", "show_help", "Help"),
    ]

    def __init__(self, config: Config) -> None:
        super().__init__()
        self.config = config
        self.event_bus: asyncio.Queue = asyncio.Queue()
        self.observer = ObserverAgent(config, self.event_bus)
        self.orchestrator = SwarmOrchestrator(
            config,
            hitl_callback=self._hitl_callback,
            event_bus=self.event_bus,
        )
        self.audit = AuditLog(config.audit_log_path)
        self._monitor_screen: Optional[MonitorScreen] = None
        self._incident_screen: Optional[IncidentScreen] = None
        self._pipeline_task: Optional[asyncio.Task] = None
        self._observer_task: Optional[asyncio.Task] = None
        self._event_task: Optional[asyncio.Task] = None
        self._active_incident: Optional[Incident] = None

    async def on_mount(self) -> None:
        self._monitor_screen = MonitorScreen(self.config)
        await self.push_screen(self._monitor_screen)
        self._observer_task = asyncio.create_task(self.observer.run())
        self._event_task = asyncio.create_task(self._process_events())

    async def on_unmount(self) -> None:
        for task in (self._pipeline_task, self._observer_task, self._event_task):
            if task is not None and not task.done():
                task.cancel()

    async def action_manual_investigate(self) -> None:
        """`i` from Screen A: prompt-less manual trigger using current namespace."""
        if self._pipeline_task is not None and not self._pipeline_task.done():
            self.notify("A pipeline is already running.")
            return
        now = datetime.now(timezone.utc)
        ev = IncidentEvent(
            id=new_incident_id(),
            service=(self.config.services[0].name if self.config.services else "unknown"),
            namespace=self.config.cluster.namespace,
            trigger="manual",
            value=0.0,
            threshold=0.0,
            first_seen=now,
            confirmed_at=now,
        )
        incident = Incident(id=ev.id, event=ev, created_at=now, updated_at=now)
        await self._start_pipeline(incident, description="manual trigger from TUI")

    async def _start_pipeline(self, incident: Incident, description: str | None = None) -> None:
        self._active_incident = incident
        self._incident_screen = IncidentScreen(incident)
        await self.push_screen(self._incident_screen)
        self._pipeline_task = asyncio.create_task(
            self.orchestrator.run(incident, description=description)
        )

    async def _process_events(self) -> None:
        while True:
            event = await self.event_bus.get()
            try:
                await self._dispatch(event)
            except Exception as exc:  # noqa: BLE001
                self.notify(f"event dispatch error: {exc}")

    async def _dispatch(self, event) -> None:
        if isinstance(event, IncidentEvent):
            await self._on_observer_incident(event)
        elif isinstance(event, AgentPhaseChange):
            if self._incident_screen is not None:
                self._incident_screen.update_phase(event.agent, event.phase, event.detail)
        elif isinstance(event, AgentToken):
            if self._incident_screen is not None:
                self._incident_screen.append_token(event.agent, event.token)
        elif isinstance(event, RemediationPlanReady):
            # Phase 4 CLI handles HITL inline; TUI shows the plan via agent card detail.
            pass
        elif isinstance(event, PipelineDone):
            if self._monitor_screen is not None:
                self._monitor_screen.set_incident_count(0)
        elif isinstance(event, dict) and event.get("type") == "observer_log":
            if self._monitor_screen is not None:
                self._monitor_screen.observer_log(event.get("message", ""))

    async def _on_observer_incident(self, event: IncidentEvent) -> None:
        if self._monitor_screen is not None:
            self._monitor_screen.set_incident_count(1)
        now = datetime.now(timezone.utc)
        incident = Incident(
            id=event.id,
            event=event,
            status=IncidentStatus.INVESTIGATING,
            created_at=now,
            updated_at=now,
        )
        await self._start_pipeline(incident)

    async def _hitl_callback(self, plan: RemediationPlan) -> ApprovalDecision:
        """Phase 3 TUI placeholder — approval screen lands later. Auto-skip for now."""
        self.audit.write(
            "hitl_skipped_tui",
            incident=plan.incident_id,
            steps=[s.step_number for s in plan.steps],
            note="approval screen not yet implemented",
        )
        return ApprovalDecision()

    async def action_show_help(self) -> None:
        self.notify(
            "i=investigate  ·  c=chaos lab  ·  ESC=back  ·  q=quit",
            timeout=6,
        )
