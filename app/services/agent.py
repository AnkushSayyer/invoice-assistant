"""Autonomous invoice-reimbursement agent.

Orchestrates the tools in ``agent_tools`` into a goal-directed loop:

    perceive -> reconcile -> (remediate) -> duplicate/policy checks -> decide -> act

The agent decides on its own whether to APPROVE, REJECT, or ESCALATE a claim,
self-corrects reconciliation failures, remembers per-vendor quirks, and records a
full audit trail. It sits *alongside* the existing manual upload/template pipeline
rather than replacing it.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from types import SimpleNamespace
from typing import List, Optional
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from sqlalchemy import select

from app.db.models import AgentRun, ClarificationStatus, Document, RuleScope, RuleType
from app.schemas.agent import (
    AgentDecision,
    AgentResult,
    AgentStep,
    ExpensePolicy,
    ResolvePreviewResponse,
)
from app.schemas.clarification import ClarificationResponse
from app.schemas.documents import DocumentStatus
from app.schemas.extraction import InvoiceExtraction
from app.schemas.validation_rules import TemplateValidationRules
from app.services.agent_tools import (
    LOW_CONFIDENCE_THRESHOLD,
    Perception,
    check_duplicate,
    check_expense_policy,
    learn_vendor_policy,
    lookup_vendor_policy,
    needs_escalation_over_limit,
    perceive,
    recover_missing_charge,
    reconcile_math,
    remediate,
)
from app.services.clarification import (
    detect_ambiguity,
    generate_clarification,
    get_clarification,
    list_open_clarifications,
    rule_spec_from_answer,
)
from app.services.document_processor import (
    DocumentNotFoundError,
    InvalidDocumentStateError,
)
from app.services.extractor import PDFReadError, read_pdf_text
from app.services.knowledge import (
    VendorKnowledge,
    add_few_shot,
    build_vendor_knowledge,
    create_rule,
    record_event,
)
from app.services.validation import (
    MATH_TOLERANCE,
    ValidationResult,
    derive_validation_rules,
    load_template_validation_rules,
    validate_invoice_math,
)

# Confidence boost when the arithmetic reconciles exactly, since a clean
# reconciliation is strong independent evidence the extraction is correct.
_RECONCILE_BONUS = 0.2

# Max clarification rounds before a document falls back to full manual escalation,
# so the ask-and-learn loop can never spin forever.
MAX_CLARIFY_ROUNDS = 3


@dataclass
class _Assessment:
    """Perception + rule resolution + self-correction, shared by the first run and
    the post-clarification re-run."""

    perception: Perception
    extraction: InvoiceExtraction
    rules: TemplateValidationRules
    reconciliation: ValidationResult
    tolerance: Decimal
    calculated_total: Decimal
    knowledge: VendorKnowledge
    remediations: List[str] = field(default_factory=list)
    steps: List[AgentStep] = field(default_factory=list)


def _resolve_rules(
    db: Session,
    perception: Perception,
    extraction: InvoiceExtraction,
    knowledge: VendorKnowledge,
    steps: List[AgentStep],
) -> tuple[TemplateValidationRules, Decimal]:
    """Learned validation rule > legacy vendor policy > derived-from-extraction."""
    tolerance = MATH_TOLERANCE * max(1, perception.segments)
    vendor_policy = lookup_vendor_policy(db, perception.vendor_key)
    if knowledge.validation_rules is not None:
        rules = knowledge.validation_rules
        steps.append(
            AgentStep(
                tool="lookup_vendor_policy",
                summary=f"applied learned validation rule for {perception.vendor_key}",
                data={"vendor_key": perception.vendor_key},
            )
        )
    elif vendor_policy is not None:
        rules = load_template_validation_rules(vendor_policy.validation_rules)
        if vendor_policy.tolerance is not None:
            tolerance = vendor_policy.tolerance
        steps.append(
            AgentStep(
                tool="lookup_vendor_policy",
                summary=f"applied remembered rules for {perception.vendor_key} "
                f"(seen {vendor_policy.times_seen}x)",
                data={"vendor_key": perception.vendor_key},
            )
        )
    else:
        rules = derive_validation_rules(extraction)
        steps.append(
            AgentStep(
                tool="lookup_vendor_policy",
                summary="no vendor memory; derived rules from the extraction",
                ok=False,
            )
        )
    return rules, tolerance


def _self_correct(
    db: Session,
    pdf_bytes: bytes,
    *,
    perception: Perception,
    extraction: InvoiceExtraction,
    rules: TemplateValidationRules,
    reconciliation: ValidationResult,
    knowledge: VendorKnowledge,
    claimed_amount: Decimal,
    tolerance: Decimal,
    remediations: List[str],
    steps: List[AgentStep],
) -> tuple[Perception, InvoiceExtraction, TemplateValidationRules, ValidationResult]:
    """Try, in order: deterministic capture recovery (Tier A) -> re-extract with
    learned knowledge (Tier C) -> formula search (remediate)."""
    # Tier A: deterministic capture of a known missing charge (no LLM).
    if knowledge.capture_rules and perception.raw_text:
        recovery = recover_missing_charge(
            extraction,
            rules,
            perception.raw_text,
            claimed_amount,
            tolerance,
            knowledge.capture_rules,
        )
        if recovery is not None and recovery.result.is_valid:
            remediations.append(recovery.explanation)
            steps.append(
                AgentStep(tool="recover_missing_charge", summary=recovery.explanation)
            )
            return perception, recovery.extraction, rules, recovery.result

    # Tier C: re-extract with learned directives + few-shots, then re-check.
    if perception.source == "llm" and (
        knowledge.directives or knowledge.few_shots
    ):
        reperceived = perceive(db, pdf_bytes, knowledge=knowledge)
        new_extraction = reperceived.extraction
        new_recon = reconcile_math(new_extraction, rules, claimed_amount, tolerance)
        if (
            not new_recon.is_valid
            and knowledge.capture_rules
            and reperceived.raw_text
        ):
            rec = recover_missing_charge(
                new_extraction,
                rules,
                reperceived.raw_text,
                claimed_amount,
                tolerance,
                knowledge.capture_rules,
            )
            if rec is not None and rec.result.is_valid:
                new_extraction = rec.extraction
                new_recon = rec.result
                remediations.append(rec.explanation)
        if new_recon.is_valid:
            steps.append(
                AgentStep(
                    tool="re_extract",
                    summary="re-extracted with learned vendor knowledge",
                )
            )
            return reperceived, new_extraction, rules, new_recon

    # Formula search: adopt an alternative total formula that reconciles.
    fix = remediate(extraction, claimed_amount, tolerance)
    if fix is not None:
        remediations.append(fix.explanation)
        steps.append(
            AgentStep(
                tool="remediate",
                summary=fix.explanation,
                data={"rules": fix.rules.model_dump(mode="json")},
            )
        )
        return perception, extraction, fix.rules, fix.result

    steps.append(
        AgentStep(
            tool="remediate",
            summary="no alternative formula reconciled the claim",
            ok=False,
        )
    )
    return perception, extraction, rules, reconciliation


def _assess(
    db: Session,
    pdf_bytes: bytes,
    claimed_amount: Decimal,
    *,
    category: Optional[str] = None,
) -> _Assessment:
    """Perceive, resolve rules (using learned knowledge), reconcile, self-correct."""
    steps: List[AgentStep] = []
    remediations: List[str] = []

    perception = perceive(db, pdf_bytes)
    steps.append(
        AgentStep(
            tool="perceive",
            summary=(
                f"extracted via {perception.source}; "
                f"{perception.segments} segment(s); "
                f"confidence {perception.confidence:.2f}"
            ),
            data={
                "source": perception.source,
                "segments": perception.segments,
                "vendor_key": perception.vendor_key,
            },
        )
    )
    extraction = perception.extraction

    knowledge = build_vendor_knowledge(db, perception.vendor_key, category=category)
    rules, tolerance = _resolve_rules(db, perception, extraction, knowledge, steps)

    reconciliation = reconcile_math(extraction, rules, claimed_amount, tolerance)
    steps.append(
        AgentStep(
            tool="reconcile_math",
            summary=(
                "math reconciles"
                if reconciliation.is_valid
                else "math does not reconcile"
            ),
            ok=reconciliation.is_valid,
            data={"errors": reconciliation.errors},
        )
    )

    if not reconciliation.is_valid:
        perception, extraction, rules, reconciliation = _self_correct(
            db,
            pdf_bytes,
            perception=perception,
            extraction=extraction,
            rules=rules,
            reconciliation=reconciliation,
            knowledge=knowledge,
            claimed_amount=claimed_amount,
            tolerance=tolerance,
            remediations=remediations,
            steps=steps,
        )

    return _Assessment(
        perception=perception,
        extraction=extraction,
        rules=rules,
        reconciliation=reconciliation,
        tolerance=tolerance,
        calculated_total=reconciliation.calculated_total,
        knowledge=knowledge,
        remediations=remediations,
        steps=steps,
    )


def _confidence(perception_confidence: float, reconciled: bool) -> float:
    if reconciled:
        # A clean reconciliation is strong independent evidence; lift a low OCR
        # score to at least the threshold before applying the reconcile bonus.
        return min(
            0.99,
            max(perception_confidence, LOW_CONFIDENCE_THRESHOLD) + _RECONCILE_BONUS,
        )
    return perception_confidence


def _decide(
    *,
    duplicate,
    violations: List[str],
    reconciliation: ValidationResult,
    perception_confidence: float,
    over_limit: bool,
    policy: ExpensePolicy,
    claimed_amount: Decimal,
    clarification_spec,
) -> tuple[AgentDecision, List[str]]:
    """The decision cascade. CLARIFY (ask a targeted question) takes precedence over
    a blanket ESCALATE whenever the ambiguity is answerable and learnable."""
    if duplicate is not None:
        return AgentDecision.REJECT, [
            f"duplicate submission of invoice {duplicate.invoice_number} "
            f"(already processed as {duplicate.document_id})"
        ]
    if violations:
        return AgentDecision.REJECT, list(violations)
    if not reconciliation.is_valid and clarification_spec is not None:
        return AgentDecision.CLARIFY, [
            "the invoice math did not reconcile; asking a targeted question to "
            "resolve it and learn the answer",
            clarification_spec.question,
        ]
    if policy.require_math_reconciliation and not reconciliation.is_valid:
        return AgentDecision.ESCALATE, [
            "math could not be reconciled after remediation",
            *reconciliation.errors,
        ]
    if perception_confidence < LOW_CONFIDENCE_THRESHOLD and not reconciliation.is_valid:
        return AgentDecision.ESCALATE, [
            f"extraction confidence {perception_confidence:.2f} is below the "
            f"{LOW_CONFIDENCE_THRESHOLD:.2f} threshold and the math did not "
            f"reconcile; a clearer document is needed"
        ]
    if over_limit:
        return AgentDecision.ESCALATE, [
            f"claim {claimed_amount} is above the auto-approve limit "
            f"{policy.auto_approve_limit}; needs human sign-off"
        ]
    reasons = ["math reconciles, no duplicate, within policy"]
    if perception_confidence < LOW_CONFIDENCE_THRESHOLD:
        reasons.append(
            f"low extraction confidence {perception_confidence:.2f} accepted "
            "because the deterministic math reconciles exactly"
        )
    return AgentDecision.APPROVE, reasons


def run_agent(
    db: Session,
    pdf_bytes: bytes,
    filename: str,
    claimed_amount: Decimal,
    *,
    category: Optional[str] = None,
    expense_policy: Optional[ExpensePolicy] = None,
) -> AgentResult:
    """Process one invoice end-to-end and commit an autonomous decision."""
    policy = expense_policy or ExpensePolicy.from_env()

    assessment = _assess(db, pdf_bytes, claimed_amount, category=category)
    perception = assessment.perception
    extraction = assessment.extraction
    rules = assessment.rules
    reconciliation = assessment.reconciliation
    steps = assessment.steps

    duplicate = check_duplicate(db, extraction)
    steps.append(
        AgentStep(
            tool="check_duplicate",
            summary=(
                f"duplicate of {duplicate.document_id}"
                if duplicate is not None
                else "no duplicate found"
            ),
            ok=duplicate is None,
            data=(
                {"document_id": str(duplicate.document_id)}
                if duplicate is not None
                else {}
            ),
        )
    )

    violations = check_expense_policy(
        extraction, claimed_amount, policy, category=category
    )
    over_limit = needs_escalation_over_limit(claimed_amount, policy)
    steps.append(
        AgentStep(
            tool="check_expense_policy",
            summary=(
                "; ".join(violations)
                if violations
                else ("above auto-approve limit" if over_limit else "within policy")
            ),
            ok=not violations,
            data={"violations": violations, "over_auto_approve_limit": over_limit},
        )
    )

    clarification_spec = None
    if not reconciliation.is_valid:
        clarification_spec = detect_ambiguity(
            extraction,
            rules,
            reconciliation,
            vendor_key=perception.vendor_key,
            category=category,
            tolerance=assessment.tolerance,
        )

    calculated_total = reconciliation.calculated_total
    decision, reasons = _decide(
        duplicate=duplicate,
        violations=violations,
        reconciliation=reconciliation,
        perception_confidence=perception.confidence,
        over_limit=over_limit,
        policy=policy,
        claimed_amount=claimed_amount,
        clarification_spec=clarification_spec,
    )
    confidence = _confidence(perception.confidence, reconciliation.is_valid)
    steps.append(
        AgentStep(
            tool="decide",
            summary=f"decision={decision.value}; confidence {confidence:.2f}",
            ok=decision == AgentDecision.APPROVE,
            data={"reasons": reasons},
        )
    )

    # Act: learn on approval, record stats, persist the document + audit run.
    if decision == AgentDecision.APPROVE:
        learn_vendor_policy(
            db,
            perception.vendor_key,
            rules,
            display_name=extraction.vendor,
            tolerance=tolerance_or_none(assessment.tolerance),
            category=category,
        )
        record_event(
            db,
            perception.vendor_key,
            seen=True,
            approved=True,
            display_name=extraction.vendor,
            category=category,
        )
    elif decision == AgentDecision.CLARIFY:
        record_event(
            db,
            perception.vendor_key,
            seen=True,
            clarified=True,
            display_name=extraction.vendor,
            category=category,
        )
    else:
        record_event(
            db,
            perception.vendor_key,
            seen=True,
            display_name=extraction.vendor,
            category=category,
        )

    approved_amount = claimed_amount if decision == AgentDecision.APPROVE else None

    document = _persist_document(
        db,
        filename=filename,
        pdf_bytes=pdf_bytes,
        extraction=extraction,
        rules=rules,
        claimed_amount=claimed_amount,
        calculated_total=calculated_total,
        approved_amount=approved_amount,
        vendor_key=perception.vendor_key,
        decision=decision,
        reasons=reasons,
        raw_text=perception.raw_text,
    )

    clarifications: List[ClarificationResponse] = []
    if decision == AgentDecision.CLARIFY and clarification_spec is not None:
        clarification = generate_clarification(
            db,
            document_id=document.id,
            vendor_key=perception.vendor_key,
            spec=clarification_spec,
            round=1,
        )
        clarifications = [ClarificationResponse.model_validate(clarification)]

    result = AgentResult(
        decision=decision,
        confidence=confidence,
        reasons=reasons,
        remediations=assessment.remediations,
        steps=steps,
        extraction=extraction,
        validation_rules=rules,
        calculated_total=calculated_total,
        claimed_amount=claimed_amount,
        approved_amount=approved_amount,
        vendor_key=perception.vendor_key,
        duplicate_of=str(duplicate.document_id) if duplicate is not None else None,
        source=perception.source,
        document_id=str(document.id),
        clarifications=clarifications,
    )

    _persist_run(db, document, result)
    db.commit()
    db.refresh(document)
    return result


def tolerance_or_none(tolerance: Decimal) -> Optional[Decimal]:
    """A vendor-specific tolerance is only worth remembering when it is non-default."""
    return tolerance if tolerance != MATH_TOLERANCE else None


def _status_for_decision(decision: AgentDecision) -> str:
    if decision == AgentDecision.APPROVE:
        return DocumentStatus.PROCESSED
    if decision == AgentDecision.REJECT:
        return DocumentStatus.FAILED
    if decision == AgentDecision.CLARIFY:
        return DocumentStatus.NEEDS_INPUT  # parked awaiting a human answer
    return DocumentStatus.PENDING  # ESCALATE -> waits in the human review queue


def _persist_document(
    db: Session,
    *,
    filename: str,
    pdf_bytes: bytes,
    extraction,
    rules: TemplateValidationRules,
    claimed_amount: Decimal,
    calculated_total: Decimal,
    approved_amount: Optional[Decimal],
    vendor_key: Optional[str],
    decision: AgentDecision,
    reasons: List[str],
    raw_text: str = "",
) -> Document:
    document = Document(
        id=uuid4(),
        filename=filename,
        # The agent path does not fingerprint; reuse the field for the vendor key
        # so existing list/review views keep working.
        fingerprint=vendor_key or "agent",
        vendor_key=vendor_key,
        raw_text=raw_text or None,
        extracted_fields=extraction.model_dump(mode="json"),
        claimed_amount=claimed_amount,
        calculated_total=calculated_total,
        approved_amount=approved_amount,
        validation_rules=rules.model_dump(mode="json"),
        pdf_data=pdf_bytes,
        has_pdf=True,
        status=_status_for_decision(decision),
        error_message=(
            "; ".join(reasons) if decision != AgentDecision.APPROVE else None
        ),
        auto_approved=decision == AgentDecision.APPROVE,
    )
    db.add(document)
    db.flush()  # assign document.id without committing yet
    return document


def _persist_run(db: Session, document: Document, result: AgentResult) -> AgentRun:
    duplicate_of: Optional[UUID] = None
    if result.duplicate_of:
        try:
            duplicate_of = UUID(result.duplicate_of)
        except ValueError:
            duplicate_of = None
    run = AgentRun(
        document_id=document.id,
        filename=document.filename,
        decision=result.decision.value,
        confidence=result.confidence,
        source=result.source,
        vendor_key=result.vendor_key,
        duplicate_of=duplicate_of,
        reasons=result.reasons,
        remediations=result.remediations,
        steps=[step.model_dump(mode="json") for step in result.steps],
    )
    db.add(run)
    return run


def _document_tolerance(document: Document) -> Decimal:
    segment_count = len(document.invoice_segments or []) or 1
    return MATH_TOLERANCE * segment_count


def _example_text_for(document: Document) -> Optional[str]:
    """Recover the invoice's layout text for use as a learned few-shot example."""
    if document.raw_text:
        return document.raw_text
    if not document.pdf_data:
        return None
    try:
        text = read_pdf_text(document.pdf_data)
    except PDFReadError:
        return None
    return text or None


