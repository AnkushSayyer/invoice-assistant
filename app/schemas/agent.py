import os
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Dict, List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

TargetField = Literal["subtotal", "tax", "fees", "tip"]
LearnScope = Literal["vendor", "category", "global"]

from app.schemas.documents import DocumentResponse
from app.schemas.extraction import InvoiceExtraction
from app.schemas.validation_rules import TemplateValidationRules


class AgentDecision(str, Enum):
    """Terminal outcome the agent commits to for a document."""

    APPROVE = "approve"
    REJECT = "reject"
    ESCALATE = "escalate"
    # The agent has a specific, answerable question; it parks the document and
    # asks a human rather than dumping the whole doc into a manual queue.
    CLARIFY = "clarify"


class AgentStep(BaseModel):
    """One tool invocation in the agent's reasoning trace (for the audit log)."""

    model_config = ConfigDict(extra="forbid")

    tool: str
    summary: str
    ok: bool = True
    data: Dict[str, Any] = Field(default_factory=dict)


class ExpensePolicy(BaseModel):
    """Business rules the agent enforces before auto-approving a claim."""

    model_config = ConfigDict(extra="forbid")

    max_amount: Optional[Decimal] = Field(
        default=None, description="Claims above this are rejected outright"
    )
    auto_approve_limit: Optional[Decimal] = Field(
        default=None,
        description="Valid claims above this are escalated to a human, not auto-approved",
    )
    allowed_categories: Optional[List[str]] = Field(
        default=None, description="If set, category must be one of these"
    )
    max_tip_ratio: Optional[Decimal] = Field(
        default=None, description="Ceiling on tip / subtotal (e.g. 0.30)"
    )
    require_math_reconciliation: bool = Field(
        default=True,
        description="When true, a claim whose math cannot be reconciled is never approved",
    )

    @classmethod
    def from_env(cls) -> "ExpensePolicy":
        """Load policy limits from environment variables (all optional)."""

        def _dec(name: str) -> Optional[Decimal]:
            raw = os.getenv(name, "").strip()
            if not raw:
                return None
            try:
                return Decimal(raw)
            except (InvalidOperation, ValueError):
                return None

        categories_raw = os.getenv("EXPENSE_ALLOWED_CATEGORIES", "").strip()
        allowed = (
            [c.strip() for c in categories_raw.split(",") if c.strip()]
            if categories_raw
            else None
        )
        return cls(
            max_amount=_dec("EXPENSE_MAX_AMOUNT"),
            auto_approve_limit=_dec("EXPENSE_AUTO_APPROVE_LIMIT"),
            allowed_categories=allowed,
            max_tip_ratio=_dec("EXPENSE_MAX_TIP_RATIO"),
            require_math_reconciliation=os.getenv(
                "EXPENSE_REQUIRE_RECONCILIATION", "true"
            ).strip().lower()
            not in ("0", "false", "no"),
        )


class AgentResult(BaseModel):
    """The agent's decision plus the full reasoning trace for a document."""

    model_config = ConfigDict(extra="forbid")

    decision: AgentDecision
    confidence: float
    reasons: List[str] = Field(default_factory=list)
    remediations: List[str] = Field(default_factory=list)
    steps: List[AgentStep] = Field(default_factory=list)
    extraction: InvoiceExtraction
    validation_rules: TemplateValidationRules
    calculated_total: Decimal
    claimed_amount: Decimal
    approved_amount: Optional[Decimal] = None
    vendor_key: Optional[str] = None
    duplicate_of: Optional[str] = None
    source: str = "llm"
    document_id: Optional[str] = None
    # When the decision is CLARIFY, the targeted questions awaiting a human answer.
    clarifications: List["ClarificationResponse"] = Field(default_factory=list)


from app.schemas.clarification import ClarificationResponse  # noqa: E402

AgentResult.model_rebuild()


class AgentRunSummary(BaseModel):
    """A stored agent decision, for the audit-trail endpoint."""

    model_config = ConfigDict(from_attributes=True)

    id: Any
    document_id: Optional[Any] = None
    filename: Optional[str] = None
    decision: str
    confidence: float
    source: Optional[str] = None
    vendor_key: Optional[str] = None
    duplicate_of: Optional[Any] = None
    reasons: List[Any] = Field(default_factory=list)
    remediations: List[Any] = Field(default_factory=list)
    steps: List[Any] = Field(default_factory=list)


