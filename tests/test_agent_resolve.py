from decimal import Decimal
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.db.models import Document, RuleScope, RuleType
from app.schemas.agent import AgentDecision
from app.schemas.documents import DocumentStatus
from app.schemas.extraction import InvoiceExtraction
from app.schemas.validation_rules import TemplateValidationRules
from app.services.agent import (
    list_agent_queue,
    preview_resolution,
    resolve_document,
)
from app.services.document_processor import (
    DocumentNotFoundError,
    InvalidDocumentStateError,
)


def _extraction(total="170.68") -> InvoiceExtraction:
    return InvoiceExtraction(
        vendor="Merry Berry",
        invoice_number="INV-9001",
        date="2026-04-11",
        line_items=[{"description": "Item", "amount": "150.00"}],
        subtotal="150.00",
        tax="10.68",
        fees="10.00",
        total=total,
    )


def _document(status=DocumentStatus.PENDING) -> Document:
    return Document(
        id=uuid4(),
        filename="invoice.pdf",
        fingerprint="pan:AADCD4946L",
        vendor_key="pan:AADCD4946L",
        extracted_fields=_extraction().model_dump(mode="json"),
        claimed_amount=Decimal("170.68"),
        calculated_total=Decimal("170.68"),
        validation_rules={},
        invoice_segments=None,
        status=status,
    )


def test_resolve_approve_with_corrected_fields() -> None:
    db = MagicMock()
    document = _document()
    db.get.return_value = document
    with patch("app.services.agent.learn_vendor_policy") as mock_learn:
        result = resolve_document(
            db,
            document.id,
            decision="approve",
            approved_fields=_extraction(),
        )

    assert result.decision == AgentDecision.APPROVE
    assert result.source == "human"
    assert document.status == DocumentStatus.PROCESSED
    assert document.auto_approved is False
    # No explicit approved_amount -> defaults to the claimed amount.
    assert document.approved_amount == Decimal("170.68")
    assert result.approved_amount == Decimal("170.68")
    mock_learn.assert_called_once()
    db.commit.assert_called_once()


def test_resolve_learns_few_shot_example_from_correction() -> None:
    db = MagicMock()
    document = _document()
    document.raw_text = "Zomato invoice layout text"  # available worked example
    db.get.return_value = document
    corrected = _extraction()
    with patch("app.services.agent.learn_vendor_policy") as mock_learn:
        resolve_document(
            db,
            document.id,
            decision="approve",
            approved_fields=corrected,
        )

    mock_learn.assert_called_once()
    kwargs = mock_learn.call_args.kwargs
    # The reviewer's correction is stored as a reusable few-shot for this vendor.
    assert kwargs["example_text"] == "Zomato invoice layout text"
    assert kwargs["example_fields"] == corrected.model_dump(mode="json")


def test_resolve_approve_with_manual_approved_amount() -> None:
    db = MagicMock()
    document = _document()
    db.get.return_value = document
    with patch("app.services.agent.learn_vendor_policy"):
        result = resolve_document(
            db,
            document.id,
            decision="approve",
            approved_fields=_extraction(),
            approved_amount=Decimal("120.00"),  # reviewer approves less
            note="policy cap applied",
        )

    assert result.decision == AgentDecision.APPROVE
    assert document.approved_amount == Decimal("120.00")
    assert result.approved_amount == Decimal("120.00")


def test_resolve_manual_amount_overrides_math_mismatch_without_force() -> None:
    db = MagicMock()
    document = _document()
    db.get.return_value = document
    with patch("app.services.agent.learn_vendor_policy"):
        # Fields do not reconcile against the claim, but a manual approved amount
        # is authoritative, so approval proceeds without force.
        result = resolve_document(
            db,
            document.id,
            decision="approve",
            approved_fields=_extraction(total="999.00"),
            approved_amount=Decimal("261.03"),
        )

    assert result.decision == AgentDecision.APPROVE
    assert document.status == DocumentStatus.PROCESSED
    assert document.approved_amount == Decimal("261.03")
    assert any("overriding math discrepancy" in r for r in result.reasons)