def _stored_extraction(document: Document) -> InvoiceExtraction:
    if not document.extracted_fields:
        raise InvalidDocumentStateError(
            f"Document {document.id} has no extracted fields to resolve"
        )
    return InvoiceExtraction.model_validate(document.extracted_fields)


def _format_rules_directive(rules: TemplateValidationRules) -> str:
    """Human-readable summary of a total formula, for a learned rule's directive."""
    formula = " + ".join(rules.total_components)
    if rules.subtract_discounts:
        formula += " - discounts"
    return formula


def _resolve_learn_scope(
    document: Document,
    learn_scope: str,
    learn_scope_key: Optional[str],
    category: Optional[str],
) -> tuple[str, str]:
    """Final (scope, scope_key) for a rule learned on resolve.

    Honour an explicit key, else default: vendor -> the document's vendor_key,
    category -> the category, global -> the global key.
    """
    if learn_scope_key:
        return learn_scope, learn_scope_key
    if learn_scope == RuleScope.VENDOR:
        return RuleScope.VENDOR, document.vendor_key or ""
    if learn_scope == RuleScope.CATEGORY:
        return RuleScope.CATEGORY, category or ""
    return RuleScope.GLOBAL, RuleScope.GLOBAL_KEY


def preview_resolution(
    db: Session,
    document_id: UUID,
    *,
    approved_fields: Optional[InvoiceExtraction] = None,
    validation_rules: Optional[TemplateValidationRules] = None,
    capture_anchor: Optional[str] = None,
    capture_target_field: Optional[str] = None,
) -> ResolvePreviewResponse:
    """Read-only dry-run: apply the reviewer's edits + a candidate rule to the
    current document and report whether it reconciles. Persists nothing, so it is
    safe to call on every keystroke while the reviewer tunes a rule.
    """
    document = db.get(Document, document_id)
    if document is None:
        raise DocumentNotFoundError(f"Document {document_id} not found")

    extraction = approved_fields or _stored_extraction(document)
    rules = (
        validation_rules
        or load_template_validation_rules(document.validation_rules)
        or derive_validation_rules(extraction)
    )
    tolerance = _document_tolerance(document)
    claimed_amount = document.claimed_amount

    recovered_fields: Optional[InvoiceExtraction] = None
    capture_previewable = True
    if capture_anchor and capture_target_field:
        if document.raw_text:
            capture_rule = SimpleNamespace(
                capture_anchor=capture_anchor, target_field=capture_target_field
            )
            recovery = recover_missing_charge(
                extraction,
                rules,
                document.raw_text,
                claimed_amount if claimed_amount is not None else Decimal("0"),
                tolerance,
                [capture_rule],
            )
            if recovery is not None:
                extraction = recovery.extraction
                recovered_fields = recovery.extraction
        else:
            capture_previewable = False

    validation = validate_invoice_math(
        extraction,
        rules,
        claimed_amount=claimed_amount,
        tolerance=tolerance,
    )
    return ResolvePreviewResponse(
        is_valid=validation.is_valid,
        calculated_total=validation.calculated_total,
        errors=validation.errors,
        recovered_fields=recovered_fields,
        capture_previewable=capture_previewable,
    )


