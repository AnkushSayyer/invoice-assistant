from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from app.db.models import Document, Template
from app.schemas.extraction import InvoiceExtraction
from app.schemas.validation_rules import (
    FOOD_DELIVERY_VALIDATION_RULES,
    UBER_RIDE_VALIDATION_RULES,
)
from app.services.document_processor import approve_document, process_upload
from app.services.validation import ValidationResult, derive_validation_rules


def test_process_upload_uses_template_validation_rules() -> None:
    template = Template(
        name="Uber",
        vendor_fingerprint="uber fingerprint",
        validation_rules=UBER_RIDE_VALIDATION_RULES.model_dump(mode="json"),
    )
    extraction = InvoiceExtraction(
        vendor="Uber",
        invoice_number="TRIP-1",
        date="2024-01-15",
        line_items=[{"description": "Trip fare", "amount": "20.00"}],
        subtotal="20.00",
        tip="4.00",
        total="24.00",
    )
    validation = ValidationResult(
        is_valid=True,
        errors=[],
        calculated_total=Decimal("24.00"),
    )
    template_match = SimpleNamespace(template=template, similarity_score=0.95)

    with (
        patch(
            "app.services.document_processor.read_pdf_pages",
            return_value=["uber receipt"],
        ),
        patch(
            "app.services.document_processor.generate_signature",
            return_value="uber fingerprint",
        ),
        patch("app.services.document_processor.mask_invoice_text", return_value="masked"),
        patch(
            "app.services.document_processor.find_matching_template",
            return_value=template_match,
        ),
        patch("app.services.document_processor.get_template_example", return_value=None),
        patch(
            "app.services.document_processor.extract_invoice_from_text",
            return_value=extraction,
        ),
        patch(
            "app.services.document_processor.validate_invoice_math",
            return_value=validation,
        ) as mock_validate,
    ):
        db = MagicMock()
        document, _, match, rules, auto_approved = process_upload(
            db,
            b"%PDF-1.4",
            "uber.pdf",
            Decimal("24.00"),
        )

    mock_validate.assert_called_once()
    assert mock_validate.call_args.kwargs["claimed_amount"] == Decimal("24.00")
    assert rules.total_components == ["subtotal", "tip"]
    assert match is template_match
    assert document.claimed_amount == Decimal("24.00")
    assert document.calculated_total == Decimal("24.00")
    assert document.validation_rules == UBER_RIDE_VALIDATION_RULES.model_dump(mode="json")
    assert document.has_pdf is True
    assert document.pdf_data == b"%PDF-1.4"
    assert document.status == "processed"
    assert document.auto_approved is True
    assert document.matched_template_id == template.id
    assert auto_approved is True


def test_process_upload_does_not_auto_approve_below_threshold() -> None:
    template = Template(
        name="Uber",
        vendor_fingerprint="uber fingerprint",
        validation_rules=UBER_RIDE_VALIDATION_RULES.model_dump(mode="json"),
    )
    extraction = InvoiceExtraction(
        vendor="Uber",
        invoice_number="TRIP-1",
        date="2024-01-15",
        line_items=[{"description": "Trip fare", "amount": "20.00"}],
        subtotal="20.00",
        tip="4.00",
        total="24.00",
    )
    validation = ValidationResult(
        is_valid=True,
        errors=[],
        calculated_total=Decimal("24.00"),
    )
    template_match = SimpleNamespace(template=template, similarity_score=0.88)

    with (
        patch(
            "app.services.document_processor.read_pdf_pages",
            return_value=["uber receipt"],
        ),
        patch(
            "app.services.document_processor.generate_signature",
            return_value="uber fingerprint",
        ),
        patch("app.services.document_processor.mask_invoice_text", return_value="masked"),
        patch(
            "app.services.document_processor.find_matching_template",
            return_value=template_match,
        ),
        patch("app.services.document_processor.get_template_example", return_value=None),
        patch(
            "app.services.document_processor.extract_invoice_from_text",
            return_value=extraction,
        ),
        patch(
            "app.services.document_processor.validate_invoice_math",
            return_value=validation,
        ),
    ):
        document, _, _, _, auto_approved = process_upload(
            MagicMock(),
            b"%PDF-1.4",
            "uber.pdf",
            Decimal("24.00"),
        )

    assert document.status == "pending"
    assert document.auto_approved is False
    assert document.matched_template_id is None
    assert auto_approved is False


