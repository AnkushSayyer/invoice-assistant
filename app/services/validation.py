from dataclasses import dataclass
from decimal import Decimal
from typing import List, Optional

from app.schemas.extraction import InvoiceExtraction
from app.schemas.validation_rules import (
    DEFAULT_VALIDATION_RULES,
    TemplateValidationRules,
)

MATH_TOLERANCE = Decimal("0.01")

_COMPONENT_GETTERS = {
    "subtotal": lambda invoice: invoice.subtotal,
    "tax": lambda invoice: invoice.tax,
    "fees": lambda invoice: invoice.fees,
    "tip": lambda invoice: invoice.tip,
}


def _discounts_total(invoice: InvoiceExtraction) -> Decimal:
    return sum((discount.amount for discount in invoice.discounts), Decimal("0"))


def _format_total_formula(rules: TemplateValidationRules) -> str:
    formula = " + ".join(rules.total_components)
    if rules.subtract_discounts:
        formula = f"{formula} - discounts"
    return formula


@dataclass(frozen=True)
class ValidationResult:
    is_valid: bool
    errors: List[str]
    calculated_total: Decimal


def _within_tolerance(left: Decimal, right: Decimal) -> bool:
    return abs(left - right) <= MATH_TOLERANCE


def compute_calculated_total(
    invoice: InvoiceExtraction,
    rules: TemplateValidationRules = DEFAULT_VALIDATION_RULES,
) -> Decimal:
    """Compute the claimable total using the template's component formula."""
    total = Decimal("0")
    for component in rules.total_components:
        total += _COMPONENT_GETTERS[component](invoice)
    if rules.subtract_discounts:
        total -= _discounts_total(invoice)
    return total


def derive_validation_rules(invoice: InvoiceExtraction) -> TemplateValidationRules:
    """Infer validation rules from an approved invoice when none are provided."""
    total_components: List[str] = ["subtotal", "tax", "fees"]
    if invoice.tip > 0:
        total_components.append("tip")

    # Decide how (and whether) to validate line items by seeing which interpretation
    # of the line totals reconciles: net-of-tax (== subtotal) or tax-inclusive
    # (== subtotal + tax, common on GST/VAT invoices). If neither matches, the line
    # structure is ambiguous (e.g. separately taxed fees), so skip the line check and
    # rely on the component-based total formula instead.
    validate_line_items = False
    line_amount_includes_tax = False
    if invoice.line_items:
        line_items_total = sum(
            (item.amount for item in invoice.line_items), Decimal("0")
        )
        if _within_tolerance(line_items_total, invoice.subtotal):
            validate_line_items = True
        elif _within_tolerance(line_items_total, invoice.subtotal + invoice.tax):
            validate_line_items = True
            line_amount_includes_tax = True

    return TemplateValidationRules(
        validate_line_items=validate_line_items,
        total_components=total_components,  # type: ignore[arg-type]
        line_amount_includes_tax=line_amount_includes_tax,
    )


def load_template_validation_rules(
    rules_data: Optional[dict],
) -> TemplateValidationRules:
    if not rules_data:
        return DEFAULT_VALIDATION_RULES
    return TemplateValidationRules.model_validate(rules_data)


def validate_invoice_math(
    invoice: InvoiceExtraction,
    rules: TemplateValidationRules = DEFAULT_VALIDATION_RULES,
    *,
    claimed_amount: Optional[Decimal] = None,
    tolerance: Decimal = MATH_TOLERANCE,
) -> ValidationResult:
    """Verify invoice internals and that claimed amount matches the template formula.

    ``tolerance`` is the allowed rounding slack per comparison. When several invoices
    are bundled into one PDF and summed, each contributes its own 2-decimal rounding,
    so callers should widen the tolerance accordingly (e.g. one unit per invoice).
    """
    errors: List[str] = []

    def within(left: Decimal, right: Decimal) -> bool:
        return abs(left - right) <= tolerance

    line_items_total = sum((item.amount for item in invoice.line_items), Decimal("0"))
    discounts_total = _discounts_total(invoice)

    if rules.validate_line_items and invoice.line_items:
        if rules.line_amount_includes_tax:
            expected_line_total = invoice.subtotal + invoice.tax
            target_label = "subtotal + tax"
        else:
            expected_line_total = invoice.subtotal
            target_label = "subtotal"
        if not within(line_items_total, expected_line_total):
            errors.append(
                f"sum of line items does not match {target_label}: "
                f"expected {expected_line_total}, got {line_items_total}"
            )

    calculated_total = compute_calculated_total(invoice, rules)

    if not within(calculated_total, invoice.total):
        errors.append(
            "extracted total does not match template formula "
            f"({_format_total_formula(rules)}): "
            f"expected {calculated_total}, got {invoice.total}"
        )

    if claimed_amount is not None and not within(claimed_amount, calculated_total):
        errors.append(
            "claimed amount does not match calculated total: "
            f"expected {calculated_total}, got {claimed_amount}"
        )

    return ValidationResult(
        is_valid=len(errors) == 0,
        errors=errors,
        calculated_total=calculated_total,
    )