def resolve_document(
    db: Session,
    document_id: UUID,
    *,
    decision: str,
    approved_fields: Optional[InvoiceExtraction] = None,
    validation_rules: Optional[TemplateValidationRules] = None,
    approved_amount: Optional[Decimal] = None,
    category: Optional[str] = None,
    note: Optional[str] = None,
    force: bool = False,
    learn_vendor: bool = True,
    learn_scope: str = "vendor",
    learn_scope_key: Optional[str] = None,
    capture_anchor: Optional[str] = None,
    capture_target_field: Optional[str] = None,
    directive: Optional[str] = None,
) -> AgentResult:
    """Human-in-the-loop resolution of an escalated or rejected agent decision.

    A reviewer supplies corrected fields and either approves (re-running the
    deterministic math check, optionally forcing past a residual mismatch) or
    rejects. The corrected rules are written back to vendor memory on approval, and
    the human decision is appended to the same audit trail as autonomous runs.
    """
    document = db.get(Document, document_id)
    if document is None:
        raise DocumentNotFoundError(f"Document {document_id} not found")
    if document.status == DocumentStatus.PROCESSED:
        raise InvalidDocumentStateError(
            f"Document {document_id} is already approved and cannot be re-resolved"
        )

    steps: List[AgentStep] = [
        AgentStep(
            tool="human_review",
            summary=f"reviewer chose to {decision}",
            data={"note": note} if note else {},
        )
    ]

    if decision == "reject":
        extraction = approved_fields or _stored_extraction(document)
        rules = load_template_validation_rules(document.validation_rules)
        reason = note or "rejected by reviewer"
        document.status = DocumentStatus.FAILED
        document.error_message = reason
        document.auto_approved = False
        document.approved_amount = None  # nothing reimbursed on rejection
        result = AgentResult(
            decision=AgentDecision.REJECT,
            confidence=1.0,
            reasons=[reason],
            remediations=[],
            steps=steps,
            extraction=extraction,
            validation_rules=rules,
            calculated_total=document.calculated_total or Decimal("0"),
            claimed_amount=document.claimed_amount or Decimal("0"),
            approved_amount=None,
            vendor_key=document.vendor_key,
            source="human",
            document_id=str(document.id),
        )
        _persist_run(db, document, result)
        db.commit()
        db.refresh(document)
        return result

    # decision == "approve"
    extraction = approved_fields or _stored_extraction(document)
    rules = (
        validation_rules
        or load_template_validation_rules(document.validation_rules)
        or derive_validation_rules(extraction)
    )
    tolerance = _document_tolerance(document)
    validation = validate_invoice_math(
        extraction,
        rules,
        claimed_amount=document.claimed_amount,
        tolerance=tolerance,
    )
    reasons: List[str] = ["approved by reviewer"]
    if approved_amount is not None:
        # The reviewer's manual amount is authoritative, so a claimed-vs-calculated
        # mismatch does not block approval; it is recorded for the audit trail.
        final_approved_amount = approved_amount
        if not validation.is_valid:
            reasons.append(
                "reviewer set approved amount, overriding math discrepancy: "
                + "; ".join(validation.errors)
            )
    else:
        if not validation.is_valid and not force:
            raise InvalidDocumentStateError("; ".join(validation.errors))
        if not validation.is_valid:
            reasons.append(
                "forced despite unresolved math: " + "; ".join(validation.errors)
            )
        final_approved_amount = (
            document.claimed_amount
            if document.claimed_amount is not None
            else validation.calculated_total
        )
    if note:
        reasons.append(note)

    document.status = DocumentStatus.PROCESSED
    document.extracted_fields = extraction.model_dump(mode="json")
    document.calculated_total = validation.calculated_total
    document.approved_amount = final_approved_amount
    document.validation_rules = rules.model_dump(mode="json")
    document.auto_approved = False  # human-approved, not autonomous
    document.error_message = (
        None if validation.is_valid else "approved with unresolved math"
    )

    if learn_vendor:
        scope, scope_key = _resolve_learn_scope(
            document, learn_scope, learn_scope_key, category
        )
        learned: List[str] = []

        # Persist the typed, scoped lessons the reviewer just validated so future
        # invoices in this scope apply them automatically.
        if scope_key:
            if validation_rules is not None:
                create_rule(
                    db,
                    scope=scope,
                    scope_key=scope_key,
                    rule_type=RuleType.VALIDATION,
                    trigger="reviewer-corrected total formula",
                    directive=(
                        "Validate totals with formula: "
                        + _format_rules_directive(rules)
                    ),
                    payload=rules.model_dump(mode="json"),
                    source="human",
                    document_id=document.id,
                )
                learned.append(f"validation rule ({scope})")
            if capture_anchor and capture_target_field:
                create_rule(
                    db,
                    scope=scope,
                    scope_key=scope_key,
                    rule_type=RuleType.FIELD_MAPPING,
                    trigger="reviewer-taught capture rule",
                    directive=(
                        f"Capture the '{capture_anchor}' line amount into "
                        f"{capture_target_field} for this {scope}."
                    ),
                    capture_anchor=capture_anchor,
                    target_field=capture_target_field,
                    source="human",
                    document_id=document.id,
                )
                learned.append(
                    f"capture '{capture_anchor}' -> {capture_target_field}"
                )
            if directive:
                create_rule(
                    db,
                    scope=scope,
                    scope_key=scope_key,
                    rule_type=RuleType.HINT,
                    trigger="reviewer instruction",
                    directive=directive,
                    source="human",
                    document_id=document.id,
                )
                learned.append(f"extraction hint ({scope})")

        # Keep the few-shot side-effect (drives LLM re-extraction) for vendor scope:
        # the invoice text paired with the human-approved fields, so next time the
        # LLM path sees this vendor it extracts to the corrected shape on its own.
        if scope == RuleScope.VENDOR and document.vendor_key:
            example_text = _example_text_for(document)
            learn_vendor_policy(
                db,
                document.vendor_key,
                rules,
                display_name=extraction.vendor,
                tolerance=tolerance if tolerance != MATH_TOLERANCE else None,
                category=category,
                example_text=example_text,
                example_fields=(
                    extraction.model_dump(mode="json") if example_text else None
                ),
            )

        if learned:
            reasons.append("learned: " + "; ".join(learned))

    steps.append(
        AgentStep(
            tool="reconcile_math",
            summary=(
                "math reconciles"
                if validation.is_valid
                else "approved despite unresolved mismatch"
            ),
            ok=validation.is_valid,
            data={"errors": validation.errors},
        )
    )
    result = AgentResult(
        decision=AgentDecision.APPROVE,
        confidence=1.0,
        reasons=reasons,
        remediations=[],
        steps=steps,
        extraction=extraction,
        validation_rules=rules,
        calculated_total=validation.calculated_total,
        claimed_amount=document.claimed_amount or Decimal("0"),
        approved_amount=final_approved_amount,
        vendor_key=document.vendor_key,
        source="human",
        document_id=str(document.id),
    )
    _persist_run(db, document, result)
    db.commit()
    db.refresh(document)
    return result


