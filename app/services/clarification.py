"""Ask-and-learn: turn an unresolved ambiguity into a targeted, answerable question,
and turn the human's answer into a reusable :class:`~app.db.models.VendorRule`.

The agent asks a question only when the ambiguity is *answerable and learnable*
(e.g. a missing charge, tax-inclusive lines, an unrecorded discount). A pure
claimed-vs-calculated mismatch is a user claim error, not a vendor quirk, so it is
left to the full manual escalation path instead of being asked.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    ClarificationRequest,
    ClarificationStatus,
    RuleScope,
    RuleType,
)
from app.schemas.extraction import InvoiceExtraction
from app.schemas.validation_rules import TemplateValidationRules
from app.services.validation import ValidationResult, compute_calculated_total

# Ambiguity types the agent knows how to ask about.
AMB_MISSING_CHARGE = "missing_charge"
AMB_UNRECORDED_DISCOUNT = "unrecorded_discount"
AMB_LINE_TAX_INCLUSIVE = "line_tax_inclusive"

_MISSING_TARGET_BY_OPTION = {"fee": "fees", "tax": "tax", "tip": "tip"}


@dataclass
class AmbiguitySpec:
    """A resolved, answerable ambiguity ready to be turned into a question."""

    ambiguity_type: str
    question: str
    options: List[Dict[str, str]]
    agent_hypothesis: str
    proposed_scope: str
    proposed_scope_key: str
    evidence: Dict[str, Any] = field(default_factory=dict)


def _proposed_scope(
    vendor_key: Optional[str], category: Optional[str]
) -> tuple[str, str]:
    if vendor_key:
        return RuleScope.VENDOR, vendor_key
    if category:
        return RuleScope.CATEGORY, category
    return RuleScope.GLOBAL, RuleScope.GLOBAL_KEY


def _has(errors: List[str], needle: str) -> bool:
    return any(needle in error for error in errors)


def detect_ambiguity(
    extraction: InvoiceExtraction,
    rules: TemplateValidationRules,
    reconciliation: ValidationResult,
    *,
    vendor_key: Optional[str] = None,
    category: Optional[str] = None,
    tolerance: Decimal = Decimal("0.01"),
) -> Optional[AmbiguitySpec]:
    """Classify a reconciliation failure into an answerable question, or None.

    Returns None for claim-only mismatches (the internal invoice math is fine but
    the claimed amount differs) since that is not a vendor-learnable extraction
    issue and belongs in full manual review.
    """
    if reconciliation.is_valid:
        return None

    errors = reconciliation.errors
    total_mismatch = _has(errors, "template formula")
    line_mismatch = _has(errors, "line items")
    claim_only = bool(errors) and not total_mismatch and not line_mismatch
    if claim_only:
        return None  # user claim error, not a vendor quirk -> escalate, don't ask

    scope, scope_key = _proposed_scope(vendor_key, category)
    calculated_total = compute_calculated_total(extraction, rules)
    gap = extraction.total - calculated_total

    if total_mismatch and gap > tolerance:
        return _missing_charge_spec(
            extraction, rules, gap, calculated_total, scope, scope_key
        )
    if total_mismatch and gap < -tolerance:
        return _unrecorded_discount_spec(
            extraction, rules, gap, calculated_total, scope, scope_key
        )
    if line_mismatch:
        return _line_tax_spec(extraction, rules, scope, scope_key)
    return None


def _base_evidence(
    extraction: InvoiceExtraction,
    rules: TemplateValidationRules,
    calculated_total: Decimal,
    gap: Decimal,
) -> Dict[str, Any]:
    return {
        "gap": str(gap),
        "total": str(extraction.total),
        "calculated_total": str(calculated_total),
        "subtotal": str(extraction.subtotal),
        "tax": str(extraction.tax),
        "fees": str(extraction.fees),
        "tip": str(extraction.tip),
        "rules": rules.model_dump(mode="json"),
    }


def _missing_charge_spec(
    extraction: InvoiceExtraction,
    rules: TemplateValidationRules,
    gap: Decimal,
    calculated_total: Decimal,
    scope: str,
    scope_key: str,
) -> AmbiguitySpec:
    question = (
        f"The stated total ({extraction.total}) is {gap} more than the sum of the "
        f"extracted components ({calculated_total}). What accounts for the extra "
        f"{gap}?"
    )
    options = [
        {"id": "fee", "label": "An unlisted fee / surcharge (add to fees)"},
        {"id": "tax", "label": "A tax I missed (add to tax)"},
        {"id": "tip", "label": "A tip / gratuity (add to tip)"},
        {"id": "other", "label": "Something else / not sure"},
    ]
    return AmbiguitySpec(
        ambiguity_type=AMB_MISSING_CHARGE,
        question=question,
        options=options,
        agent_hypothesis="fee",
        proposed_scope=scope,
        proposed_scope_key=scope_key,
        evidence=_base_evidence(extraction, rules, calculated_total, gap),
    )


def _unrecorded_discount_spec(
    extraction: InvoiceExtraction,
    rules: TemplateValidationRules,
    gap: Decimal,
    calculated_total: Decimal,
    scope: str,
    scope_key: str,
) -> AmbiguitySpec:
    question = (
        f"The sum of the extracted components ({calculated_total}) is {abs(gap)} more "
        f"than the stated total ({extraction.total}). What reduces the total by "
        f"{abs(gap)}?"
    )
    options = [
        {"id": "discount", "label": "A discount / credit to subtract"},
        {"id": "over_extraction", "label": "I double-counted a component"},
        {"id": "other", "label": "Something else / not sure"},
    ]
    return AmbiguitySpec(
        ambiguity_type=AMB_UNRECORDED_DISCOUNT,
        question=question,
        options=options,
        agent_hypothesis="discount",
        proposed_scope=scope,
        proposed_scope_key=scope_key,
        evidence=_base_evidence(extraction, rules, calculated_total, gap),
    )


def _line_tax_spec(
    extraction: InvoiceExtraction,
    rules: TemplateValidationRules,
    scope: str,
    scope_key: str,
) -> AmbiguitySpec:
    line_total = sum((item.amount for item in extraction.line_items), Decimal("0"))
    question = (
        f"The line items sum to {line_total}, but the subtotal is "
        f"{extraction.subtotal}. How should the line amounts be read?"
    )
    options = [
        {"id": "yes", "label": "Line amounts already include tax (GST/VAT-inclusive)"},
        {"id": "no", "label": "Line amounts are net of tax (extraction error)"},
        {"id": "other", "label": "Something else / not sure"},
    ]
    evidence = _base_evidence(
        extraction, rules, compute_calculated_total(extraction, rules), Decimal("0")
    )
    evidence["line_items_total"] = str(line_total)
    return AmbiguitySpec(
        ambiguity_type=AMB_LINE_TAX_INCLUSIVE,
        question=question,
        options=options,
        agent_hypothesis="yes",
        proposed_scope=scope,
        proposed_scope_key=scope_key,
        evidence=evidence,
    )


def generate_clarification(
    db: Session,
    *,
    document_id: UUID,
    vendor_key: Optional[str],
    spec: AmbiguitySpec,
    round: int = 1,
) -> ClarificationRequest:
    """Persist a clarification request for a human to answer."""
    clarification = ClarificationRequest(
        id=uuid4(),
        document_id=document_id,
        vendor_key=vendor_key,
        ambiguity_type=spec.ambiguity_type,
        question=spec.question,
        options=spec.options,
        agent_hypothesis=spec.agent_hypothesis,
        evidence=spec.evidence,
        proposed_scope=spec.proposed_scope,
        proposed_scope_key=spec.proposed_scope_key,
        status=ClarificationStatus.OPEN,
        round=round,
    )
    db.add(clarification)
    db.flush()
    return clarification


def rule_spec_from_answer(
    clarification: ClarificationRequest,
    *,
    answer_option_id: str,
    answer_note: Optional[str],
    scope: str,
    scope_key: str,
) -> Optional[Dict[str, Any]]:
    """Map a human answer onto a concrete rule spec for ``knowledge.create_rule``.

    Returns None when the answer carries nothing learnable (e.g. "not sure" with no
    note), in which case the caller falls back to escalation without inventing a rule.
    """
    note = (answer_note or "").strip() or None
    base = {"scope": scope, "scope_key": scope_key}

    if clarification.ambiguity_type == AMB_MISSING_CHARGE:
        target = _MISSING_TARGET_BY_OPTION.get(answer_option_id)
        if target is not None:
            if note:
                # Human named the exact line -> deterministic capture rule (Tier A).
                return {
                    **base,
                    "rule_type": RuleType.FIELD_MAPPING,
                    "trigger": "total exceeds the sum of extracted components",
                    "directive": (
                        f"Capture the '{note}' line amount into {target} for this "
                        "vendor; it is a charge included in the total but easy to miss."
                    ),
                    "capture_anchor": note,
                    "target_field": target,
                    "payload": {},
                }
            # No exact line named -> a prompt hint (Tier C, backstopped by the math).
            return {
                **base,
                "rule_type": RuleType.HINT,
                "trigger": "total exceeds the sum of extracted components",
                "directive": (
                    f"This vendor sometimes has an unlisted {target} (e.g. a "
                    f"surcharge) not shown in the main table; find it and include it "
                    f"in {target}."
                ),
                "capture_anchor": None,
                "target_field": target,
                "payload": {},
            }
        if answer_option_id == "other" and note:
            return {
                **base,
                "rule_type": RuleType.HINT,
                "trigger": "total exceeds the sum of extracted components",
                "directive": note,
                "capture_anchor": None,
                "target_field": None,
                "payload": {},
            }
        return None

    if clarification.ambiguity_type == AMB_LINE_TAX_INCLUSIVE:
        rules = _rules_from_evidence(clarification)
        if answer_option_id == "yes":
            payload = rules.model_copy(
                update={"validate_line_items": True, "line_amount_includes_tax": True}
            ).model_dump(mode="json")
            return {
                **base,
                "rule_type": RuleType.VALIDATION,
                "trigger": "line items sum differs from subtotal",
                "directive": (
                    "Line item amounts include their own tax for this vendor "
                    "(compare line totals against subtotal + tax)."
                ),
                "payload": payload,
                "capture_anchor": None,
                "target_field": None,
            }
        if answer_option_id == "no" and note:
            return {
                **base,
                "rule_type": RuleType.HINT,
                "trigger": "line items sum differs from subtotal",
                "directive": note,
                "capture_anchor": None,
                "target_field": None,
                "payload": {},
            }
        return None

    if clarification.ambiguity_type == AMB_UNRECORDED_DISCOUNT:
        rules = _rules_from_evidence(clarification)
        if answer_option_id == "discount":
            payload = rules.model_copy(
                update={"subtract_discounts": True}
            ).model_dump(mode="json")
            return {
                **base,
                "rule_type": RuleType.VALIDATION,
                "trigger": "components sum exceeds the stated total",
                "directive": (
                    "This vendor applies a discount/credit that must be subtracted "
                    "from the total; record it as a discount."
                ),
                "payload": payload,
                "capture_anchor": None,
                "target_field": None,
            }
        if note:
            return {
                **base,
                "rule_type": RuleType.HINT,
                "trigger": "components sum exceeds the stated total",
                "directive": note,
                "capture_anchor": None,
                "target_field": None,
                "payload": {},
            }
        return None

    return None


def _rules_from_evidence(
    clarification: ClarificationRequest,
) -> TemplateValidationRules:
    rules_data = (clarification.evidence or {}).get("rules")
    if rules_data:
        return TemplateValidationRules.model_validate(rules_data)
    return TemplateValidationRules()


def list_open_clarifications(db: Session) -> List[ClarificationRequest]:
    return list(
        db.execute(
            select(ClarificationRequest)
            .where(ClarificationRequest.status == ClarificationStatus.OPEN)
            .order_by(ClarificationRequest.created_at.desc())
        ).scalars().all()
    )


def get_clarification(
    db: Session, clarification_id: UUID
) -> Optional[ClarificationRequest]:
    return db.get(ClarificationRequest, clarification_id)


def open_clarifications_for_document(
    db: Session, document_id: UUID
) -> List[ClarificationRequest]:
    return list(
        db.execute(
            select(ClarificationRequest)
            .where(ClarificationRequest.document_id == document_id)
            .where(ClarificationRequest.status == ClarificationStatus.OPEN)
            .order_by(ClarificationRequest.created_at.asc())
        ).scalars().all()
    )
