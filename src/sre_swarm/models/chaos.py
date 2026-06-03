from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ReplayValidation(str, Enum):
    CONFIRMED = "confirmed"
    PARTIAL = "partial"
    REJECTED = "rejected"


class ReplayResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    scenario_applied: str = ""
    sandbox_namespace: str = "sre-sandbox"
    duration_s: int = 0
    validation: ReplayValidation
    similarity_score: float = Field(ge=0.0, le=1.0, default=0.0)
    sandbox_metrics: dict = Field(default_factory=dict)
    original_metrics: dict = Field(default_factory=dict)

    @field_validator("validation", mode="before")
    @classmethod
    def _lower(cls, v):
        if isinstance(v, str):
            return v.lower()
        return v