def list_training_examples(db: Session, limit: int = 200) -> List[Document]:
    """Human-verified documents usable as labelled data for uptraining a parser.

    A document qualifies once a reviewer has approved it (``source='human'``), which
    means its stored ``extracted_fields`` are trusted labels for the attached PDF.
    Feed these to Document AI Workbench to uptrain the Invoice Parser.
    """
    stmt = (
        select(Document)
        .join(AgentRun, AgentRun.document_id == Document.id)
        .where(Document.status == DocumentStatus.PROCESSED)
        .where(Document.has_pdf.is_(True))
        .where(AgentRun.source == "human")
        .where(AgentRun.decision == AgentDecision.APPROVE.value)
        .order_by(Document.updated_at.desc())
        .distinct()
        .limit(limit)
    )
    return list(db.execute(stmt).scalars().all())


def _resolve_scope(
    clarification,
    document: Document,
    confirmed_scope: Optional[str],
    confirmed_scope_key: Optional[str],
) -> tuple[str, str]:
    """Final (scope, scope_key) for the learned rule: honour the reviewer's
    confirmation, else fall back to the agent's proposal."""
    scope = confirmed_scope or clarification.proposed_scope
    if confirmed_scope_key:
        return scope, confirmed_scope_key
    if scope == RuleScope.VENDOR:
        return scope, document.vendor_key or clarification.proposed_scope_key
    if scope == RuleScope.GLOBAL:
        return scope, RuleScope.GLOBAL_KEY
    return scope, clarification.proposed_scope_key


