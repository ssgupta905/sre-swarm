from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

from .incident import Urgency


class IncidentCategory(str, Enum):
    LATENCY = "latency"
    ERROR_RATE = "error_rate"
    CRASH_LOOP = "crash_loop"
    RESOURCE = "resource"
    DEPENDENCY = "dependency"
    APP_BUG = "app_bug"
    DATABASE = "database"

    @classmethod
    def _missing_(cls, value):
        if isinstance(value, str):
            try:
                return cls(value.lower())
            except ValueError:
                return None
        return None


class TriageResult(BaseModel):
    category: IncidentCategory
    affected_services: list[str] = []
    affected_pods: list[str] = []
    upstream_degraded: bool = False
    downstream_degraded: bool = False
    anomaly_started_at: Optional[datetime] = None
    urgency: Urgency
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[str] = []