def test_approve_uses_validation_rules_from_document_when_not_provided() -> None:
    document_id = uuid4()
    document = Document(
        id=document_id,
        filename="zomato.pdf",
        fingerprint="zomato fingerprint",
        raw_text="receipt text",
        masked_text="masked",
        claimed_amount=Decimal("261.03"),
        validation_rules=FOOD_DELIVERY_VALIDATION_RULES.model_dump(mode="json"),
        status="pending",
    )
    approved_fields = InvoiceExtraction(
        vendor="Zomato",
        invoice_number="ORDER-1",
        date="2024-01-15",
        line_items=[{"description": "Biryani", "amount": "200.00"}],
        discounts=[{"description": "Gold", "amount": "5.00"}],
        subtotal="200.00",
        tax="10.00",
        fees="51.03",
        tip="0.00",
        total="261.03",
    )

    db = MagicMock()
    db.get.return_value = document

    with patch(
        "app.services.document_processor.validate_invoice_math",
        return_value=ValidationResult(
            is_valid=True,
            errors=[],
            calculated_total=Decimal("261.03"),
        ),
    ) as mock_validate:
        approve_document(
            db,
            document_id,
            "Zomato Food",
            approved_fields,
            None,
        )

    assert mock_validate.call_args.args[1] == FOOD_DELIVERY_VALIDATION_RULES


def test_approve_persists_validation_rules() -> None:
    document_id = uuid4()
    document = Document(
        id=document_id,
        filename="uber.pdf",
        fingerprint="uber fingerprint",
        raw_text="trip text",
        masked_text="masked",
        claimed_amount=Decimal("24.00"),
        status="pending",
    )
    approved_fields = InvoiceExtraction(
        vendor="Uber",
        invoice_number="TRIP-1",
        date="2024-01-15",
        line_items=[{"description": "Trip fare", "amount": "20.00"}],
        subtotal="20.00",
        tip="4.00",
        total="24.00",
    )

    db = MagicMock()
    db.get.return_value = document

    _, template, _, rules = approve_document(
        db,
        document_id,
        "Uber Rides",
        approved_fields,
        UBER_RIDE_VALIDATION_RULES,
    )

    assert template.validation_rules == UBER_RIDE_VALIDATION_RULES.model_dump(mode="json")
    assert rules.total_components == ["subtotal", "tip"]
    assert document.status == "processed"
    assert document.calculated_total == Decimal("24.00")
    assert document.validation_rules == UBER_RIDE_VALIDATION_RULES.model_dump(mode="json")


def test_process_upload_without_template_derives_validation_rules() -> None:
    extraction = InvoiceExtraction(
        vendor="Zomato",
        invoice_number="ORDER-1",
        date="2024-01-15",
        line_items=[{"description": "Biryani", "amount": "200.00"}],
        subtotal="200.00",
        tax="10.00",
        fees="51.03",
        tip="5.00",
        total="266.03",
    )
    validation = ValidationResult(
        is_valid=False,
        errors=["claimed amount does not match calculated total"],
        calculated_total=Decimal("266.03"),
    )

    with (
        patch(
            "app.services.document_processor.read_pdf_pages",
            return_value=["zomato receipt"],
        ),
        patch(
            "app.services.document_processor.generate_signature",
            return_value="zomato fingerprint",
        ),
        patch("app.services.document_processor.mask_invoice_text", return_value="masked"),
        patch("app.services.document_processor.find_matching_template", return_value=None),
        patch("app.services.document_processor.get_template_example", return_value=None),
        patch(
            "app.services.document_processor.extract_invoice_from_text",
            return_value=extraction,
        ),
        patch(
            "app.services.document_processor.validate_invoice_math",
            return_value=validation,
        ),
    ):
        document, result, match, rules, auto_approved = process_upload(
            MagicMock(),
            b"%PDF-1.4",
            "zomato.pdf",
            Decimal("250.00"),
        )

    assert match is None
    assert result.is_valid is False
    assert rules == derive_validation_rules(extraction)
    assert document.error_message is not None
    assert auto_approved is False


