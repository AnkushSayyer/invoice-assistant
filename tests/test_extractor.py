import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import fitz
import pytest

from decimal import Decimal

from app.schemas.extraction import InvoiceExtraction
from app.services.extractor import (
    ExtractionTimeoutError,
    PDFReadError,
    _build_messages,
    _cluster_words_into_rows,
    _row_to_text,
    combine_invoices,
    extract_invoice_from_text,
    read_pdf_pages,
    read_pdf_text,
    split_invoice_page_groups,
    split_invoice_segments,
    subset_pdf_pages,
)
from app.services.llm import ExtractionLLMError, MissingLLMApiKeyError


def _word(x0: float, y0: float, x1: float, y1: float, text: str):
    """Build a PyMuPDF-style word tuple for the layout helpers."""
    return (x0, y0, x1, y1, text, 0, 0, 0)


def test_read_pdf_text_raises_on_invalid_bytes() -> None:
    with pytest.raises(PDFReadError):
        read_pdf_text(b"not-a-pdf")


def test_cluster_words_into_rows_groups_by_vertical_position() -> None:
    words = [
        # Row 1 (y ~ 10), given out of reading order to prove sorting.
        _word(50, 10, 70, 20, "168.00"),
        _word(10, 11, 30, 21, "Parotta"),
        # Row 2 (y ~ 40).
        _word(10, 40, 30, 50, "Juice"),
        _word(50, 41, 70, 51, "51.00"),
    ]

    rows = _cluster_words_into_rows(words, y_tolerance=6.0)

    assert len(rows) == 2
    assert [w[4] for w in rows[0]] == ["Parotta", "168.00"]
    assert [w[4] for w in rows[1]] == ["Juice", "51.00"]


def test_row_to_text_inserts_tab_for_column_gaps() -> None:
    row = [
        _word(0, 0, 40, 10, "Parotta"),  # label
        _word(200, 0, 230, 10, "168.00"),  # far-away column
    ]

    # Space width ~5 -> gap of 160 pts is a column break -> tab separator.
    rendered = _row_to_text(row, space_width=5.0)

    assert rendered == "Parotta\t168.00"


def test_row_to_text_uses_space_for_adjacent_words() -> None:
    row = [
        _word(0, 0, 20, 10, "Egg"),
        _word(24, 0, 60, 10, "Kothu"),
    ]

    rendered = _row_to_text(row, space_width=5.0)

    assert rendered == "Egg Kothu"


def test_read_pdf_text_preserves_tabular_rows() -> None:
    doc = fitz.open()
    page = doc.new_page()
    # Two columns per row: a label on the left, an amount on the right.
    page.insert_text((72, 100), "Egg Kothu Parotta")
    page.insert_text((400, 100), "168.00")
    page.insert_text((72, 130), "Lime Juice")
    page.insert_text((400, 130), "51.00")
    pdf_bytes = doc.tobytes()
    doc.close()

    text = read_pdf_text(pdf_bytes)
    lines = text.splitlines()

    assert any(
        "Egg Kothu Parotta" in line and "168.00" in line for line in lines
    ), text
    assert any("Lime Juice" in line and "51.00" in line for line in lines), text


def test_build_messages_without_example() -> None:
    messages = _build_messages("Invoice total $10.00")

    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    assert "Invoice total $10.00" in messages[1]["content"]


def test_build_messages_injects_few_shot_example() -> None:
    example = SimpleNamespace(
        raw_text="Example vendor invoice body",
        masked_text="masked fallback",
        expected_fields={
            "vendor": "Acme Corp",
            "invoice_number": "INV-1",
            "date": "2024-01-15",
            "line_items": [],
            "discounts": [],
            "subtotal": "100.00",
            "tax": "8.00",
            "fees": "2.00",
            "tip": "0.00",
            "total": "110.00",
        },
    )

    messages = _build_messages("New invoice body", template_example=example)

    assert len(messages) == 4
    assert "Example vendor invoice body" in messages[1]["content"]
    assert messages[2]["role"] == "assistant"
    assert json.loads(messages[2]["content"]) == example.expected_fields
    assert "New invoice body" in messages[3]["content"]


def test_build_messages_uses_model_role_for_gemini_few_shot() -> None:
    example = SimpleNamespace(
        raw_text="Example vendor invoice body",
        masked_text="masked fallback",
        expected_fields={"vendor": "Acme Corp"},
    )

    messages = _build_messages(
        "New invoice body",
        template_example=example,
        provider="gemini",
    )

    assert messages[2]["role"] == "model"


def test_build_messages_falls_back_to_masked_text() -> None:
    example = SimpleNamespace(
        raw_text=None,
        masked_text="masked example text",
        expected_fields={"vendor": "Acme Corp"},
    )

    messages = _build_messages("Invoice body", template_example=example)

    assert "masked example text" in messages[1]["content"]


