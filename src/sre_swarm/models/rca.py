from pydantic import BaseModel, Field


class Hypothesis(BaseModel):
    cause: str
    evidence: list[str]
    confidence: float = Field(ge=0.0, le=1.0)


class RCAResult(BaseModel):
    hypotheses: list[Hypothesis]
    top_cause: str
    timeline: list[dict]
    affected_pods: list[str]
    log_patterns: list[str]
    resource_issues: bool
