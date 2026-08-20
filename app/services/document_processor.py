from dataclasses import dataclass
from decimal import Decimal
from typing import List, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, defer

from app.db.models import Document, Template, TemplateExample
from app.schemas.documents import DocumentStatus
from app.schemas.extraction import InvoiceExtraction
from app.schemas.validation_rules import TemplateValidationRules
from app.services.extractor import (
    combine_invoices,
    extract_invoice_from_text,
    get_template_example,
    read_pdf_pages,
    split_invoice_segments,
)
from app.services.fingerprint import (
    extract_vendor_key,
    generate_signature,
    mask_invoice_text,
)
from app.services.template_matcher import (
    AUTO_APPROVE_THRESHOLD,
    TemplateMatch,
    find_matching_template,
)
from app.services.validation import (
    MATH_TOLERANCE,
    ValidationResult,
    derive_validation_rules,
    load_template_validation_rules,
    validate_invoice_math,
)


class DocumentNotFoundError(Exception):
    """Raised when a document id does not exist."""


class InvalidDocumentStateError(Exception):
    """Raised when a document is not in the expected workflow state."""


class DuplicateTemplateNameError(Exception):
    """Raised when approving would create a duplicate template name."""


def list_pending_documents(db: Session) -> list[Document]:
    stmt = (
        select(Document)
        .options(defer(Document.pdf_data))
        .where(Document.status == DocumentStatus.PENDING)
        .order_by(Document.created_at.desc())
    )
    return list(db.execute(stmt).scalars().all())


def get_document_pdf(db: Session, document_id: UUID) -> tuple[Document, bytes]:
    document = db.get(Document, document_id)
    if document is None:
        raise DocumentNotFoundError(f"Document {document_id} not found")
    if not document.has_pdf or document.pdf_data is None:
        raise DocumentNotFoundError(f"No PDF stored for document {document_id}")
    return document, document.pdf_data


@dataclass
class _SegmentResult:
    """Per-invoice extraction, fingerprint, and template match within one PDF."""

    signature: str
    vendor_key: Optional[str]
    match: Optional[TemplateMatch]
    extraction: InvoiceExtraction


def _process_segments(db: Session, segments: List[str]) -> List[_SegmentResult]:
    """Fingerprint, match, and extract each invoice segment independently."""
    results: List[_SegmentResult] = []
    for segment in segments:
        signature = generate_signature(segment)
        vendor_key = extract_vendor_key(segment)
        match = find_matching_template(db, signature, vendor_key=vendor_key)
        template_example = (
            get_template_example(db, match.template.id) if match is not None else None
        )
        extraction = extract_invoice_from_text(
            segment,
            template_example=template_example,
        )
        results.append(
            _SegmentResult(
                signature=signature,
                vendor_key=vendor_key,
                match=match,
                extraction=extraction,
            )
        )
    return results


def _resolve_validation_rules(
    segments: List[_SegmentResult],
    extraction: InvoiceExtraction,
) -> TemplateValidationRules:
    """Use a single matched template's formula; otherwise derive from the extraction.

    With multiple invoices bundled in one PDF, per-template formulas cannot be merged
    unambiguously, so the rules are derived from the combined extraction instead.
    """
    if len(segments) == 1 and segments[0].match is not None:
        return load_template_validation_rules(
            segments[0].match.template.validation_rules
        )
    return derive_validation_rules(extraction)


def _primary_match(segments: List[_SegmentResult]) -> Optional[TemplateMatch]:
    """The match surfaced to the API: the sole match, else the first matched segment."""
    if len(segments) == 1:
        return segments[0].match
    return next((seg.match for seg in segments if seg.match is not None), None)


def _should_auto_approve(
    segments: List[_SegmentResult],
    validation: ValidationResult,
) -> bool:
    """Auto-approve only when every invoice segment confidently matched a template."""
    if not validation.is_valid or not segments:
        return False
    return all(
        seg.match is not None
        and seg.match.similarity_score >= AUTO_APPROVE_THRESHOLD
        for seg in segments
    )


