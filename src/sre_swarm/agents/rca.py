"""RCA agent prompt builder."""
from __future__ import annotations

import json

from ..models import Incident
from .prompts import RCA_SYSTEM_PROMPT  # noqa: F401  re-export


def build_rca_user_prompt(incident: Incident) -> str:
    """Render the prompt for the RCA agent. Triage output is the primary context."""
    context = {
        "incident_id": incident.id,
        "event": incident.event.model_dump(mode="json"),
        "triage": (
            incident.triage.model_dump(mode="json") if incident.triage else None
        ),
    }
    return (
        "Investigate the root cause of the following incident.\n"
        "Use the tools to fetch logs, events, and metric history.\n"
        "Return ONLY JSON matching the RCAResult schema.\n\n"
        f"Context:\n{json.dumps(context, default=str, indent=2)}\n"
    )