def test_resolve_approve_rejects_unreconciled_without_force() -> None:
    db = MagicMock()
    document = _document()
    db.get.return_value = document
    with patch("app.services.agent.learn_vendor_policy"):
        with pytest.raises(InvalidDocumentStateError):
            resolve_document(
                db,
                document.id,
                decision="approve",
                approved_fields=_extraction(total="999.00"),  # will not reconcile
            )


def test_resolve_approve_can_force_past_mismatch() -> None:
    db = MagicMock()
    document = _document()
    db.get.return_value = document
    with patch("app.services.agent.learn_vendor_policy"):
        result = resolve_document(
            db,
            document.id,
            decision="approve",
            approved_fields=_extraction(total="999.00"),
            force=True,
            note="receipt verified manually",
        )

    assert result.decision == AgentDecision.APPROVE
    assert document.status == DocumentStatus.PROCESSED
    assert any("forced" in reason for reason in result.reasons)
    assert "receipt verified manually" in result.reasons


def test_resolve_reject_marks_failed() -> None:
    db = MagicMock()
    document = _document()
    db.get.return_value = document
    with patch("app.services.agent.learn_vendor_policy") as mock_learn:
        result = resolve_document(
            db,
            document.id,
            decision="reject",
            note="not a business expense",
        )

    assert result.decision == AgentDecision.REJECT
    assert document.status == DocumentStatus.FAILED
    assert document.error_message == "not a business expense"
    mock_learn.assert_not_called()


def test_resolve_missing_document_raises() -> None:
    db = MagicMock()
    db.get.return_value = None
    with pytest.raises(DocumentNotFoundError):
        resolve_document(db, uuid4(), decision="approve", approved_fields=_extraction())


def test_resolve_already_processed_raises() -> None:
    db = MagicMock()
    document = _document(status=DocumentStatus.PROCESSED)
    db.get.return_value = document
    with pytest.raises(InvalidDocumentStateError):
        resolve_document(db, document.id, decision="approve", approved_fields=_extraction())


def test_resolve_learns_scoped_validation_rule() -> None:
    db = MagicMock()
    document = _document()
    db.get.return_value = document
    rules = TemplateValidationRules(total_components=["subtotal", "tax", "fees"])
    with (
        patch("app.services.agent.learn_vendor_policy") as mock_policy,
        patch("app.services.agent.create_rule") as mock_create,
    ):
        result = resolve_document(
            db,
            document.id,
            decision="approve",
            approved_fields=_extraction(),
            validation_rules=rules,
            learn_scope="global",
        )

    assert result.decision == AgentDecision.APPROVE
    mock_create.assert_called_once()
    kwargs = mock_create.call_args.kwargs
    assert kwargs["scope"] == RuleScope.GLOBAL
    assert kwargs["scope_key"] == RuleScope.GLOBAL_KEY
    assert kwargs["rule_type"] == RuleType.VALIDATION
    assert kwargs["payload"] == rules.model_dump(mode="json")
    # A global lesson must not touch the vendor-only policy/few-shot store.
    mock_policy.assert_not_called()


def test_resolve_learns_capture_rule_at_vendor_scope() -> None:
    db = MagicMock()
    document = _document()
    db.get.return_value = document
    with (
        patch("app.services.agent.learn_vendor_policy") as mock_policy,
        patch("app.services.agent.create_rule") as mock_create,
    ):
        resolve_document(
            db,
            document.id,
            decision="approve",
            approved_fields=_extraction(),
            capture_anchor="Airport Surcharge",
            capture_target_field="fees",
        )

    mock_create.assert_called_once()
    kwargs = mock_create.call_args.kwargs
    assert kwargs["rule_type"] == RuleType.FIELD_MAPPING
    assert kwargs["capture_anchor"] == "Airport Surcharge"
    assert kwargs["target_field"] == "fees"
    assert kwargs["scope"] == RuleScope.VENDOR
    # Vendor scope also refreshes the few-shot policy store.
    mock_policy.assert_called_once()


