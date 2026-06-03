"""Single-agent card widget. Streams chain-of-thought tokens in-place."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static


PHASE_LABEL = {
    "pending": "PENDING",
    "running": "RUNNING",
    "done": "DONE",
    "waiting": "WAITING",
    "error": "ERROR",
    "fired": "FIRED",
}


class AgentCard(Widget):
    MAX_VISIBLE_LINES = 6

    phase: reactive[str] = reactive("pending")
    summary: reactive[str] = reactive("")

    def __init__(self, agent: str) -> None:
        super().__init__(id=f"agent-{agent}")
        self.agent = agent
        self._lines: list[str] = [""]
        self.add_class("-pending")

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self._header(), id=f"hdr-{self.agent}")
            yield Static("", id=f"body-{self.agent}")

    def _header(self) -> str:
        badge = PHASE_LABEL.get(self.phase, self.phase.upper())
        return f"[b]{self.agent}[/]  · {badge}"

    def watch_phase(self, _old: str, _new: str) -> None:
        for cls in ("-pending", "-running", "-done", "-waiting", "-error", "-fired"):
            self.remove_class(cls)
        self.add_class(f"-{_new}")
        self._refresh_header()

    def watch_summary(self, _old: str, _new: str) -> None:
        self._refresh_body()

    def _refresh_header(self) -> None:
        try:
            self.query_one(f"#hdr-{self.agent}", Static).update(self._header())
        except Exception:  # noqa: BLE001
            pass

    def _refresh_body(self) -> None:
        try:
            if self.summary:
                self.query_one(f"#body-{self.agent}", Static).update(self.summary)
            else:
                tail = self._lines[-self.MAX_VISIBLE_LINES :]
                self.query_one(f"#body-{self.agent}", Static).update("\n".join(tail))
        except Exception:  # noqa: BLE001
            pass

    def append_token(self, token: str) -> None:
        if not self._lines:
            self._lines = [""]
        if "\n" in token:
            parts = token.split("\n")
            self._lines[-1] += parts[0]
            self._lines.extend(parts[1:])
        else:
            self._lines[-1] += token
        if len(self._lines) > 50:
            self._lines = self._lines[-50:]
        if not self.summary:
            self._refresh_body()
