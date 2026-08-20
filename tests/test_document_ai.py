from types import SimpleNamespace

import pytest

from app.services.document_ai import (
    DocumentAINotConfigured,
    extract_with_document_ai,
    is_document_ai_configured,
    map_document_to_extraction,
    overall_confidence,
)


def _entity(type_, mention_text, confidence=0.9, normalized=None, properties=None):
    return SimpleNamespace(
        type_=type_,
        mention_text=mention_text,
        confidence=confidence,
        normalized_value=normalized,
        properties=properties or [],
    )


def _fake_invoice_document():
    line_item = _entity(
        "line_item",
        "Mango 150",
        properties=[
            _entity("line_item/description", "Mini Alphonso Mango"),
            _entity("line_item/amount", "150.00"),
            _entity("line_item/quantity", "1"),
        ],
    )
    return SimpleNamespace(
        entities=[
            _entity("supplier_name", "Merry Berry"),
            _entity("invoice_id", "INV-9001"),
            _entity("invoice_date", "2026-04-11"),
            _entity("net_amount", "150.00"),
            _entity("total_tax_amount", "10.68"),
            _entity("freight_amount", "10.00"),
            _entity("total_amount", "170.68"),
            _entity("supplier_tax_id", "29AADCD4946L1Z6"),
            line_item,
        ]
    )


def test_map_document_to_extraction_maps_core_fields() -> None:
    extraction, confidences, vendor_tax_id = map_document_to_extraction(
        _fake_invoice_document()
    )

    assert extraction.vendor == "Merry Berry"
    assert extraction.invoice_number == "INV-9001"
    assert str(extraction.date) == "2026-04-11"
    assert str(extraction.subtotal) == "150.00"
    assert str(extraction.tax) == "10.68"
    assert str(extraction.fees) == "10.00"
    assert str(extraction.total) == "170.68"
    assert len(extraction.line_items) == 1
    assert extraction.line_items[0].description == "Mini Alphonso Mango"
    assert vendor_tax_id == "29AADCD4946L1Z6"
    assert "total" in confidences


def test_map_document_infers_total_when_missing() -> None:
    document = SimpleNamespace(
        entities=[
            _entity("net_amount", "100.00"),
            _entity("total_tax_amount", "18.00"),
        ]
    )
    extraction, _confidences, _tax_id = map_document_to_extraction(document)

    # total omitted -> subtotal + tax + fees
    assert str(extraction.total) == "118.00"


def test_overall_confidence_is_weakest_critical_field() -> None:
    assert overall_confidence({"total": 0.9, "subtotal": 0.6, "tax": 0.95}) == 0.6
    assert overall_confidence({}) == 0.0


def test_is_document_ai_configured_false_without_env(monkeypatch) -> None:
    for name in ("DOCAI_PROJECT_ID", "DOCAI_LOCATION", "DOCAI_PROCESSOR_ID"):
        monkeypatch.delenv(name, raising=False)
    assert is_document_ai_configured() is False


def test_extract_with_document_ai_raises_when_not_configured(monkeypatch) -> None:
    for name in ("DOCAI_PROJECT_ID", "DOCAI_LOCATION", "DOCAI_PROCESSOR_ID"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(DocumentAINotConfigured):
        extract_with_document_ai(b"%PDF-1.4")