def _apply_decision_to_document(
    document: Document,
    *,
    extraction: InvoiceExtraction,
    rules: TemplateValidationRules,
    calculated_total: Decimal,
    approved_amount: Optional[Decimal],
    decision: AgentDecision,
    reasons: List[str],
    raw_text: str,
) -> None:
    document.extracted_fields = extraction.model_dump(mode="json")
    document.validation_rules = rules.model_dump(mode="json")
    document.calculated_total = calculated_total
    document.approved_amount = approved_amount
    document.status = _status_for_decision(decision)
    document.auto_approved = False  # a human clarification loop was involved
    document.error_message = (
        None if decision == AgentDecision.APPROVE else "; ".join(reasons)
    )
    if raw_text:
        document.raw_text = raw_text


def _rerun_document(
    db: Session,
    document: Document,
    *,
    round: int,
    policy: ExpensePolicy,
    category: Optional[str] = None,
) -> AgentResult:
    """Re-run the pipeline over a stored document after a clarification was answered.

    The newly-learned rule is now in the knowledge base, so the deterministic
    recovery / corrected formula / learned few-shot applies automatically. Bounded
    by ``MAX_CLARIFY_ROUNDS`` so it can never loop forever.
    """
    claimed_amount = document.claimed_amount or Decimal("0")
    steps: List[AgentStep] = [
        AgentStep(
            tool="human_review",
            summary=f"re-running after clarification (round {round})",
        )
    ]

    if not document.pdf_data:
        document.status = DocumentStatus.PENDING  # cannot re-extract -> manual queue
        rules = load_template_validation_rules(document.validation_rules)
        result = AgentResult(
            decision=AgentDecision.ESCALATE,
            confidence=1.0,
            reasons=["no stored PDF to re-run; needs manual review"],
            remediations=[],
            steps=steps,
            extraction=_stored_extraction(document),
            validation_rules=rules,
            calculated_total=document.calculated_total or Decimal("0"),
            claimed_amount=claimed_amount,
            approved_amount=None,
            vendor_key=document.vendor_key,
            source="human",
            document_id=str(document.id),
        )
        _persist_run(db, document, result)
        return result

    assessment = _assess(db, document.pdf_data, claimed_amount, category=category)
    perception = assessment.perception
    extraction = assessment.extraction
    rules = assessment.rules
    reconciliation = assessment.reconciliation
    steps.extend(assessment.steps)

    duplicate = check_duplicate(db, extraction, exclude_document_id=document.id)
    violations = check_expense_policy(
        extraction, claimed_amount, policy, category=category
    )
    over_limit = needs_escalation_over_limit(claimed_amount, policy)

    clarification_spec = None
    if not reconciliation.is_valid:
        clarification_spec = detect_ambiguity(
            extraction,
            rules,
            reconciliation,
            vendor_key=perception.vendor_key,
            category=category,
            tolerance=assessment.tolerance,
        )

    decision, reasons = _decide(
        duplicate=duplicate,
        violations=violations,
        reconciliation=reconciliation,
        perception_confidence=perception.confidence,
        over_limit=over_limit,
        policy=policy,
        claimed_amount=claimed_amount,
        clarification_spec=clarification_spec,
    )

    # Never ask forever: past the round budget, hand off to full manual review.
    if decision == AgentDecision.CLARIFY and round > MAX_CLARIFY_ROUNDS:
        decision = AgentDecision.ESCALATE
        reasons = [
            f"still ambiguous after {MAX_CLARIFY_ROUNDS} clarification rounds; "
            "needs manual review",
            *reasons,
        ]
        clarification_spec = None

    confidence = _confidence(perception.confidence, reconciliation.is_valid)
    calculated_total = reconciliation.calculated_total

    approved_amount: Optional[Decimal] = None
    if decision == AgentDecision.APPROVE:
        learn_vendor_policy(
            db,
            perception.vendor_key,
            rules,
            display_name=extraction.vendor,
            tolerance=tolerance_or_none(assessment.tolerance),
            category=category,
        )
        record_event(db, perception.vendor_key, approved=True)
        approved_amount = document.claimed_amount
        # Reinforce with a worked few-shot so future invoices from this vendor
        # extract cleanly on the first pass (Tier C of the guarantee).
        if perception.raw_text:
            add_few_shot(
                db,
                perception.vendor_key,
                example_text=perception.raw_text,
                example_fields=extraction.model_dump(mode="json"),
                source_document_id=document.id,
            )
    elif decision == AgentDecision.CLARIFY:
        record_event(db, perception.vendor_key, clarified=True)

    _apply_decision_to_document(
        document,
        extraction=extraction,
        rules=rules,
        calculated_total=calculated_total,
        approved_amount=approved_amount,
        decision=decision,
        reasons=reasons,
        raw_text=perception.raw_text,
    )

    clarifications: List[ClarificationResponse] = []
    if decision == AgentDecision.CLARIFY and clarification_spec is not None:
        clarification = generate_clarification(
            db,
            document_id=document.id,
            vendor_key=perception.vendor_key,
            spec=clarification_spec,
            round=round,
        )
        clarifications = [ClarificationResponse.model_validate(clarification)]

    result = AgentResult(
        decision=decision,
        confidence=confidence,
        reasons=reasons,
        remediations=assessment.remediations,
        steps=steps,
        extraction=extraction,
        validation_rules=rules,
        calculated_total=calculated_total,
        claimed_amount=claimed_amount,
        approved_amount=approved_amount,
        vendor_key=perception.vendor_key,
        duplicate_of=str(duplicate.document_id) if duplicate is not None else None,
        source="human",
        document_id=str(document.id),
        clarifications=clarifications,
    )
    _persist_run(db, document, result)
    return result


