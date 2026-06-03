"""Heal agent — plan generation and step execution helpers."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

from ..models import BlastRadius, Incident, RemediationPlan, RemediationStep
from .prompts import (  # noqa: F401  re-exported for callers
    HEAL_EXECUTE_SYSTEM_PROMPT,
    HEAL_PLAN_SYSTEM_PROMPT,
)


def build_heal_plan_user_prompt(incident: Incident) -> str:
    service = (incident.event.service if incident.event else None) or "unknown-service"
    namespace = (incident.event.namespace if incident.event else None) or "default"
    context = {
        "incident_id": incident.id,
        "event": incident.event.model_dump(mode="json"),
        "triage": incident.triage.model_dump(mode="json") if incident.triage else None,
        "rca": incident.rca.model_dump(mode="json") if incident.rca else None,
        "replay": incident.replay.model_dump(mode="json") if incident.replay else None,
        "predict": incident.predict.model_dump(mode="json") if incident.predict else None,
    }
    return (
        "Generate a remediation plan. Return ONLY JSON matching the RemediationPlan schema:\n"
        "  {\n"
        f'    "incident_id": "{incident.id}",\n'
        '    "steps": [ {"step_number": int, "action": str, '
        '"kubectl_command": str|null, '
        '"blast_radius": "read_only|low|medium|high|critical", '
        '"rollback_cmd": str|null, "expected_effect": str} ],\n'
        '    "estimated_recovery_min": int, "confidence": float, '
        '"requires_individual_confirm": [int]\n'
        "  }\n\n"
        "GROUNDING RULES (must follow):\n"
        f"- The affected service is EXACTLY '{service}'. Every kubectl_command "
        f"and rollback_cmd must reference this service name verbatim. Do NOT "
        f"target any other service.\n"
        f"- The cluster namespace is '{namespace}'. Every kubectl_command MUST "
        f"include '-n {namespace}' (or `--namespace={namespace}`). Never use 'default'.\n"
        "- If you cannot ground a step in real evidence from the context below, "
        "omit it. A 3-step grounded plan beats a 5-step hallucinated plan.\n\n"
        f"Context:\n{json.dumps(context, default=str, indent=2)}\n"
    )


def build_heal_execute_prompt(step: RemediationStep, service: str, namespace: str) -> str:
    return HEAL_EXECUTE_SYSTEM_PROMPT.format(
        step_number=step.step_number,
        action=step.action,
        kubectl_command=(
            f"Suggested kubectl command (use as-is unless clearly wrong): {step.kubectl_command}"
            if step.kubectl_command
            else "no direct kubectl command supplied; choose the right write tool"
        ),
        service=service,
        namespace=namespace,
        blast_radius=(step.blast_radius.value if hasattr(step.blast_radius, "value") else step.blast_radius),
    )


def annotate_requires_individual(plan: RemediationPlan) -> RemediationPlan:
    """Always require individual confirm for HIGH or CRITICAL blast radius."""
    required = {
        step.step_number
        for step in plan.steps
        if step.blast_radius in (BlastRadius.HIGH, BlastRadius.CRITICAL)
    }
    merged = sorted(set(plan.requires_individual_confirm) | required)
    plan.requires_individual_confirm = merged
    return plan


@dataclass
class ApprovalDecision:
    approved_steps: list[int] = field(default_factory=list)
    rejected: bool = False
    modified_plan: Optional[RemediationPlan] = None
    rejection_reason: Optional[str] = None