def test_resolve_learns_directive_hint() -> None:
    db = MagicMock()
    document = _document()
    db.get.return_value = document
    with (
        patch("app.services.agent.learn_vendor_policy"),
        patch("app.services.agent.create_rule") as mock_create,
    ):
        resolve_document(
            db,
            document.id,
            decision="approve",
            approved_fields=_extraction(),
            directive="Include the footer service charge in fees.",
        )

    mock_create.assert_called_once()
    kwargs = mock_create.call_args.kwargs
    assert kwargs["rule_type"] == RuleType.HINT
    assert kwargs["directive"] == "Include the footer service charge in fees."


def test_resolve_learn_vendor_false_persists_nothing() -> None:
    db = MagicMock()
    document = _document()
    db.get.return_value = document
    with (
        patch("app.services.agent.learn_vendor_policy") as mock_policy,
        patch("app.services.agent.create_rule") as mock_create,
    ):
        resolve_document(
            db,
            document.id,
            decision="approve",
            approved_fields=_extraction(),
            validation_rules=TemplateValidationRules(
                total_components=["subtotal", "tax", "fees"]
            ),
            directive="ignored",
            learn_vendor=False,
        )

    mock_create.assert_not_called()
    mock_policy.assert_not_called()


def test_preview_resolution_reconciles_without_persisting() -> None:
    db = MagicMock()
    document = _document()
    db.get.return_value = document
    result = preview_resolution(
        db,
        document.id,
        approved_fields=_extraction(),
        validation_rules=TemplateValidationRules(
            total_components=["subtotal", "tax", "fees"]
        ),
    )
    assert result.is_valid is True
    assert result.calculated_total == Decimal("170.68")
    # Pure dry-run: nothing is written or committed.
    db.add.assert_not_called()
    db.commit.assert_not_called()


def test_preview_resolution_reports_mismatch() -> None:
    db = MagicMock()
    document = _document()
    db.get.return_value = document
    result = preview_resolution(
        db,
        document.id,
        approved_fields=_extraction(total="999.00"),
    )
    assert result.is_valid is False
    assert result.errors
    db.commit.assert_not_called()


def test_preview_capture_not_previewable_without_raw_text() -> None:
    db = MagicMock()
    document = _document()  # no stored raw_text
    db.get.return_value = document
    result = preview_resolution(
        db,
        document.id,
        approved_fields=_extraction(),
        capture_anchor="Airport Surcharge",
        capture_target_field="fees",
    )
    assert result.capture_previewable is False
    assert result.recovered_fields is None


def test_preview_capture_recovers_amount_from_raw_text() -> None:
    db = MagicMock()
    document = _document()
    document.raw_text = "Subtotal 150.00\nAirport Surcharge 25.00\nTotal 195.68"
    db.get.return_value = document
    result = preview_resolution(
        db,
        document.id,
        approved_fields=_extraction(),
        capture_anchor="Airport Surcharge",
        capture_target_field="fees",
    )
    assert result.capture_previewable is True
    assert result.recovered_fields is not None
    # fees bumped by the recovered 25.00 (10.00 + 25.00).
    assert result.recovered_fields.fees == Decimal("35.00")


def test_list_agent_queue_includes_only_agent_documents() -> None:
    db = MagicMock()
    document = _document()
    run = MagicMock()
    docs_result = MagicMock()
    docs_result.scalars.return_value.all.return_value = [document]
    run_result = MagicMock()
    run_result.scalar_one_or_none.return_value = run
    db.execute.side_effect = [docs_result, run_result]

    items = list_agent_queue(db)

    assert len(items) == 1
    assert items[0][0] is document
    assert items[0][1] is run
