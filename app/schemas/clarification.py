"""Pydantic schemas for the ask-and-learn clarification flow and knowledge base.

These back the ``/agent/clarify`` endpoints: the agent emits a typed, answerable
question when it hits an ambiguity, a human answers it (multiple choice + an
optional free-text note), and the answer is generalized into a reusable
:class:`app.db.models.VendorRule`.
"""

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.documents import DocumentResponse


class ClarificationOption(BaseModel):
    """One selectable answer for a clarification question."""

    model_config = ConfigDict(extra="forbid")

    id: str
    label: str


class ClarificationResponse(BaseModel):
    """A stored clarification request, as returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    document_id: UUID
    vendor_key: Optional[str] = None
    ambiguity_type: str
    question: str
    options: List[Dict[str, Any]] = Field(default_factory=list)
    agent_hypothesis: Optional[str] = None
    evidence: Dict[str, Any] = Field(default_factory=dict)
    proposed_scope: str
    proposed_scope_key: str
    status: str
    answer_option_id: Optional[str] = None
    answer_note: Optional[str] = None
    confirmed_scope: Optional[str] = None
    confirmed_scope_key: Optional[str] = None
    resulting_rule_id: Optional[UUID] = None
    round: int = 1
    created_at: Optional[datetime] = None


class ClarificationQueueItem(BaseModel):
    """A document parked for a human answer, with its open clarification(s)."""

    model_config = ConfigDict(extra="forbid")

    document: DocumentResponse
    clarifications: List[ClarificationResponse] = Field(default_factory=list)


class ClarificationAnswer(BaseModel):
    """A human's answer to one clarification question."""

    model_config = ConfigDict(extra="forbid")

    clarification_id: UUID
    answer_option_id: str = Field(
        ..., description="Which of the offered options the reviewer chose"
    )
    answer_note: Optional[str] = Field(
        default=None,
        description=(
            "Optional free text. For a missing-charge answer, naming the exact "
            "line (e.g. 'Airport Surcharge') lets the agent build a deterministic "
            "capture rule so the charge is never missed again."
        ),
    )
    confirmed_scope: Optional[Literal["vendor", "category", "global"]] = Field(
        default=None,
        description="Override the agent's proposed scope for the learned rule.",
    )
    confirmed_scope_key: Optional[str] = Field(
        default=None, description="Scope key when confirming a non-vendor scope."
    )
    learn: bool = Field(
        default=True,
        description="Persist the answer as a reusable rule (turn off for one-offs).",
    )


class VendorRuleResponse(BaseModel):
    """A learned rule, for inspection endpoints."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    scope: str
    scope_key: str
    rule_type: str
    trigger: Optional[str] = None
    directive: str
    payload: Dict[str, Any] = Field(default_factory=dict)
    capture_anchor: Optional[str] = None
    target_field: Optional[str] = None
    source: str
    confidence: float
    active: bool
    times_applied: int
