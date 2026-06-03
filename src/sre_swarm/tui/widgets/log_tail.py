"""Scrolling log tail widget."""
from __future__ import annotations

from textual.widget import Widget
from textual.widgets import Log


class LogTail(Widget):
    DEFAULT_CSS = """LogTail { height: 6; }"""

    def __init__(self, title: str = "log", max_lines: int = 200) -> None:
        super().__init__()
        self._title = title
        self._max_lines = max_lines
        self._log: Log | None = None

    def compose(self):
        self._log = Log(highlight=False, auto_scroll=True, max_lines=self._max_lines)
        yield self._log

    def write(self, line: str) -> None:
        if self._log is not None:
            self._log.write_line(line)
