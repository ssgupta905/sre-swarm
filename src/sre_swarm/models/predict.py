from typing import Optional

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from .incident import Urgency


class PredictResult(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    will_self_heal: bool
    self_heal_reason: Optional[str] = None
    cascade_risk: Urgency
    mttr_without_min: int = Field(
        validation_alias=AliasChoices("mttr_without_min", "mttr_without_intervention")
    )
    risk_level: Urgency
