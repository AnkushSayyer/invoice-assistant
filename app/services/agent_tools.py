"""Tools the autonomous invoice agent calls.

Each tool is a small, independently testable function. The agent
(``app/services/agent.py``) orchestrates them into a perceive -> reconcile ->
remediate -> decide -> act loop. Nothing here removes or changes the existing
upload/template pipeline; these are additive capabilities.
"""

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from itertools import product
from types import SimpleNamespace
from typing import Any, List, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Document, VendorPolicy, VendorRule
from app.schemas.documents import DocumentStatus
from app.schemas.extraction import InvoiceExtraction
from app.schemas.agent import ExpensePolicy
from app.schemas.validation_rules import TemplateValidationRules
from app.services.document_ai import (
    DocumentAIError,
    DocumentAINotConfigured,
    extract_with_document_ai,
    is_document_ai_configured,
)
from app.services.extractor import (
    PDFReadError,
    combine_invoices,
    extract_invoice_from_text,
    read_pdf_pages,
    split_invoice_page_groups,
    split_invoice_segments,
    subset_pdf_pages,
)
from app.services.fingerprint import extract_vendor_key
from app.services.validation import (
    ValidationResult,
    compute_calculated_total,
    validate_invoice_math,
)

# Below this perception confidence the agent will not auto-approve; it escalates
# and asks for a clearer document instead of guessing.
LOW_CONFIDENCE_THRESHOLD = 0.6
# Baseline confidence for the LLM path, which yields no per-field probabilities.
LLM_BASE_CONFIDENCE = 0.75


@dataclass
class Perception:
    """Result of the perception tool: what the agent believes the invoice says."""

    extraction: InvoiceExtraction
    confidence: float
    source: str  # "document_ai" | "llm"
    segments: int
    vendor_key: Optional[str]
    raw_text: str = ""


@dataclass
class Remediation:
    """A rule set the agent found that makes an otherwise-failing claim reconcile."""

    rules: TemplateValidationRules
    result: ValidationResult
    explanation: str


@dataclass
class DuplicateHit:
    document_id: UUID
    invoice_number: str


@dataclass
class ChargeRecovery:
    """A deterministically recovered missing charge that makes the claim reconcile."""

    extraction: InvoiceExtraction
    result: ValidationResult
    explanation: str


# Money on an invoice line, e.g. "₹1,234.50" or "40.00".
_MONEY_RE = re.compile(r"(?:[$€£¥₹]\s*)?(\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)")


# --------------------------------------------------------------------------- #
# Tool 1: perceive
# --------------------------------------------------------------------------- #
def perceive(
    db: Session,
    pdf_bytes: bytes,
    *,
    knowledge: Optional[Any] = None,
) -> Perception:
    """Extract invoice fields, preferring Document AI, falling back to the LLM.

    Document AI (option 2) OCRs scans and returns per-field confidence. When it is
    not configured or fails, the existing PyMuPDF + LLM multi-segment pipeline is
    used unchanged.

    ``knowledge`` (a :class:`app.services.knowledge.VendorKnowledge`) injects
    learned per-vendor directives and worked few-shot examples into the LLM path so
    a previously-clarified layout is extracted correctly on its own next time.
    """
    if is_document_ai_configured():
        try:
            return _perceive_with_document_ai(pdf_bytes)
        except (DocumentAINotConfigured, DocumentAIError):
            pass  # Fall back to the existing pipeline.

    return _perceive_with_llm(db, pdf_bytes, knowledge=knowledge)


def _safe_page_groups(pdf_bytes: bytes) -> List[List[int]]:
    """Best-effort per-invoice page grouping; empty when the PDF can't be read."""
    try:
        pages = read_pdf_pages(pdf_bytes)
    except PDFReadError:
        return []
    return split_invoice_page_groups(pages)


