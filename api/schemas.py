"""Human-review decision request."""
from pydantic import BaseModel, Field
class ReviewDecision(BaseModel):
    decision_id: str = Field(min_length=1,max_length=160)
    decision: str
    reviewer: str = Field(default="operator",min_length=1,max_length=100)
    note: str | None = Field(default=None,max_length=2000)
