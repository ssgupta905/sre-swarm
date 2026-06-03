"""Triage agent prompt builders."""
from __future__ import annotations

import json
from typing import Optional

from ..models import Incident, IncidentEvent
from .prompts import TRIAGE_SYSTEM_PROMPT  # noqa: F401  re-exported for callers


def build_triage_user_prompt(incident: Incident, description: Optional[str] = None) -> str:
    """Render the user-side prompt that asks Triage to classify an incident."""
    event_data = _event_to_dict(incident.event) if incident.event else {}
    body = {
        "incident_id": incident.id,
        "trigger_event": event_data,
        "user_description": description,
        "namespace": incident.event.namespace if incident.event else None,
    }
    return (
        "Classify the following incident and return ONLY JSON matching the "
        "TriageResult schema.\n\n"
        f"Incident context:\n{json.dumps(body, default=str, indent=2)}\n"
    )


def _event_to_dict(event: IncidentEvent) -> dict:
    data = event.model_dump(mode="json")
    return data
