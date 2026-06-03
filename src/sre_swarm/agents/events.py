"""Lightweight value-objects pushed onto the event bus by the orchestrator.

The TUI consumes these to update widgets; non-TUI callers can ignore them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(slots=True)
class AgentPhaseChange:
    agent: str
    phase: str  # pending | running | done | waiting | error | fired
    detail: Optional[Any] = None
    incident_id: Optional[str] = None


@dataclass(slots=True)
class AgentToken:
    agent: str
    token: str
    incident_id: Optional[str] = None


@dataclass(slots=True)
class AgentMessage:
    """A complete text block emitted by the agent (one settled bubble)."""
    agent: str
    text: str
    incident_id: Optional[str] = None


@dataclass(slots=True)
class AgentToolCall:
    agent: str
    tool_id: str
    tool_name: str
    input: dict = field(default_factory=dict)
    incident_id: Optional[str] = None


@dataclass(slots=True)
class AgentToolResult:
    agent: str
    tool_id: str
    content: Any
    is_error: bool = False
    incident_id: Optional[str] = None


@dataclass(slots=True)
class HitlRequest:
    """Emitted when an agent pauses awaiting operator approval.

    `kind` distinguishes which gate is open ("heal" | "chaos_replay" | ...).
    For heal gates `plan` carries the RemediationPlan; for others it can hold
    a short structured summary the UI uses to render the bubble.
    """
    incident_id: str
    kind: str = "heal"
    plan: Any = None
    summary: str = ""


@dataclass(slots=True)
class UserMessage:
    """An operator-typed message injected into an incident's chat."""
    incident_id: str
    text: str


@dataclass(slots=True)
class RemediationPlanReady:
    incident_id: str
    plan: Any


@dataclass(slots=True)
class Narration:
    """Copilot-style streaming narration from the orchestrator.

    `phase` is one of: "intent" (about to do X) or "summary" (just finished X).
    `step` is the agent / stage being narrated.
    """
    incident_id: str
    step: str
    phase: str
    text: str


@dataclass(slots=True)
class PipelineDone:
    incident_id: str


@dataclass(slots=True)
class Finding:
    """A single concerning signal detected by the observer."""
    source: str          # prometheus | health | splunk
    service: str
    signal: str          # error_rate | p99_latency | pod_restarts | db_connections | health | error_volume
    severity: str        # low | medium | high | critical
    value: Any
    threshold: Any = None
    note: str = ""


@dataclass(slots=True)
class VulnerabilityReport:
    """Snapshot of all current concerns across every source. Emitted each poll."""
    generated_at: str
    findings: list = field(default_factory=list)