def resolve_clarification(
    db: Session,
    clarification_id: UUID,
    *,
    answer_option_id: str,
    answer_note: Optional[str] = None,
    confirmed_scope: Optional[str] = None,
    confirmed_scope_key: Optional[str] = None,
    learn: bool = True,
    category: Optional[str] = None,
) -> AgentResult:
    """Apply a human's answer: generalize it into a rule, then re-run the document.

    The answer becomes a durable, scoped :class:`~app.db.models.VendorRule` so the
    same question is never asked again, and the document is immediately re-processed
    with the new knowledge in effect.
    """
    clarification = get_clarification(db, clarification_id)
    if clarification is None:
        raise DocumentNotFoundError(f"Clarification {clarification_id} not found")
    if clarification.status != ClarificationStatus.OPEN:
        raise InvalidDocumentStateError(
            f"Clarification {clarification_id} has already been answered"
        )
    document = db.get(Document, clarification.document_id)
    if document is None:
        raise DocumentNotFoundError(
            f"Document {clarification.document_id} not found"
        )

    scope, scope_key = _resolve_scope(
        clarification, document, confirmed_scope, confirmed_scope_key
    )

    rule = None
    if learn:
        spec = rule_spec_from_answer(
            clarification,
            answer_option_id=answer_option_id,
            answer_note=answer_note,
            scope=scope,
            scope_key=scope_key,
        )
        if spec is not None:
            rule = create_rule(
                db,
                source="human",
                document_id=document.id,
                clarification_id=clarification.id,
                **spec,
            )

    clarification.status = ClarificationStatus.ANSWERED
    clarification.answer_option_id = answer_option_id
    clarification.answer_note = answer_note
    clarification.confirmed_scope = scope
    clarification.confirmed_scope_key = scope_key
    if rule is not None:
        clarification.resulting_rule_id = rule.id

    result = _rerun_document(
        db,
        document,
        round=clarification.round + 1,
        policy=ExpensePolicy.from_env(),
        category=category,
    )
    db.commit()
    db.refresh(document)
    return result