def test_process_upload_combines_multiple_invoices_in_one_pdf() -> None:
    # A GST PDF bundling a restaurant invoice and a separate platform-fee invoice.
    pages = [
        "Tax Invoice\nAbooz Cafe\nInvoice No.: INV-A\nTotal 241.45",
        "Tax Invoice\nETERNAL LIMITED\nInvoice No: INV-B\nPlatform fee\nTotal 17.58",
    ]
    food = InvoiceExtraction(
        vendor="Abooz Cafe",
        invoice_number="INV-A",
        date="2026-06-07",
        line_items=[{"description": "Parotta", "amount": "168"}],
        subtotal="219.00",
        tax="11.50",
        fees="10.95",
        total="241.45",
    )
    platform = InvoiceExtraction(
        vendor="Eternal",
        invoice_number="INV-B",
        date="2026-06-07",
        subtotal="14.90",
        tax="2.68",
        fees="0",
        total="17.58",
    )

    with (
        patch(
            "app.services.document_processor.read_pdf_pages",
            return_value=pages,
        ),
        patch(
            "app.services.document_processor.generate_signature",
            return_value="gst fingerprint",
        ),
        patch("app.services.document_processor.mask_invoice_text", return_value="masked"),
        patch("app.services.document_processor.find_matching_template", return_value=None),
        patch("app.services.document_processor.get_template_example", return_value=None),
        patch(
            "app.services.document_processor.extract_invoice_from_text",
            side_effect=[food, platform],
        ) as mock_extract,
    ):
        document, result, match, rules, auto_approved = process_upload(
            MagicMock(),
            b"%PDF-1.4",
            "gst.pdf",
            Decimal("259.03"),
        )

    # Both invoices extracted independently, then reconciled as one document.
    assert mock_extract.call_count == 2
    assert match is None
    assert result.is_valid is True
    assert document.calculated_total == Decimal("259.03")
    assert document.extracted_fields["total"] == "259.03"
    assert document.extracted_fields["invoice_number"] == "INV-A, INV-B"
    assert auto_approved is False
    # Per-segment fingerprinting records one entry per invoice.
    assert len(document.invoice_segments) == 2


def test_process_upload_records_per_segment_fingerprints_and_vendor_key() -> None:
    pages = [
        "Tax Invoice\nETERNAL LIMITED\nEmail ID: order@zomato.com\n"
        "Invoice No: INV-1\nPlatform fee 14.90\nTotal 17.58",
    ]
    extraction = InvoiceExtraction(
        vendor="Eternal",
        invoice_number="INV-1",
        date="2026-06-07",
        subtotal="14.90",
        tax="2.68",
        total="17.58",
    )

    with (
        patch(
            "app.services.document_processor.read_pdf_pages",
            return_value=pages,
        ),
        patch("app.services.document_processor.mask_invoice_text", return_value="masked"),
        patch("app.services.document_processor.find_matching_template", return_value=None),
        patch("app.services.document_processor.get_template_example", return_value=None),
        patch(
            "app.services.document_processor.extract_invoice_from_text",
            return_value=extraction,
        ),
    ):
        document, _, _, _, _ = process_upload(
            MagicMock(),
            b"%PDF-1.4",
            "platform.pdf",
            Decimal("17.58"),
        )

    # The vendor key is derived from the real fingerprint helper (email domain).
    assert document.vendor_key == "zomato.com"
    assert len(document.invoice_segments) == 1
    assert document.invoice_segments[0]["vendor_key"] == "zomato.com"
    assert document.invoice_segments[0]["matched_template_id"] is None
    assert document.fingerprint  # non-empty masked signature


