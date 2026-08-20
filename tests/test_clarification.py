from decimal import Decimal
from types import SimpleNamespace

from app.db.models import RuleScope, RuleType
from app.schemas.extraction import InvoiceExtraction
from app.schemas.validation_rules import TemplateValidationRules
from app.services.clarification import (
    AMB_LINE_TAX_INCLUSIVE,
    AMB_MISSING_CHARGE,
    detect_ambiguity,
    rule_spec_from_answer,
)
from app.services.validation import validate_invoice_math


def _extraction(**overrides) -> InvoiceExtraction:
    defaults = dict(
        vendor="Uber",
        invoice_number="TRIP-1",
        date="2026-04-11",
        subtotal="500.00",
        tax="25.00",
        fees="0.00",
        total="565.00",
    )
    defaults.update(overrides)
    return InvoiceExtraction(**defaults)


def test_detect_missing_charge_ambiguity() -> None:
    extraction = _extraction()
    rules = TemplateValidationRules()
    reconciliation = validate_invoice_math(
        extraction, rules, claimed_amount=Decimal("565.00")
    )
    assert not reconciliation.is_valid

    spec = detect_ambiguity(
        extraction, rules, reconciliation, vendor_key="uber.com"
    )
    assert spec is not None
    assert spec.ambiguity_type == AMB_MISSING_CHARGE
    assert spec.proposed_scope == RuleScope.VENDOR
    assert spec.proposed_scope_key == "uber.com"
    assert spec.evidence["gap"] == "40.00"
    assert {opt["id"] for opt in spec.options} >= {"fee", "tax", "tip"}


def test_claim_only_mismatch_is_not_asked() -> None:
    # Internal math reconciles; only the claimed amount differs -> not a vendor quirk.
    extraction = _extraction(fees="40.00")  # 500 + 25 + 40 = 565 == total
    rules = TemplateValidationRules()
    reconciliation = validate_invoice_math(
        extraction, rules, claimed_amount=Decimal("999.00")
    )
    assert not reconciliation.is_valid
    assert detect_ambiguity(extraction, rules, reconciliation, vendor_key="uber.com") is None


def test_detect_line_tax_inclusive_ambiguity() -> None:
    extraction = _extraction(
        subtotal="100.00",
        tax="18.00",
        fees="0.00",
        total="118.00",
        line_items=[{"description": "Item", "amount": "118.00"}],  # tax-inclusive
    )
    rules = TemplateValidationRules(validate_line_items=True)
    reconciliation = validate_invoice_math(
        extraction, rules, claimed_amount=Decimal("118.00")
    )
    assert not reconciliation.is_valid

    spec = detect_ambiguity(extraction, rules, reconciliation, vendor_key="acme.com")
    assert spec is not None
    assert spec.ambiguity_type == AMB_LINE_TAX_INCLUSIVE


def test_valid_reconciliation_has_no_ambiguity() -> None:
    extraction = _extraction(fees="40.00")
    rules = TemplateValidationRules()
    reconciliation = validate_invoice_math(
        extraction, rules, claimed_amount=Decimal("565.00")
    )
    assert reconciliation.is_valid
    assert detect_ambiguity(extraction, rules, reconciliation) is None


def _clarification(ambiguity_type: str, **evidence) -> SimpleNamespace:
    return SimpleNamespace(ambiguity_type=ambiguity_type, evidence=evidence)


def test_rule_spec_missing_charge_with_note_builds_capture_rule() -> None:
    clarification = _clarification(AMB_MISSING_CHARGE)
    spec = rule_spec_from_answer(
        clarification,
        answer_option_id="fee",
        answer_note="Airport Surcharge",
        scope=RuleScope.VENDOR,
        scope_key="uber.com",
    )
    assert spec is not None
    assert spec["rule_type"] == RuleType.FIELD_MAPPING
    assert spec["capture_anchor"] == "Airport Surcharge"
    assert spec["target_field"] == "fees"
    assert spec["scope_key"] == "uber.com"


def test_rule_spec_missing_charge_without_note_builds_hint() -> None:
    clarification = _clarification(AMB_MISSING_CHARGE)
    spec = rule_spec_from_answer(
        clarification,
        answer_option_id="tax",
        answer_note=None,
        scope=RuleScope.VENDOR,
        scope_key="uber.com",
    )
    assert spec is not None
    assert spec["rule_type"] == RuleType.HINT
    assert spec["capture_anchor"] is None
    assert spec["target_field"] == "tax"


def test_rule_spec_not_sure_without_note_learns_nothing() -> None:
    clarification = _clarification(AMB_MISSING_CHARGE)
    spec = rule_spec_from_answer(
        clarification,
        answer_option_id="other",
        answer_note=None,
        scope=RuleScope.VENDOR,
        scope_key="uber.com",
    )
    assert spec is None


def test_rule_spec_line_tax_inclusive_builds_validation_rule() -> None:
    rules = TemplateValidationRules(validate_line_items=True)
    clarification = _clarification(
        AMB_LINE_TAX_INCLUSIVE, rules=rules.model_dump(mode="json")
    )
    spec = rule_spec_from_answer(
        clarification,
        answer_option_id="yes",
        answer_note=None,
        scope=RuleScope.CATEGORY,
        scope_key="restaurant",
    )
    assert spec is not None
    assert spec["rule_type"] == RuleType.VALIDATION
    assert spec["payload"]["line_amount_includes_tax"] is True
    assert spec["scope"] == RuleScope.CATEGORY
