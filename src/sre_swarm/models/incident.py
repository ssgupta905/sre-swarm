from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Optional

from pydantic import BaseModel

if TYPE_CHECKING:
    from .triage import TriageResult
    from .rca import RCAResult
    from .chaos import ReplayResult
    from .predict import PredictResult
    from .heal import RemediationPlan


class IncidentStatus(str, Enum):
    OPEN = "open"
    INVESTIGATING = "investigating"
    HEALING = "healing"
    RESOLVED = "resolved"
    USER_REJECTED = "user_rejected"


class Urgency(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @classmethod
    def _missing_(cls, value):  # tolerate uppercase agent output
        if isinstance(value, str):
            try:
                return cls(value.lower())
            except ValueError:
                return None
        return None


class IncidentEvent(BaseModel):
    id: str
    service: str
    namespace: str
    trigger: str
    value: float
    threshold: float
    first_seen: datetime
    confirmed_at: datetime


class Incident(BaseModel):
    id: str
    event: IncidentEvent
    status: IncidentStatus = IncidentStatus.OPEN
    urgency: Optional[Urgency] = None
    created_at: datetime
    updated_at: datetime
    resolved_at: Optional[datetime] = None
    triage: Optional["TriageResult"] = None
    rca: Optional["RCAResult"] = None
    replay: Optional["ReplayResult"] = None
    predict: Optional["PredictResult"] = None
    plan: Optional["RemediationPlan"] = None
