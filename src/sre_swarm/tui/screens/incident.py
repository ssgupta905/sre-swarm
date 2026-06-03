"""Screen B — incident active view with streaming agent pipeline."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Static

from ...models import Incident
from ..widgets.agent_pipeline import AgentPipeline
from ..widgets.log_tail import LogTail


class IncidentScreen(Screen):
    BINDINGS = [
        Binding("p", "pause", "Pause"),
        Binding("l", "logs", "Full logs"),
        Binding("escape", "back", "Back"),
    ]

    def __init__(self, incident: Incident) -> None:
        super().__init__()
        self.incident = incident
        self.started_at = datetime.now(timezone.utc)
        self._topbar: Optional[Static] = None
        self._pipeline: Optional[AgentPipeline] = None
        self._logs: Optional[LogTail] = None

    def compose(self) -> ComposeResult:
        self._topbar = Static(self._render_topbar(), id="topbar")
        yield self._topbar

        with Horizontal():
            with Vertical(classes="panel", id="left-panel"):
                yield Static("[b]AFFECTED SERVICES[/]", classes="section-title")
                yield Static(self._affected_services_line())
                yield Static("[b]LIVE METRICS[/]", classes="section-title")
                yield Static(self._metrics_summary())
            with Vertical(classes="panel"):
                yield Static("[b]AGENT PIPELINE[/]", classes="section-title")
                self._pipeline = AgentPipeline()
                yield self._pipeline

        self._logs = LogTail("incident-logs")
        yield self._logs
        yield Static(
            "[i]p[/]=pause  [i]l[/]=full logs  [i]ESC[/]=back",
            id="keybar",
        )

    def _render_topbar(self) -> str:
        urgency = self.incident.urgency.value if self.incident.urgency else "—"
        service = self.incident.event.service if self.incident.event else "—"
        elapsed = (datetime.now(timezone.utc) - self.started_at).total_seconds()
        return (
            f"sre-swarm · [b]{self.incident.id}[/] · urgency:[b]{urgency}[/] · "
            f"svc:{service} · {int(elapsed)}s · swarm active"
        )

    def _affected_services_line(self) -> str:
        if self.incident.triage and self.incident.triage.affected_services:
            return " · ".join(self.incident.triage.affected_services)
        return self.incident.event.service if self.incident.event else "(unknown)"

    def _metrics_summary(self) -> str:
        ev = self.incident.event
        return (
            f"trigger: {ev.trigger}\n"
            f"value: {ev.value:g}\n"
            f"threshold: {ev.threshold:g}"
        )

    def update_phase(self, agent: str, phase: str, summary=None) -> None:
        if self._pipeline is not None:
            text = self._summary_for(summary) if summary is not None else None
            self._pipeline.update_phase(agent, phase, text)
        if agent == "triage" and phase == "done":
            self.refresh_topbar()

    def append_token(self, agent: str, token: str) -> None:
        if self._pipeline is not None:
            self._pipeline.append_token(agent, token)

    def write_log(self, line: str) -> None:
        if self._logs is not None:
            self._logs.write(line)

    def refresh_topbar(self) -> None:
        if self._topbar is not None:
            self._topbar.update(self._render_topbar())

    def _summary_for(self, detail) -> str:
        try:
            if hasattr(detail, "top_cause"):
                return f"top cause: {detail.top_cause[:80]}"
            if hasattr(detail, "validation"):
                return f"validation: {detail.validation.value} ({detail.similarity_score:.2f})"
            if hasattr(detail, "risk_level"):
                return f"risk: {detail.risk_level.value} · mttr {detail.mttr_without_min}m"
            if hasattr(detail, "category"):
                return f"category: {detail.category.value} · urgency {detail.urgency.value}"
            if hasattr(detail, "steps"):
                return f"plan: {len(detail.steps)} steps · conf {detail.confidence:.2f}"
        except Exception:  # noqa: BLE001
            pass
        return ""

    async def action_pause(self) -> None:
        self.app.notify("Pause not yet implemented (orchestrator does not yet support pause).")

    async def action_logs(self) -> None:
        self.app.notify("Full log view not yet implemented (Phase 5).")

    async def action_back(self) -> None:
        self.app.pop_screen()