def list_clarification_queue(
    db: Session,
) -> List[tuple[Document, List]]:
    """Documents parked awaiting a human answer, each with its open clarification(s)."""
    grouped: dict = {}
    order: List = []
    for clarification in list_open_clarifications(db):
        if clarification.document_id not in grouped:
            grouped[clarification.document_id] = []
            order.append(clarification.document_id)
        grouped[clarification.document_id].append(clarification)

    items: List[tuple[Document, List]] = []
    for document_id in order:
        document = db.get(Document, document_id)
        if document is not None:
            items.append((document, grouped[document_id]))
    return items


def list_agent_queue(db: Session) -> List[tuple[Document, Optional[AgentRun]]]:
    """Documents the agent left for a human: escalated (pending) or rejected (failed).

    Only documents that have an agent run are included, so the manual-upload review
    queue and the agent queue stay distinct.
    """
    stmt = (
        select(Document)
        .where(Document.status.in_([DocumentStatus.PENDING, DocumentStatus.FAILED]))
        .order_by(Document.created_at.desc())
    )
    items: List[tuple[Document, Optional[AgentRun]]] = []
    for document in db.execute(stmt).scalars().all():
        run = db.execute(
            select(AgentRun)
            .where(AgentRun.document_id == document.id)
            .order_by(AgentRun.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if run is not None:
            items.append((document, run))
    return items
