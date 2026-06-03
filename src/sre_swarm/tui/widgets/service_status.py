"""Left-panel service status list."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widget import Widget
from textual.widgets import Static


class ServiceStatus(Widget):
    DEFAULT_CSS = """ServiceStatus { height: auto; }"""

    def __init__(self, services: list[str]) -> None:
        super().__init__()
        self._services = services
        self._rows: dict[str, Static] = {}

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("[b]SERVICES[/]", classes="section-title")
            for svc in self._services:
                row = Static(self._fmt_row(svc, None), id=f"svc-{svc}")
                self._rows[svc] = row
                yield row

    def _fmt_row(self, name: str, err_pct: float | None) -> str:
        if err_pct is None:
            dot = "[dim]●[/]"
            tail = "[dim]—[/]"
        elif err_pct < 1:
            dot = "[green]●[/]"
            tail = f"{err_pct:.1f}%"
        elif err_pct < 5:
            dot = "[yellow]●[/]"
            tail = f"{err_pct:.1f}%"
        else:
            dot = "[red]●[/]"
            tail = f"{err_pct:.1f}%"
        return f"{dot} {name:<24} {tail}"

    def update_service(self, name: str, err_pct: float | None) -> None:
        row = self._rows.get(name)
        if row is not None:
            row.update(self._fmt_row(name, err_pct))