def test_approve_updates_formula_from_payload_over_document_rules() -> None:
    document_id = uuid4()
    document = Document(
        id=document_id,
        filename="zomato.pdf",
        fingerprint="zomato fingerprint",
        raw_text="receipt text",
        masked_text="masked",
        claimed_amount=Decimal("261.03"),
        validation_rules={"validate_line_items": True, "total_components": ["subtotal"]},
        status="pending",
    )
    approved_fields = InvoiceExtraction(
        vendor="Zomato",
        invoice_number="ORDER-1",
        date="2024-01-15",
        line_items=[{"description": "Biryani", "amount": "200.00"}],
        discounts=[{"description": "Gold", "amount": "5.00"}],
        subtotal="200.00",
        tax="10.00",
        fees="51.03",
        tip="0.00",
        total="261.03",
    )

    db = MagicMock()
    db.get.return_value = document

    with patch(
        "app.services.document_processor.validate_invoice_math",
        return_value=ValidationResult(
            is_valid=True,
            errors=[],
            calculated_total=Decimal("261.03"),
        ),
    ) as mock_validate:
        _, template, _, rules = approve_document(
            db,
            document_id,
            "Zomato Food",
            approved_fields,
            FOOD_DELIVERY_VALIDATION_RULES,
        )

    assert mock_validate.call_args.args[1] == FOOD_DELIVERY_VALIDATION_RULES
    assert template.validation_rules == FOOD_DELIVERY_VALIDATION_RULES.model_dump(mode="json")
    assert document.validation_rules == FOOD_DELIVERY_VALIDATION_RULES.model_dump(mode="json")
    assert rules == FOOD_DELIVERY_VALIDATION_RULES


def test_matched_template_reuses_saved_formula_on_next_upload() -> None:
    saved_rules = FOOD_DELIVERY_VALIDATION_RULES.model_dump(mode="json")
    template = Template(
        name="Zomato Food",
        vendor_fingerprint="zomato fingerprint",
        validation_rules=saved_rules,
    )
    extraction = InvoiceExtraction(
        vendor="Zomato",
        invoice_number="ORDER-2",
        date="2024-02-01",
        line_items=[{"description": "Paneer", "amount": "180.00"}],
        subtotal="180.00",
        tax="9.00",
        fees="40.00",
        total="229.00",
    )
    template_match = SimpleNamespace(template=template, similarity_score=0.96)

    with (
        patch(
            "app.services.document_processor.read_pdf_pages",
            return_value=["zomato receipt"],
        ),
        patch(
            "app.services.document_processor.generate_signature",
            return_value="zomato fingerprint",
        ),
        patch("app.services.document_processor.mask_invoice_text", return_value="masked"),
        patch(
            "app.services.document_processor.find_matching_template",
            return_value=template_match,
        ),
        patch("app.services.document_processor.get_template_example", return_value=None),
        patch(
            "app.services.document_processor.extract_invoice_from_text",
            return_value=extraction,
        ),
        patch(
            "app.services.document_processor.validate_invoice_math",
            return_value=ValidationResult(
                is_valid=True,
                errors=[],
                calculated_total=Decimal("229.00"),
            ),
        ) as mock_validate,
    ):
        _, _, match, rules, auto_approved = process_upload(
            MagicMock(),
            b"%PDF-1.4",
            "zomato-2.pdf",
            Decimal("229.00"),
        )

    assert match is template_match
    assert rules == FOOD_DELIVERY_VALIDATION_RULES
    assert mock_validate.call_args.args[1] == FOOD_DELIVERY_VALIDATION_RULES
    assert auto_approved is True