def _perceive_with_document_ai(pdf_bytes: bytes) -> Perception:
    """Perceive via Document AI, splitting bundled invoices into clean sub-PDFs.

    A single PDF that bundles several invoices (e.g. a restaurant tax invoice plus a
    separate platform-fee invoice) is mis-parsed as one jumbled document, tanking
    confidence and breaking the math. We detect the invoice boundaries and send each
    invoice to Document AI on its own, then sum the results — mirroring the LLM path
    — so each segment parses cleanly and the combined math reconciles.
    """
    groups = _safe_page_groups(pdf_bytes)
    if len(groups) > 1:
        extractions: List[InvoiceExtraction] = []
        overalls: List[float] = []
        vendor_tax_id: Optional[str] = None
        for group in groups:
            sub_pdf = subset_pdf_pages(pdf_bytes, group)
            extraction, _confidences, overall, tax_id = extract_with_document_ai(sub_pdf)
            extractions.append(extraction)
            overalls.append(overall)
            vendor_tax_id = vendor_tax_id or tax_id
        combined = combine_invoices(extractions)
        vendor_key = _vendor_key_from_docai(combined, vendor_tax_id)
        return Perception(
            extraction=combined,
            # The weakest segment gates the whole bundle.
            confidence=min(overalls) if overalls else 0.0,
            source="document_ai",
            segments=len(groups),
            vendor_key=vendor_key,
        )

    extraction, _confidences, overall, vendor_tax_id = extract_with_document_ai(pdf_bytes)
    vendor_key = _vendor_key_from_docai(extraction, vendor_tax_id)
    return Perception(
        extraction=extraction,
        confidence=overall,
        source="document_ai",
        segments=1,
        vendor_key=vendor_key,
    )


def _perceive_with_llm(
    db: Session,
    pdf_bytes: bytes,
    *,
    knowledge: Optional[Any] = None,
) -> Perception:
    """Perceive via the PyMuPDF + LLM pipeline, applying any learned few-shot."""
    pages = read_pdf_pages(pdf_bytes)
    raw_text = "\n".join(page.strip() for page in pages if page.strip())
    vendor_key = extract_vendor_key(raw_text)

    # Injected learned knowledge (directives + many few-shots) takes priority. When
    # absent, fall back to the legacy single-example vendor-policy few-shot so the
    # original behaviour is preserved.
    directives: Optional[List[str]] = None
    few_shots: Optional[List[Any]] = None
    few_shot = None
    if knowledge is not None and not getattr(knowledge, "is_empty", True):
        directives = list(getattr(knowledge, "directives", []) or [])
        few_shots = list(getattr(knowledge, "few_shots", []) or [])
    else:
        # A human-corrected example for this vendor (learned on resolve) steers the
        # LLM so previously-escalated layouts extract correctly without intervention.
        few_shot = _vendor_few_shot(db, vendor_key)

    segments = split_invoice_segments(pages)
    extractions = [
        extract_invoice_from_text(
            segment,
            template_example=few_shot,
            directives=directives,
            few_shots=few_shots,
        )
        for segment in segments
    ]
    extraction = combine_invoices(extractions)
    return Perception(
        extraction=extraction,
        confidence=LLM_BASE_CONFIDENCE,
        source="llm",
        segments=len(segments),
        vendor_key=vendor_key,
        raw_text=raw_text,
    )


def _vendor_few_shot(db: Session, vendor_key: Optional[str]) -> Optional[Any]:
    """Build a few-shot example from a vendor's learned correction, if any.

    Returns a duck-typed object exposing ``raw_text``/``masked_text``/
    ``expected_fields`` (the shape ``extractor._build_messages`` consumes) so the
    LLM sees a worked example for this vendor before extracting the new invoice.
    """
    policy = lookup_vendor_policy(db, vendor_key)
    if policy is None or not policy.example_text or not policy.example_fields:
        return None
    return SimpleNamespace(
        raw_text=policy.example_text,
        masked_text=policy.example_text,
        expected_fields=policy.example_fields,
    )


def _vendor_key_from_docai(
    extraction: InvoiceExtraction, vendor_tax_id: Optional[str]
) -> Optional[str]:
    if vendor_tax_id:
        # Reuse the same anchoring convention as the fingerprint path.
        anchored = extract_vendor_key(vendor_tax_id)
        if anchored:
            return anchored
        return f"taxid:{vendor_tax_id.strip()}"
    return extract_vendor_key(extraction.vendor or "")


# --------------------------------------------------------------------------- #
# Tool 2: reconcile math
# --------------------------------------------------------------------------- #
def reconcile_math(
    extraction: InvoiceExtraction,
    rules: TemplateValidationRules,
    claimed_amount: Decimal,
    tolerance: Decimal,
) -> ValidationResult:
    """Deterministically verify internal math + claimed == total (the guardrail)."""
    return validate_invoice_math(
        extraction,
        rules,
        claimed_amount=claimed_amount,
        tolerance=tolerance,
    )


