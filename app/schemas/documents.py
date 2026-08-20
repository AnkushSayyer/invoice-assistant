from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Dict, List, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.extraction import InvoiceExtraction
from app.schemas.validation_rules import TemplateValidationRules

if TYPE_CHECKING:
    from app.db.models import Document


class DocumentStatus:
    PENDING = "pending"
    PROCESSED = "processed"
    FAILED = "failed"
    # The agent asked a targeted clarification and is waiting on a human answer.
    NEEDS_INPUT = "needs_input"


class DocumentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    filename: str
    fingerprint: str
    vendor_key: Optional[str] = None
    invoice_segments: Optional[List[Dict[str, Any]]] = None
    status: str
    extracted_fields: Optional[Dict[str, Any]] = None
    claimed_amount: Optional[Decimal] = None
    calculated_total: Optional[Decimal] = None
    approved_amount: Optional[Decimal] = None
    error_message: Optional[str] = None
    matched_template_id: Optional[UUID] = None
    auto_approved: bool = False
    validation_rules: Optional[TemplateValidationRules] = None
    has_pdf: bool = False
    pdf_url: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    @field_validator("auto_approved", mode="before")
    @classmethod
    def coerce_auto_approved(cls, value: object) -> bool:
        if value is None:
            return False
        return bool(value)


def build_document_response(document: "Document") -> DocumentResponse:
    from app.services.validation import load_template_validation_rules

    validation_rules = None
    if document.validation_rules:
        validation_rules = load_template_validation_rules(document.validation_rules)

    response = DocumentResponse.model_validate(document)
    updates: dict[str, object] = {}
    if document.has_pdf:
        updates["pdf_url"] = f"/documents/{document.id}/pdf"
    if validation_rules is not None:
        updates["validation_rules"] = validation_rules
    if updates:
        return response.model_copy(update=updates)
    return response


class UploadResponse(BaseModel):
    document: DocumentResponse
    math_valid: bool
    validation_errors: List[str] = Field(default_factory=list)
    calculated_total: Decimal
    claimed_amount: Decimal
    matched_template_id: Optional[UUID] = None
    similarity_score: Optional[float] = None
    validation_rules: TemplateValidationRules
    auto_approved: bool = False


class ApproveRequest(BaseModel):
    document_id: UUID
    template_name: str = Field(..., min_length=1, max_length=255)
    approved_fields: InvoiceExtraction
    validation_rules: Optional[TemplateValidationRules] = Field(
        default=None,
        description=(
            "Formula to validate and persist on the template. "
            "When omitted, uses the rules stored at upload time."
        ),
    )
    description: Optional[str] = None


class ApproveResponse(BaseModel):
    document: DocumentResponse
    template_id: UUID
    template_example_id: UUID
    validation_rules: TemplateValidationRules