def test_split_invoice_segments_splits_multiple_invoices() -> None:
    pages = [
        "Tax Invoice\nRestaurant Name: Abooz Cafe\nInvoice No.: 26LXV6R8\nTotal 241.45",
        "Tax Invoice\nETERNAL LIMITED\nInvoice No: Z27KAOT\nPlatform fee 14.90\nTotal 17.58",
    ]

    segments = split_invoice_segments(pages)

    assert len(segments) == 2
    assert "Abooz Cafe" in segments[0]
    assert "ETERNAL LIMITED" in segments[1]


def test_split_invoice_segments_keeps_single_receipt_intact() -> None:
    pages = [
        "Zomato Food Order: Summary and Receipt\n"
        "Order ID: 8364541366\nTotal 261.03",
    ]

    segments = split_invoice_segments(pages)

    assert len(segments) == 1
    assert "261.03" in segments[0]


def test_split_invoice_segments_treats_continuation_page_as_same_invoice() -> None:
    # Second page has no invoice-number marker, so it belongs to the first invoice.
    pages = [
        "Tax Invoice\nInvoice No.: INV-1\nSubtotal 100",
        "Terms and conditions continued...",
    ]

    segments = split_invoice_segments(pages)

    assert len(segments) == 1
    assert "continued" in segments[0]


def test_combine_invoices_returns_single_invoice_unchanged() -> None:
    invoice = InvoiceExtraction(
        vendor="Solo",
        invoice_number="INV-1",
        date="2026-06-07",
        subtotal="10.00",
        total="10.00",
    )

    assert combine_invoices([invoice]) is invoice


def test_combine_invoices_sums_components_and_concatenates() -> None:
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
        total="17.58",
    )

    combined = combine_invoices([food, platform])

    assert combined.vendor == "Abooz Cafe + Eternal"
    assert combined.invoice_number == "INV-A, INV-B"
    assert combined.subtotal == Decimal("233.90")
    assert combined.tax == Decimal("14.18")
    assert combined.fees == Decimal("10.95")
    assert combined.total == Decimal("259.03")
    assert len(combined.line_items) == 1


@patch("app.services.extractor.create_instructor_client")
def test_extract_invoice_from_text_returns_structured_result(
    mock_create_client: MagicMock,
) -> None:
    expected = InvoiceExtraction(
        vendor="Acme Corp",
        invoice_number="INV-1001",
        date="2024-01-15",
        line_items=[],
        discounts=[],
        subtotal="100.00",
        tax="8.00",
        fees="2.00",
        tip="0.00",
        total="110.00",
    )
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = expected
    mock_create_client.return_value = mock_client

    result = extract_invoice_from_text("Invoice text")

    assert result == expected
    mock_client.chat.completions.create.assert_called_once()


@patch("app.services.extractor.create_instructor_client")
def test_extract_invoice_from_text_raises_on_timeout(
    mock_create_client: MagicMock,
) -> None:
    from openai import APITimeoutError

    mock_client = MagicMock()
    mock_client.chat.completions.create.side_effect = APITimeoutError("timed out")
    mock_create_client.return_value = mock_client

    with pytest.raises(ExtractionTimeoutError):
        extract_invoice_from_text("Invoice text")


@patch("app.services.extractor.create_instructor_client")
def test_extract_invoice_from_text_raises_llm_error(
    mock_create_client: MagicMock,
) -> None:
    mock_client = MagicMock()
    mock_client.chat.completions.create.side_effect = RuntimeError("provider failed")
    mock_create_client.return_value = mock_client

    with pytest.raises(ExtractionLLMError):
        extract_invoice_from_text("Invoice text")


def test_split_invoice_page_groups_single_invoice() -> None:
    pages = ["Invoice No: A-1\nItems...", "continued totals..."]
    assert split_invoice_page_groups(pages) == [[0, 1]]


def test_split_invoice_page_groups_splits_bundle() -> None:
    pages = [
        "Invoice No: A-1\nRestaurant charges",
        "Invoice Number B-2\nPlatform fee",
    ]
    assert split_invoice_page_groups(pages) == [[0], [1]]


def test_split_invoice_page_groups_keeps_blank_pages_with_current() -> None:
    # A blank (e.g. scanned/image) page must not be dropped before OCR.
    pages = ["Invoice No: A-1", "", "Invoice No: B-2"]
    assert split_invoice_page_groups(pages) == [[0, 1], [2]]


def test_split_invoice_page_groups_empty_document() -> None:
    assert split_invoice_page_groups([]) == [[]]


def _make_pdf(page_texts: list[str]) -> bytes:
    doc = fitz.open()
    for text in page_texts:
        page = doc.new_page()
        page.insert_text((72, 72), text)
    data = doc.tobytes()
    doc.close()
    return data


def test_subset_pdf_pages_selects_requested_pages() -> None:
    pdf = _make_pdf(["Invoice No: A-1 alpha", "middle beta", "Invoice No: C-3 gamma"])

    subset = subset_pdf_pages(pdf, [0, 2])

    pages = read_pdf_pages(subset)
    assert len(pages) == 2
    assert "alpha" in pages[0]
    assert "gamma" in pages[1]
    assert "beta" not in "\n".join(pages)


def test_subset_pdf_pages_raises_on_invalid_pdf() -> None:
    with pytest.raises(PDFReadError):
        subset_pdf_pages(b"not-a-pdf", [0])
