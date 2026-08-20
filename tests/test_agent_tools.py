from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.schemas.agent import ExpensePolicy
from app.schemas.extraction import InvoiceExtraction
from app.services.agent_tools import (
    _vendor_few_shot,
    check_expense_policy,
    needs_escalation_over_limit,
    perceive,
    remediate,
)


def _extraction(**overrides) -> InvoiceExtraction:
    defaults = dict(
        vendor="Merry Berry",
        invoice_number="INV-9001",
        date="2026-04-11",
        line_items=[{"description": "Item", "amount": "150.00"}],
        subtotal="150.00",
        tax="10.68",
        fees="10.00",
        total="170.68",
    )
    defaults.update(overrides)
    return InvoiceExtraction(**defaults)


def test_remediate_finds_component_formula() -> None:
    extraction = _extraction()
    fix = remediate(extraction, Decimal("170.68"), Decimal("0.01"))

    assert fix is not None
    assert "subtotal" in fix.rules.total_components
    assert fix.result.is_valid


def test_remediate_returns_none_when_nothing_reconciles() -> None:
    extraction = _extraction()
    assert remediate(extraction, Decimal("999.00"), Decimal("0.01")) is None


def test_check_expense_policy_flags_max_amount() -> None:
    violations = check_expense_policy(
        _extraction(), Decimal("170.68"), ExpensePolicy(max_amount=Decimal("100"))
    )
    assert any("exceeds maximum" in v for v in violations)


def test_check_expense_policy_flags_category() -> None:
    violations = check_expense_policy(
        _extraction(),
        Decimal("170.68"),
        ExpensePolicy(allowed_categories=["travel"]),
        category="alcohol",
    )
    assert any("category" in v for v in violations)


def test_check_expense_policy_flags_tip_ratio() -> None:
    extraction = _extraction(tip="100.00")  # tip/subtotal = 0.66
    violations = check_expense_policy(
        extraction, Decimal("270.68"), ExpensePolicy(max_tip_ratio=Decimal("0.30"))
    )
    assert any("tip ratio" in v for v in violations)


def test_needs_escalation_over_limit() -> None:
    assert needs_escalation_over_limit(
        Decimal("500"), ExpensePolicy(auto_approve_limit=Decimal("100"))
    )
    assert not needs_escalation_over_limit(
        Decimal("50"), ExpensePolicy(auto_approve_limit=Decimal("100"))
    )


def test_perceive_falls_back_to_llm_when_document_ai_absent() -> None:
    extraction = _extraction()
    with (
        patch(
            "app.services.agent_tools.is_document_ai_configured", return_value=False
        ),
        patch(
            "app.services.agent_tools.read_pdf_pages",
            return_value=["invoice text with order@zomato.com"],
        ),
        patch(
            "app.services.agent_tools.split_invoice_segments",
            return_value=["invoice text"],
        ),
        patch(
            "app.services.agent_tools.extract_invoice_from_text",
            return_value=extraction,
        ),
        patch("app.services.agent_tools.lookup_vendor_policy", return_value=None),
    ):
        perception = perceive(MagicMock(), b"%PDF-1.4")

    assert perception.source == "llm"
    assert perception.segments == 1
    assert perception.extraction is extraction


def test_perceive_document_ai_splits_bundled_invoices() -> None:
    # A bundled PDF (two invoice-number markers) is sent to Document AI per invoice
    # and the per-segment results are summed, instead of parsing one jumbled doc.
    restaurant = _extraction(
        vendor="Zomato Restaurant",
        subtotal="150.00", tax="10.68", fees="10.00", total="170.68",
    )
    platform = _extraction(
        vendor="Zomato Platform",
        line_items=[{"description": "Platform fee", "amount": "5.00"}],
        subtotal="5.00", tax="0.90", fees="1.00", total="6.90",
    )
    docai_results = [
        (restaurant, {"total": 0.95}, 0.95, None),
        (platform, {"total": 0.40}, 0.40, "27AADCD4946L1Z5"),
    ]
    with (
        patch(
            "app.services.agent_tools.is_document_ai_configured", return_value=True
        ),
        patch(
            "app.services.agent_tools.read_pdf_pages",
            return_value=["Invoice No: A-1 food", "Invoice No: B-2 platform fee"],
        ),
        patch(
            "app.services.agent_tools.subset_pdf_pages",
            side_effect=[b"sub-1", b"sub-2"],
        ),
        patch(
            "app.services.agent_tools.extract_with_document_ai",
            side_effect=docai_results,
        ) as mock_docai,
    ):
        perception = perceive(MagicMock(), b"%PDF-1.4")

    assert perception.source == "document_ai"
    assert perception.segments == 2
    assert mock_docai.call_count == 2  # one call per bundled invoice
    assert perception.extraction.total == Decimal("177.58")  # 170.68 + 6.90
    assert perception.confidence == 0.40  # weakest segment gates the bundle


def test_perceive_document_ai_single_document_not_split() -> None:
    extraction = _extraction()
    with (
        patch(
            "app.services.agent_tools.is_document_ai_configured", return_value=True
        ),
        patch(
            "app.services.agent_tools.read_pdf_pages",
            return_value=["Invoice No: A-1 single invoice"],
        ),
        patch(
            "app.services.agent_tools.extract_with_document_ai",
            return_value=(extraction, {"total": 0.9}, 0.9, None),
        ) as mock_docai,
        patch("app.services.agent_tools.subset_pdf_pages") as mock_subset,
    ):
        perception = perceive(MagicMock(), b"%PDF-1.4")

    assert perception.source == "document_ai"
    assert perception.segments == 1
    mock_docai.assert_called_once()
    mock_subset.assert_not_called()  # no splitting for a single invoice


def test_vendor_few_shot_builds_example_from_policy() -> None:
    policy = SimpleNamespace(
        example_text="Invoice text for Zomato",
        example_fields={"vendor": "Zomato", "total": "170.68"},
    )
    with patch(
        "app.services.agent_tools.lookup_vendor_policy", return_value=policy
    ):
        example = _vendor_few_shot(MagicMock(), "pan:AADCD4946L")

    assert example is not None
    assert example.raw_text == "Invoice text for Zomato"
    assert example.expected_fields == {"vendor": "Zomato", "total": "170.68"}


def test_vendor_few_shot_none_without_example() -> None:
    policy = SimpleNamespace(example_text=None, example_fields=None)
    with patch(
        "app.services.agent_tools.lookup_vendor_policy", return_value=policy
    ):
        assert _vendor_few_shot(MagicMock(), "pan:AADCD4946L") is None
