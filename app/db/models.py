import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base


class Template(Base):
    __tablename__ = "templates"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    vendor_fingerprint: Mapped[str] = mapped_column(String(2048), nullable=False, index=True)
    vendor_key: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True, index=True
    )
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    field_schema: Mapped[Dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    validation_rules: Mapped[Dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    examples: Mapped[List["TemplateExample"]] = relationship(
        back_populates="template", cascade="all, delete-orphan"
    )
    documents: Mapped[List["Document"]] = relationship(back_populates="matched_template")


class TemplateExample(Base):
    __tablename__ = "template_examples"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    template_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("templates.id", ondelete="CASCADE"), nullable=False
    )
    source_filename: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    raw_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    masked_text: Mapped[str] = mapped_column(Text, nullable=False)
    expected_fields: Mapped[Dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    template: Mapped["Template"] = relationship(back_populates="examples")


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(2048), nullable=False, index=True)
    vendor_key: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True, index=True
    )
    invoice_segments: Mapped[Optional[List[Dict[str, Any]]]] = mapped_column(
        JSONB, nullable=True
    )
    raw_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    masked_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    extracted_fields: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    claimed_amount: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(12, 2), nullable=True
    )
    calculated_total: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(12, 2), nullable=True
    )
    # The amount actually approved for reimbursement. Defaults to the claimed
    # amount on a clean approval, but a human reviewer can set it higher or lower
    # (e.g. policy cap, partial approval, manual recalculation).
    approved_amount: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(12, 2), nullable=True
    )
    pdf_data: Mapped[Optional[bytes]] = mapped_column(LargeBinary, nullable=True)
    has_pdf: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="pending")
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    matched_template_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("templates.id", ondelete="SET NULL"), nullable=True
    )
    validation_rules: Mapped[Dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    auto_approved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    matched_template: Mapped[Optional["Template"]] = relationship(back_populates="documents")


class VendorPolicy(Base):
    """Per-vendor memory the agent learns and reuses across invoices.

    Keyed on the stable extracted ``vendor_key`` (GSTIN/PAN/email domain) rather
    than a layout fingerprint, so a vendor's quirks (e.g. tax-inclusive line
    amounts, a wider rounding tolerance) apply regardless of layout changes.
    """

    __tablename__ = "vendor_policies"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    vendor_key: Mapped[str] = mapped_column(
        String(255), unique=True, nullable=False, index=True
    )
    display_name: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    validation_rules: Mapped[Dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    tolerance: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(12, 4), nullable=True
    )
    category: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    times_seen: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # A human-corrected worked example (layout text + approved fields) the LLM path
    # reuses as a few-shot so a once-escalated vendor extracts correctly next time.
    example_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    example_fields: Mapped[Optional[Dict[str, Any]]] = mapped_column(
        JSONB, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class AgentRun(Base):
    """Audit trail for one autonomous agent decision over a document."""

    __tablename__ = "agent_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    document_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    filename: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    decision: Mapped[str] = mapped_column(String(50), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    source: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    vendor_key: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    duplicate_of: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    reasons: Mapped[List[Any]] = mapped_column(JSONB, nullable=False, default=list)
    remediations: Mapped[List[Any]] = mapped_column(JSONB, nullable=False, default=list)
    steps: Mapped[List[Any]] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# --------------------------------------------------------------------------- #
# Learning knowledge base (additive; independent of the legacy fingerprint /
# template pipeline). Identity is anchored on the stable ``vendor_key`` rather
# than a brittle layout fingerprint, so it scales to arbitrary vendors.
# --------------------------------------------------------------------------- #
class RuleScope:
    """How widely a learned rule applies."""

    VENDOR = "vendor"  # scope_key == vendor_key
    CATEGORY = "category"  # scope_key == expense category
    GLOBAL = "global"  # scope_key == "*"

    GLOBAL_KEY = "*"


class RuleType:
    """The kind of lesson a learned rule encodes."""

    VALIDATION = "validation_rule"  # payload is a TemplateValidationRules dump
    FIELD_MAPPING = "field_mapping"  # e.g. "capture 'Airport Surcharge' -> fees"
    POLICY = "policy_directive"  # e.g. "alcohol is not reimbursable"
    HINT = "extraction_hint"  # free-text lesson injected into the prompt


class VendorProfile(Base):
    """Per-vendor identity + rolling stats. Rules/examples live in their own tables
    so learning is additive rather than an overwrite."""

    __tablename__ = "vendor_profiles"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    vendor_key: Mapped[str] = mapped_column(
        String(255), unique=True, nullable=False, index=True
    )
    display_name: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    category: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    times_seen: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    times_approved: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    times_clarified: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class VendorRule(Base):
    """One learned, typed, scoped lesson. Additive: new lessons never overwrite old
    ones, so a scope's knowledge only accumulates.

    ``capture_anchor`` + ``target_field`` enable deterministic recovery of a
    missing charge (Tier A): when set, the reconcile step can search the raw text
    for the anchored line and assign its amount to ``target_field`` without relying
    on the LLM.
    """

    __tablename__ = "vendor_rules"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    scope: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    scope_key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    rule_type: Mapped[str] = mapped_column(String(40), nullable=False)
    trigger: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    directive: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[Dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    capture_anchor: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    target_field: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="human")
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    times_applied: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_from_clarification_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    document_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class VendorFewShot(Base):
    """A worked (invoice text -> corrected fields) example the extractor injects as
    a few-shot for this vendor. Many per vendor; they accumulate."""

    __tablename__ = "vendor_few_shots"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    vendor_key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    source_document_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    example_text: Mapped[str] = mapped_column(Text, nullable=False)
    example_fields: Mapped[Dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ClarificationStatus:
    OPEN = "open"
    ANSWERED = "answered"
    SUPERSEDED = "superseded"


class ClarificationRequest(Base):
    """A targeted, answerable question the agent asks when it hits an ambiguity it
    cannot resolve deterministically. The answer is generalized into a
    :class:`VendorRule`, so the same question is never asked twice."""

    __tablename__ = "clarification_requests"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    vendor_key: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True, index=True
    )
    ambiguity_type: Mapped[str] = mapped_column(String(40), nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    options: Mapped[List[Any]] = mapped_column(JSONB, nullable=False, default=list)
    agent_hypothesis: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    evidence: Mapped[Dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    proposed_scope: Mapped[str] = mapped_column(String(20), nullable=False)
    proposed_scope_key: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=ClarificationStatus.OPEN, index=True
    )
    answer_option_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    answer_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    confirmed_scope: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    confirmed_scope_key: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )
    resulting_rule_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    round: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
