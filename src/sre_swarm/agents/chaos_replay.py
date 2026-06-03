"""Chaos Replay agent prompt builder (FR-034).

The agent translates the top RCA hypothesis into a Chaos Mesh CRD, applies it
to the sandbox namespace only, polls for similarity, then cleans up.
"""
from __future__ import annotations

import json

from ..models import Incident
from .prompts import CHAOS_REPLAY_SYSTEM_PROMPT  # noqa: F401  re-export


def build_chaos_replay_user_prompt(incident: Incident, sandbox_namespace: str) -> str:
    event = incident.event.model_dump(mode="json")
    service = event.get("service") or "unknown-service"
    short = service.split("-service")[0]
    context = {
        "incident_id": incident.id,
        "event": event,
        "triage": incident.triage.model_dump(mode="json") if incident.triage else None,
        "rca": incident.rca.model_dump(mode="json") if incident.rca else None,
        "sandbox_namespace": sandbox_namespace,
    }
    return (
        f"Reproduce the failure on service='{short}' to validate the top RCA "
        "hypothesis. You MUST call chaos_inject first, then prometheus_query, "
        "then chaos_reset. Returning a no-action JSON is not acceptable.\n\n"
        f"If you fall back to chaos_replay_apply (Chaos Mesh), it MUST target "
        f"namespace='{sandbox_namespace}'. NEVER touch the production namespace.\n\n"
        "Return ONLY JSON matching the ReplayResult schema.\n\n"
        f"Context:\n{json.dumps(context, default=str, indent=2)}\n"
    )
