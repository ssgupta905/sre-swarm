"""Predict agent prompt builder."""
from __future__ import annotations

import json

from ..models import Incident
from .prompts import PREDICT_SYSTEM_PROMPT  # noqa: F401  re-export


def build_predict_user_prompt(incident: Incident) -> str:
    context = {
        "incident_id": incident.id,
        "event": incident.event.model_dump(mode="json"),
        "triage": incident.triage.model_dump(mode="json") if incident.triage else None,
        "rca": incident.rca.model_dump(mode="json") if incident.rca else None,
        "replay": incident.replay.model_dump(mode="json") if incident.replay else None,
    }
    return (
        "Forecast the trajectory of the incident below.\n"
        "Return ONLY JSON matching the PredictResult schema:\n"
        '  {"will_self_heal": bool, "self_heal_reason": str|null, '
        '"cascade_risk": "low|medium|high|critical", '
        '"mttr_without_min": int, "risk_level": "low|medium|high|critical"}\n\n'
        f"Context:\n{json.dumps(context, default=str, indent=2)}\n"
    )