# --------------------------------------------------------------------------- #
# Tool 3: remediate
# --------------------------------------------------------------------------- #
def _candidate_rule_sets(
    extraction: InvoiceExtraction,
) -> List[TemplateValidationRules]:
    """Plausible interpretations to try when the default formula fails to reconcile."""
    component_sets = [
        ["subtotal", "tax", "fees", "tip"],
        ["subtotal", "tax", "fees"],
        ["subtotal", "tax", "tip"],
        ["subtotal", "tip"],
        ["subtotal", "tax"],
        ["subtotal"],
    ]
    candidates: List[TemplateValidationRules] = []
    for components, subtract_discounts, includes_tax in product(
        component_sets, (True, False), (False, True)
    ):
        candidates.append(
            TemplateValidationRules(
                validate_line_items=False,
                total_components=components,  # type: ignore[arg-type]
                subtract_discounts=subtract_discounts,
                line_amount_includes_tax=includes_tax,
            )
        )
    return candidates


def remediate(
    extraction: InvoiceExtraction,
    claimed_amount: Decimal,
    tolerance: Decimal,
) -> Optional[Remediation]:
    """Search alternative total formulas for one that reconciles the claim.

    This is the agent's self-correction step: rather than rejecting a claim whose
    default formula does not add up (e.g. tax-inclusive line amounts, a missing fee
    component), it reasons over plausible interpretations and adopts the first that
    makes both the internal total and the claimed amount reconcile.
    """
    for rules in _candidate_rule_sets(extraction):
        result = validate_invoice_math(
            extraction,
            rules,
            claimed_amount=claimed_amount,
            tolerance=tolerance,
        )
        if result.is_valid:
            formula = " + ".join(rules.total_components)
            if rules.subtract_discounts:
                formula += " - discounts"
            explanation = f"reconciled using total = {formula}"
            if rules.line_amount_includes_tax:
                explanation += " (line amounts treated as tax-inclusive)"
            return Remediation(rules=rules, result=result, explanation=explanation)
    return None


# --------------------------------------------------------------------------- #
# Tool 4: vendor memory (lookup + learn)
# --------------------------------------------------------------------------- #
def lookup_vendor_policy(
    db: Session, vendor_key: Optional[str]
) -> Optional[VendorPolicy]:
    """Return remembered rules for a vendor keyed on its stable identifier."""
    if not vendor_key:
        return None
    stmt = select(VendorPolicy).where(VendorPolicy.vendor_key == vendor_key)
    return db.execute(stmt).scalar_one_or_none()


def learn_vendor_policy(
    db: Session,
    vendor_key: Optional[str],
    rules: TemplateValidationRules,
    *,
    display_name: Optional[str] = None,
    tolerance: Optional[Decimal] = None,
    category: Optional[str] = None,
    example_text: Optional[str] = None,
    example_fields: Optional[dict] = None,
) -> Optional[VendorPolicy]:
    """Persist/refresh what the agent learned so future invoices auto-apply it.

    When ``example_text``/``example_fields`` are supplied (typically from a human
    correction) they are stored as a few-shot example the LLM path reuses for this
    vendor, so a layout that once needed a human is extracted correctly next time.
    """
    if not vendor_key:
        return None
    policy = lookup_vendor_policy(db, vendor_key)
    if policy is None:
        policy = VendorPolicy(
            vendor_key=vendor_key,
            display_name=display_name,
            validation_rules=rules.model_dump(mode="json"),
            tolerance=tolerance,
            category=category,
            times_seen=1,
            example_text=example_text,
            example_fields=example_fields,
        )
        db.add(policy)
    else:
        policy.validation_rules = rules.model_dump(mode="json")
        policy.times_seen = (policy.times_seen or 0) + 1
        if display_name:
            policy.display_name = display_name
        if tolerance is not None:
            policy.tolerance = tolerance
        if category:
            policy.category = category
        if example_text is not None:
            policy.example_text = example_text
        if example_fields is not None:
            policy.example_fields = example_fields
    return policy


# --------------------------------------------------------------------------- #
# Tool 5: duplicate / fraud guard
# --------------------------------------------------------------------------- #
def check_duplicate(
    db: Session,
    extraction: InvoiceExtraction,
    *,
    exclude_document_id: Optional[UUID] = None,
) -> Optional[DuplicateHit]:
    """Flag a previously processed document with the same invoice number.

    Double-submission is the most common expense fraud/error; anchoring on the
    invoice number (plus vendor when available) catches it cheaply.
    """
    invoice_number = (extraction.invoice_number or "").strip()
    if not invoice_number or invoice_number.upper() == "UNKNOWN":
        return None
    stmt = (
        select(Document)
        .where(Document.status == DocumentStatus.PROCESSED)
        .where(
            Document.extracted_fields["invoice_number"].astext == invoice_number
        )
    )
    if exclude_document_id is not None:
        stmt = stmt.where(Document.id != exclude_document_id)
    match = db.execute(stmt.limit(1)).scalar_one_or_none()
    if match is None:
        return None
    return DuplicateHit(document_id=match.id, invoice_number=invoice_number)


