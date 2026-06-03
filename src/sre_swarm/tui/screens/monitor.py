"""Screen A — normal monitoring view."""
from __future__ import annotations

from typing import Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Static

from ...config import Config
from ..widgets.log_tail import LogTail
from ..widgets.service_status import ServiceStatus


class MonitorScreen(Screen):
    BINDINGS = [
        Binding("i", "investigate", "Investigate"),
        Binding("c", "chaos_lab", "Chaos lab"),
        Binding("q", "quit_app", "Quit"),
    ]

    def __init__(self, config: Config) -> None:
        super().__init__()
        self.config = config
        self._service_panel: Optional[ServiceStatus] = None
        self._observer_log: Optional[LogTail] = None
        self._heartbeat: Optional[Static] = None
        self._incident_summary: Optional[Static] = None

    def compose(self) -> ComposeResult:
        ns = self.config.cluster.namespace
        ctx = self.config.cluster.context
        self._heartbeat = Static(
            f"sre-swarm  ·  cluster:{ctx}  ·  ns:{ns}  ·  observer: [dim]●[/]",
            id="topbar",
        )
        yield self._heartbeat

        with Horizontal():
            with Vertical(classes="panel", id="left-panel"):
                self._service_panel = ServiceStatus(
                    [s.name for s in self.config.services]
                )
                yield self._service_panel
                yield Static("[b]INCIDENTS[/]", classes="section-title")
                self._incident_summary = Static("No open incidents")
                yield self._incident_summary
            with Vertical(classes="panel"):
                yield Static("[b]OBSERVER AGENT[/]", classes="section-title")
                self._observer_log = LogTail("observer")
                yield self._observer_log

        yield Static(
            "[i]i[/]=investigate  [i]c[/]=chaos lab  [i]q[/]=quit",
            id="keybar",
        )

    def update_service(self, name: str, err_pct: float | None) -> None:
        if self._service_panel is not None:
            self._service_panel.update_service(name, err_pct)

    def observer_log(self, line: str) -> None:
        if self._observer_log is not None:
            self._observer_log.write(line)

    def set_heartbeat_ok(self, ok: bool) -> None:
        if self._heartbeat is None:
            return
        ns = self.config.cluster.namespace
        ctx = self.config.cluster.context
        dot = "[green]●[/]" if ok else "[red]●[/]"
        self._heartbeat.update(
            f"sre-swarm  ·  cluster:{ctx}  ·  ns:{ns}  ·  observer: {dot}"
        )

    def set_incident_count(self, count: int) -> None:
        if self._incident_summary is not None:
            self._incident_summary.update(
                "No open incidents" if count == 0 else f"{count} open"
            )

    async def action_investigate(self) -> None:
        await self.app.action_manual_investigate()

    async def action_chaos_lab(self) -> None:
        self.app.notify("Chaos Lab not yet implemented (Phase 5).")

    async def action_quit_app(self) -> None:
        await self.app.action_quit()
