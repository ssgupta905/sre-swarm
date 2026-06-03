from enum import Enum
from typing import Optional

from pydantic import BaseModel


class BlastRadius(str, Enum):
    READ_ONLY = "read_only"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @classmethod
    def _missing_(cls, value):
        if isinstance(value, str):
            try:
                return cls(value.lower())
            except ValueError:
                return None
        return None


class RemediationStep(BaseModel):
    step_number: int
    action: str
    kubectl_command: Optional[str] = None
    blast_radius: BlastRadius
    rollback_cmd: Optional[str] = None
    expected_effect: str


class RemediationPlan(BaseModel):
    incident_id: str
    steps: list[RemediationStep]
    estimated_recovery_min: int
    confidence: float
    requires_individual_confirm: list[int]
