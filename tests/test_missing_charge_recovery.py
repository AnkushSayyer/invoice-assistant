from decimal import Decimal
from types import SimpleNamespace

from app.schemas.extraction import InvoiceExtraction
from app.schemas.validation_rules import TemplateValidationRules
from app.services.agent_tools import (
    find_anchored_amount,
    recover_missing_charge,
    unaccounted_gap,
)


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


_RAW_TEXT = (
    "Trip fare\t500.00\n"
    "GST\t25.00\n"
    "Airport Surcharge\t40.00\n"
    "Total\t565.00"
)


def test_unaccounted_gap_detects_missing_charge() -> None:
    gap = unaccounted_gap(_extraction(), TemplateValidationRules())
    assert gap == Decimal("40.00")  # 565 total - (500 + 25 + 0)


def test_find_anchored_amount_reads_line_value() -> None:
    assert find_anchored_amount(_RAW_TEXT, "Airport Surcharge") == Decimal("40.00")


def test_find_anchored_amount_missing_anchor_returns_none() -> None:
    assert find_anchored_amount(_RAW_TEXT, "Toll") is None


def test_recover_missing_charge_reconciles_via_capture_rule() -> None:
    capture_rule = SimpleNamespace(
        capture_anchor="Airport Surcharge", target_field="fees"
    )
    recovery = recover_missing_charge(
        _extraction(),
        TemplateValidationRules(),
        _RAW_TEXT,
        claimed_amount=Decimal("565.00"),
        tolerance=Decimal("0.01"),
        capture_rules=[capture_rule],
    )

    assert recovery is not None
    assert recovery.result.is_valid
    assert recovery.extraction.fees == Decimal("40.00")  # surcharge captured
    assert "Airport Surcharge" in recovery.explanation


def test_recover_missing_charge_returns_none_without_capture_rules() -> None:
    assert (
        recover_missing_charge(
            _extraction(),
            TemplateValidationRules(),
            _RAW_TEXT,
            claimed_amount=Decimal("565.00"),
            tolerance=Decimal("0.01"),
            capture_rules=[],
        )
        is None
    )


def test_recover_missing_charge_none_when_anchor_absent_from_text() -> None:
    capture_rule = SimpleNamespace(
        capture_anchor="Airport Surcharge", target_field="fees"
    )
    recovery = recover_missing_charge(
        _extraction(),
        TemplateValidationRules(),
        "Trip fare\t500.00\nGST\t25.00\nTotal\t565.00",  # no surcharge line
        claimed_amount=Decimal("565.00"),
        tolerance=Decimal("0.01"),
        capture_rules=[capture_rule],
    )
    assert recovery is None