# --------------------------------------------------------------------------- #
# Tool 6: expense-policy check
# --------------------------------------------------------------------------- #
def check_expense_policy(
    extraction: InvoiceExtraction,
    claimed_amount: Decimal,
    policy: ExpensePolicy,
    *,
    category: Optional[str] = None,
) -> List[str]:
    """Return a list of policy violations (empty means the claim is within policy)."""
    violations: List[str] = []
    if policy.max_amount is not None and claimed_amount > policy.max_amount:
        violations.append(
            f"claimed amount {claimed_amount} exceeds maximum allowed "
            f"{policy.max_amount}"
        )
    if (
        policy.allowed_categories is not None
        and category is not None
        and category not in policy.allowed_categories
    ):
        violations.append(
            f"category '{category}' is not in allowed categories "
            f"{policy.allowed_categories}"
        )
    if (
        policy.max_tip_ratio is not None
        and extraction.subtotal > 0
        and (extraction.tip / extraction.subtotal) > policy.max_tip_ratio
    ):
        violations.append(
            f"tip ratio {(extraction.tip / extraction.subtotal):.2f} exceeds "
            f"maximum {policy.max_tip_ratio}"
        )
    return violations


def needs_escalation_over_limit(
    claimed_amount: Decimal, policy: ExpensePolicy
) -> bool:
    """A valid-but-large claim should go to a human even when everything checks out."""
    return (
        policy.auto_approve_limit is not None
        and claimed_amount > policy.auto_approve_limit
    )


# --------------------------------------------------------------------------- #
# Tool 7: deterministic missing-charge recovery (Tier A of the learning guarantee)
# --------------------------------------------------------------------------- #
def unaccounted_gap(
    extraction: InvoiceExtraction,
    rules: TemplateValidationRules,
) -> Decimal:
    """How much the stated total exceeds the sum of the extracted components.

    A positive gap means a charge is present on the invoice total but was not
    captured into any component (the classic "missed fee/surcharge" case).
    """
    return extraction.total - compute_calculated_total(extraction, rules)


def _amount_after_anchor(line: str, anchor: str) -> Optional[Decimal]:
    """Find the money value on ``line`` at/after ``anchor`` (rightmost wins)."""
    idx = line.lower().find(anchor.lower())
    if idx < 0:
        return None
    tail = line[idx + len(anchor):]
    matches = _MONEY_RE.findall(tail)
    if not matches:
        # Some layouts put the amount before the label; scan the whole line.
        matches = _MONEY_RE.findall(line)
    if not matches:
        return None
    try:
        return Decimal(matches[-1].replace(",", ""))
    except InvalidOperation:
        return None


def find_anchored_amount(raw_text: str, anchor: str) -> Optional[Decimal]:
    """Return the amount on the first line containing ``anchor``, if any."""
    if not raw_text or not anchor:
        return None
    for line in raw_text.splitlines():
        if anchor.lower() in line.lower():
            amount = _amount_after_anchor(line, anchor)
            if amount is not None:
                return amount
    return None


def recover_missing_charge(
    extraction: InvoiceExtraction,
    rules: TemplateValidationRules,
    raw_text: str,
    claimed_amount: Decimal,
    tolerance: Decimal,
    capture_rules: List["VendorRule"],
) -> Optional[ChargeRecovery]:
    """Deterministically recover a known missing charge using learned capture rules.

    For each learned ``capture_anchor`` -> ``target_field`` rule, locate the anchored
    line in the raw invoice text, add its amount to the target field, and re-check the
    math. This is the strong (code-enforced) tier of the "won't miss it again"
    guarantee: no LLM involved, so a once-clarified charge is captured every time.
    """
    if not raw_text or not capture_rules:
        return None

    updated = extraction
    applied: List[str] = []
    for rule in capture_rules:
        anchor = rule.capture_anchor
        field = rule.target_field
        if not anchor or field not in _COMPONENT_FIELDS:
            continue
        amount = find_anchored_amount(raw_text, anchor)
        if amount is None or amount <= 0:
            continue
        current = getattr(updated, field)
        updated = updated.model_copy(update={field: current + amount})
        applied.append(f"'{anchor}' -> {field} += {amount}")

    if not applied:
        return None

    result = validate_invoice_math(
        updated, rules, claimed_amount=claimed_amount, tolerance=tolerance
    )
    return ChargeRecovery(
        extraction=updated,
        result=result,
        explanation="recovered missing charge(s): " + "; ".join(applied),
    )


_COMPONENT_FIELDS = {"subtotal", "tax", "fees", "tip"}