def process_upload(
    db: Session,
    pdf_bytes: bytes,
    filename: str,
    claimed_amount: Decimal,
) -> tuple[Document, ValidationResult, Optional[TemplateMatch], TemplateValidationRules, bool]:
    pages = read_pdf_pages(pdf_bytes)
    raw_text = "\n".join(page.strip() for page in pages if page.strip())
    masked_text = mask_invoice_text(raw_text)

    # A single PDF can bundle multiple invoices (e.g. a restaurant tax invoice plus a
    # separate platform-fee invoice). Fingerprint, match, and extract each invoice
    # independently, then combine so the document reconciles as a whole.
    segments = split_invoice_segments(pages)
    segment_results = _process_segments(db, segments)
    extraction = combine_invoices([seg.extraction for seg in segment_results])

    validation_rules = _resolve_validation_rules(segment_results, extraction)
    # Each bundled invoice rounds its own totals to 2 decimals, so summing them can
    # drift by up to one rounding unit per invoice; widen tolerance accordingly.
    tolerance = MATH_TOLERANCE * max(1, len(segment_results))
    validation = validate_invoice_math(
        extraction,
        validation_rules,
        claimed_amount=claimed_amount,
        tolerance=tolerance,
    )

    template_match = _primary_match(segment_results)
    auto_approved = _should_auto_approve(segment_results, validation)
    invoice_segments = [
        {
            "fingerprint": seg.signature,
            "vendor_key": seg.vendor_key,
            "matched_template_id": (
                str(seg.match.template.id) if seg.match is not None else None
            ),
            "similarity_score": (
                seg.match.similarity_score if seg.match is not None else None
            ),
        }
        for seg in segment_results
    ]

    document = Document(
        filename=filename,
        fingerprint=segment_results[0].signature if segment_results else "",
        vendor_key=segment_results[0].vendor_key if segment_results else None,
        invoice_segments=invoice_segments,
        raw_text=raw_text,
        masked_text=masked_text,
        extracted_fields=extraction.model_dump(mode="json"),
        claimed_amount=claimed_amount,
        calculated_total=validation.calculated_total,
        validation_rules=validation_rules.model_dump(mode="json"),
        pdf_data=pdf_bytes,
        has_pdf=True,
        status=DocumentStatus.PROCESSED if auto_approved else DocumentStatus.PENDING,
        matched_template_id=(
            template_match.template.id if template_match is not None else None
        ),
        error_message=None if validation.is_valid else "; ".join(validation.errors),
        auto_approved=auto_approved,
    )
    db.add(document)
    db.commit()
    db.refresh(document)
    return document, validation, template_match, validation_rules, document.auto_approved


def approve_document(
    db: Session,
    document_id: UUID,
    template_name: str,
    approved_fields: InvoiceExtraction,
    validation_rules: Optional[TemplateValidationRules] = None,
    description: Optional[str] = None,
) -> tuple[Document, Template, TemplateExample, TemplateValidationRules]:
    document = db.get(Document, document_id)
    if document is None:
        raise DocumentNotFoundError(f"Document {document_id} not found")
    if document.status != DocumentStatus.PENDING:
        raise InvalidDocumentStateError(
            f"Document {document_id} is not pending review"
        )

    resolved_rules = (
        validation_rules
        or load_template_validation_rules(document.validation_rules)
        or derive_validation_rules(approved_fields)
    )
    # Match the rounding slack used at upload time: one unit per bundled invoice.
    segment_count = len(document.invoice_segments or []) or 1
    tolerance = MATH_TOLERANCE * segment_count
    validation = validate_invoice_math(
        approved_fields,
        resolved_rules,
        claimed_amount=document.claimed_amount,
        tolerance=tolerance,
    )
    if not validation.is_valid:
        raise InvalidDocumentStateError("; ".join(validation.errors))

    template = Template(
        name=template_name,
        vendor_fingerprint=document.fingerprint,
        vendor_key=document.vendor_key,
        description=description,
        field_schema=approved_fields.model_dump(mode="json"),
        validation_rules=resolved_rules.model_dump(mode="json"),
    )
    db.add(template)

    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise DuplicateTemplateNameError(
            f"Template name '{template_name}' already exists"
        ) from exc

    template_example = TemplateExample(
        template_id=template.id,
        source_filename=document.filename,
        raw_text=document.raw_text,
        masked_text=document.masked_text or generate_signature(document.raw_text or ""),
        expected_fields=approved_fields.model_dump(mode="json"),
    )
    db.add(template_example)

    document.status = DocumentStatus.PROCESSED
    document.matched_template_id = template.id
    document.extracted_fields = approved_fields.model_dump(mode="json")
    document.calculated_total = validation.calculated_total
    document.validation_rules = resolved_rules.model_dump(mode="json")
    document.error_message = None

    db.commit()
    db.refresh(document)
    db.refresh(template)
    db.refresh(template_example)
    return document, template, template_example, resolved_rules
