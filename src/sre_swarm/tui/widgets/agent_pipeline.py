"""Vertical pipeline of AgentCard widgets driven by orchestrator events."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widget import Widget

from .agent_card import AgentCard


AGENT_ORDER = ["observer", "triage", "rca", "chaos_replay", "predict", "heal"]


class AgentPipeline(Widget):
    DEFAULT_CSS = """
    AgentPipeline { height: auto; }
    """

    def compose(self) -> ComposeResult:
        with Vertical():
            for agent in AGENT_ORDER:
                yield AgentCard(agent)

    def get_card(self, agent: str) -> AgentCard | None:
        try:
            return self.query_one(f"#agent-{agent}", AgentCard)
        except Exception:  # noqa: BLE001
            return None

    def update_phase(self, agent: str, phase: str, summary: str | None = None) -> None:
        card = self.get_card(agent)
        if card is None:
            return
        card.phase = phase
        if summary is not None:
            card.summary = summary

    def append_token(self, agent: str, token: str) -> None:
        card = self.get_card(agent)
        if card is None:
            return
        card.append_token(token)

    def reset(self) -> None:
        for agent in AGENT_ORDER:
            card = self.get_card(agent)
            if card is not None:
                card.phase = "pending"
                card.summary = ""
                card._lines = [""]