class AgentResolveRequest(BaseModel):
    """A human reviewer's resolution of an escalated or rejected agent decision."""

    model_config = ConfigDict(extra="forbid")

    document_id: UUID
    decision: Literal["approve", "reject"]
    approved_fields: Optional[InvoiceExtraction] = Field(
        default=None,
        description=(
            "Corrected invoice fields. Required to approve; the reviewer's edits "
            "replace the agent's extraction."
        ),
    )
    validation_rules: Optional[TemplateValidationRules] = Field(
        default=None,
        description="Override the total formula; defaults to the stored rules.",
    )
    approved_amount: Optional[Decimal] = Field(
        default=None,
        ge=0,
        description=(
            "Amount to reimburse, if it differs from the claimed amount (e.g. a "
            "policy cap or manual recalculation). When set it is authoritative, so "
            "approval proceeds even if claimed != calculated; the discrepancy is "
            "recorded. Defaults to the claimed amount."
        ),
    )
    category: Optional[str] = None
    note: Optional[str] = Field(
        default=None, description="Reviewer note recorded in the audit trail"
    )
    force: bool = Field(
        default=False,
        description=(
            "Approve even if the corrected fields still do not reconcile "
            "(the unresolved mismatch is recorded in the audit trail)."
        ),
    )
    learn_vendor: bool = Field(
        default=True,
        description="Persist the reviewer's rules to vendor memory on approval",
    )
    learn_scope: LearnScope = Field(
        default="vendor",
        description="How widely the learned rule applies (vendor / category / global).",
    )
    learn_scope_key: Optional[str] = Field(
        default=None,
        description=(
            "Explicit scope key. Defaults to the document's vendor_key for vendor "
            "scope, the category for category scope, and '*' for global scope."
        ),
    )
    capture_anchor: Optional[str] = Field(
        default=None,
        description=(
            "Line label whose amount should be captured into capture_target_field "
            "for future invoices (deterministic 'add this charge' rule)."
        ),
    )
    capture_target_field: Optional[TargetField] = Field(
        default=None,
        description="Component the captured charge is added to (with capture_anchor).",
    )
    directive: Optional[str] = Field(
        default=None,
        description=(
            "Free-text lesson persisted as an extraction hint for this scope. Steers "
            "the LLM extraction path on future invoices only; not deterministically "
            "validated. Distinct from `note`, which is audit-only."
        ),
    )

    @model_validator(mode="after")
    def _capture_both_or_neither(self) -> "AgentResolveRequest":
        if bool(self.capture_anchor) != bool(self.capture_target_field):
            raise ValueError(
                "capture_anchor and capture_target_field must be provided together"
            )
        return self


class ResolvePreviewRequest(BaseModel):
    """A read-only dry-run: test edited fields + a candidate rule against the
    current document without persisting anything."""

    model_config = ConfigDict(extra="forbid")

    document_id: UUID
    approved_fields: Optional[InvoiceExtraction] = Field(
        default=None,
        description="Edited fields to test; defaults to the stored extraction.",
    )
    validation_rules: Optional[TemplateValidationRules] = Field(
        default=None,
        description="Candidate total formula; defaults to the stored rules.",
    )
    capture_anchor: Optional[str] = Field(
        default=None,
        description="Candidate capture-rule line label to apply before validating.",
    )
    capture_target_field: Optional[TargetField] = Field(
        default=None,
        description="Component the captured charge is added to (with capture_anchor).",
    )

    @model_validator(mode="after")
    def _capture_both_or_neither(self) -> "ResolvePreviewRequest":
        if bool(self.capture_anchor) != bool(self.capture_target_field):
            raise ValueError(
                "capture_anchor and capture_target_field must be provided together"
            )
        return self


class ResolvePreviewResponse(BaseModel):
    """The outcome of a dry-run: whether the edits reconcile, and the recalculated
    total, so the reviewer can tune a rule before committing."""

    model_config = ConfigDict(extra="forbid")

    is_valid: bool
    calculated_total: Decimal
    errors: List[str] = Field(default_factory=list)
    recovered_fields: Optional[InvoiceExtraction] = Field(
        default=None,
        description="Fields after capture-rule recovery, when a capture rule applied.",
    )
    capture_previewable: bool = Field(
        default=True,
        description="False when a capture rule was requested but no raw_text exists.",
    )


class AgentQueueItem(BaseModel):
    """One document awaiting human resolution, with its latest agent decision."""

    model_config = ConfigDict(extra="forbid")

    document: DocumentResponse
    latest_run: Optional[AgentRunSummary] = None


class AgentTrainingExample(BaseModel):
    """A human-verified (document, labelled-fields) pair for uptraining a parser.

    These accumulate every time a reviewer approves a correction. Export them to
    Document AI Workbench (or any labelling tool) to uptrain the Invoice Parser so
    accuracy on your own layouts improves over time.
    """

    model_config = ConfigDict(extra="forbid")

    document_id: str
    filename: Optional[str] = None
    vendor_key: Optional[str] = None
    fields: Dict[str, Any] = Field(default_factory=dict)
