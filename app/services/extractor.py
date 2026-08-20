import json
import os
import re
from decimal import Decimal
from statistics import median
from typing import Any, List, Optional, Sequence, Tuple

import fitz
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import TemplateExample
from app.schemas.extraction import InvoiceExtraction
from app.services.llm import (
    ExtractionLLMError,
    MissingLLMApiKeyError,
    create_instructor_client,
    get_default_model,
    get_llm_provider,
    is_llm_timeout,
)

DEFAULT_TIMEOUT_SECONDS = 60.0

SYSTEM_PROMPT = (
    "You extract structured invoice and receipt data from raw text. "
    "Support standard invoices, food delivery receipts, Uber-style trip receipts, "
    "and GST/VAT tax invoices. "
    "The text is layout-preserving: columns are separated by tabs and rows by "
    "newlines, so read tabular invoices row by row. "
    "Capture each food item or fare component as a line item. "
    "Capture coupons, promos, membership savings, and credits as discounts "
    "(store discount amounts as positive numbers). "
    "Use tip for gratuity when present. "
    "Return only the requested fields with accurate numeric values. "
    # GST / tax-invoice semantics.
    "For each line item, set amount to the NET value before tax (the pre-tax amount "
    "or gross-minus-discount value); never include the line's tax in its amount. "
    "On GST tables the per-row 'Total' column already includes CGST/SGST, so do NOT "
    "use that column for the line amount. "
    "Set subtotal to the sum of the net line-item values, before tax, fees, and "
    "discounts. "
    "Put all taxes in tax: sum CGST + SGST + IGST (and any VAT/GST) into a single "
    "tax value. "
    "Put packaging charges, restaurant packaging, delivery charges, platform fees, "
    "service, convenience, and booking fees into fees (not line items). "
    "Treat amounts shown in parentheses, e.g. (\u20b948), as negative: record them as "
    "discounts with a positive amount. Empty parentheses '()' mean no amount, so "
    "ignore them. "
    "Set total to the final grand total actually charged."
)


class PDFReadError(Exception):
    """Raised when PyMuPDF cannot read the supplied PDF bytes."""


class ExtractionTimeoutError(Exception):
    """Raised when the LLM request exceeds the configured timeout."""


# A PyMuPDF "word" tuple: (x0, y0, x1, y1, text, block_no, line_no, word_no).
Word = Tuple[float, float, float, float, str, int, int, int]

# Default vertical tolerance (in points) used only when word heights are unavailable.
_DEFAULT_ROW_TOLERANCE = 3.0
# Horizontal gaps wider than this many "space widths" are treated as column breaks.
_COLUMN_GAP_MULTIPLIER = 1.8


def _estimate_space_width(words: Sequence[Word]) -> float:
    """Estimate the width of a single character to detect inter-column gaps."""
    per_char_widths = [
        (x1 - x0) / len(text)
        for x0, _, x1, _, text, *_ in words
        if text and (x1 - x0) > 0
    ]
    if not per_char_widths:
        return 4.0
    return median(per_char_widths)


def _estimate_row_tolerance(words: Sequence[Word]) -> float:
    """Estimate how far apart (vertically) two words can be and still share a row."""
    heights = [(y1 - y0) for _, y0, _, y1, *_ in words if (y1 - y0) > 0]
    if not heights:
        return _DEFAULT_ROW_TOLERANCE
    return median(heights) * 0.6


def _cluster_words_into_rows(
    words: Sequence[Word],
    y_tolerance: float,
) -> List[List[Word]]:
    """Group words into visual rows using their vertical centre, preserving x order."""
    rows: List[List[Word]] = []
    # Sort by vertical centre, then by left edge so reading order is stable.
    by_vertical = sorted(words, key=lambda w: ((w[1] + w[3]) / 2, w[0]))

    current_row: List[Word] = []
    current_center: Optional[float] = None
    for word in by_vertical:
        center = (word[1] + word[3]) / 2
        if current_center is None or abs(center - current_center) <= y_tolerance:
            current_row.append(word)
            # Track the running mean centre so tall/short words don't drift the row.
            centers = [(w[1] + w[3]) / 2 for w in current_row]
            current_center = sum(centers) / len(centers)
        else:
            rows.append(sorted(current_row, key=lambda w: w[0]))
            current_row = [word]
            current_center = center

    if current_row:
        rows.append(sorted(current_row, key=lambda w: w[0]))
    return rows


def _row_to_text(row: Sequence[Word], space_width: float) -> str:
    """Render one visual row, inserting a tab where a column gap is detected."""
    ordered = sorted(row, key=lambda w: w[0])
    gap_threshold = space_width * _COLUMN_GAP_MULTIPLIER

    parts: List[str] = []
    prev_x1: Optional[float] = None
    for x0, _, x1, _, text, *_ in ordered:
        text = text.strip()
        if not text:
            continue
        if prev_x1 is not None:
            separator = "\t" if (x0 - prev_x1) > gap_threshold else " "
            parts.append(separator)
        parts.append(text)
        prev_x1 = x1
    return "".join(parts)


def _page_to_layout_text(page: "fitz.Page") -> str:
    """Reconstruct a page's text with rows/columns preserved for tabular invoices."""
    words: List[Word] = page.get_text("words")
    if not words:
        # Scanned/image pages or empty text layers: fall back to plain extraction.
        return page.get_text().strip()

    space_width = _estimate_space_width(words)
    y_tolerance = _estimate_row_tolerance(words)
    rows = _cluster_words_into_rows(words, y_tolerance)
    lines = [_row_to_text(row, space_width) for row in rows]
    return "\n".join(line for line in lines if line.strip())


def read_pdf_pages(pdf_bytes: bytes) -> List[str]:
    """Return layout-aware text for each page of a PDF using PyMuPDF."""
    try:
        with fitz.open(stream=pdf_bytes, filetype="pdf") as document:
            return [_page_to_layout_text(page) for page in document]
    except Exception as exc:
        raise PDFReadError(f"Failed to read PDF: {exc}") from exc


def read_pdf_text(pdf_bytes: bytes) -> str:
    """Extract layout-aware text from a PDF using PyMuPDF.

    Words are clustered into visual rows (and columns via tab separators) so that
    tabular invoices are not collapsed into an ambiguous vertical stream of numbers.
    """
    pages = read_pdf_pages(pdf_bytes)
    return "\n".join(page.strip() for page in pages if page.strip())


# Matches the start of a distinct invoice: an "Invoice No", "Invoice Number", or
# "Invoice #" label. Used to detect PDFs that bundle multiple invoices (e.g. a
# restaurant tax invoice plus a separate platform-fee tax invoice).
_INVOICE_NUMBER_RE = re.compile(
    r"^\s*invoice\s*(?:no\.?|number|#)\b",
    re.IGNORECASE | re.MULTILINE,
)


def split_invoice_page_groups(pages: Sequence[str]) -> List[List[int]]:
    """Group *page indices* into one group per distinct invoice.

    A page begins a new invoice when it carries its own invoice-number line and the
    current group already contains one; otherwise the page is a continuation. Blank
    pages (scanned/image pages with no extractable text) are kept with the current
    group so nothing is dropped before OCR. Documents without repeated
    invoice-number markers collapse to a single group covering every page.

    This is the index-level counterpart of :func:`split_invoice_segments`; it lets
    callers rebuild a per-invoice sub-PDF (e.g. to send each invoice to Document AI
    separately) rather than only per-invoice text.
    """
    groups: List[List[int]] = []
    current: List[int] = []
    current_has_marker = False

    for index, page in enumerate(pages):
        has_marker = bool(page.strip()) and bool(_INVOICE_NUMBER_RE.search(page))
        if has_marker and current_has_marker:
            groups.append(current)
            current = [index]
            current_has_marker = True
        else:
            current.append(index)
            current_has_marker = current_has_marker or has_marker

    if current:
        groups.append(current)

    return [group for group in groups if group] or [list(range(len(pages)))]


def subset_pdf_pages(pdf_bytes: bytes, page_indices: Sequence[int]) -> bytes:
    """Return a new PDF containing only ``page_indices`` from ``pdf_bytes``.

    Used to hand each bundled invoice to Document AI as its own clean document,
    which parses far more reliably (and confidently) than a multi-invoice bundle.
    """
    try:
        with fitz.open(stream=pdf_bytes, filetype="pdf") as source:
            output = fitz.open()
            try:
                for index in page_indices:
                    output.insert_pdf(source, from_page=index, to_page=index)
                return output.tobytes()
            finally:
                output.close()
    except Exception as exc:
        raise PDFReadError(f"Failed to subset PDF pages: {exc}") from exc


def split_invoice_segments(pages: Sequence[str]) -> List[str]:
    """Group pages into one text segment per distinct invoice.

    A page begins a new invoice when it carries its own invoice-number line and the
    current segment already contains one; otherwise the page is treated as a
    continuation. Documents without repeated invoice-number markers (receipts,
    single invoices) collapse to a single segment, preserving existing behaviour.
    """
    groups: List[List[str]] = []
    current: List[str] = []
    current_has_marker = False

    for page in pages:
        if not page.strip():
            continue
        has_marker = bool(_INVOICE_NUMBER_RE.search(page))
        if has_marker and current_has_marker:
            groups.append(current)
            current = [page]
            current_has_marker = True
        else:
            current.append(page)
            current_has_marker = current_has_marker or has_marker

    if current:
        groups.append(current)

    segments = ["\n".join(group).strip() for group in groups]
    return [segment for segment in segments if segment] or [""]


def combine_invoices(invoices: Sequence[InvoiceExtraction]) -> InvoiceExtraction:
    """Merge multiple invoices from one document into a single extraction.

    Monetary components are summed so downstream math validation reconciles the
    document as a whole (e.g. restaurant total + platform-fee total). Line items and
    discounts are concatenated; vendor and invoice numbers are joined for traceability.
    """
    if not invoices:
        raise ValueError("combine_invoices requires at least one invoice")
    if len(invoices) == 1:
        return invoices[0]

    def _sum(field: str) -> Decimal:
        return sum(
            (getattr(invoice, field) for invoice in invoices), Decimal("0")
        )

    vendors = list(dict.fromkeys(inv.vendor for inv in invoices if inv.vendor))
    numbers = [inv.invoice_number for inv in invoices if inv.invoice_number]

    return InvoiceExtraction(
        vendor=" + ".join(vendors) if vendors else invoices[0].vendor,
        invoice_number=", ".join(numbers) if numbers else invoices[0].invoice_number,
        date=invoices[0].date,
        line_items=[item for inv in invoices for item in inv.line_items],
        discounts=[discount for inv in invoices for discount in inv.discounts],
        subtotal=_sum("subtotal"),
        tax=_sum("tax"),
        fees=_sum("fees"),
        tip=_sum("tip"),
        total=_sum("total"),
    )


def _build_messages(
    invoice_text: str,
    template_example: Optional[TemplateExample] = None,
    *,
    provider: str | None = None,
    directives: Optional[Sequence[str]] = None,
    few_shots: Optional[Sequence[Any]] = None,
) -> list[dict[str, str]]:
    system_content = SYSTEM_PROMPT
    if directives:
        # Learned, vendor-specific lessons (e.g. "capture the footer airport
        # surcharge into fees") steer extraction so a once-clarified quirk is not
        # missed again.
        lessons = "\n".join(f"- {d}" for d in directives if d)
        if lessons:
            system_content = (
                f"{SYSTEM_PROMPT}\n\n"
                "Apply these learned rules for this vendor when extracting:\n"
                f"{lessons}"
            )

    messages: list[dict[str, str]] = [
        {"role": "system", "content": system_content},
    ]

    example_role = "model" if provider == "gemini" else "assistant"

    def _append_example(example: Any) -> None:
        example_text = example.raw_text or example.masked_text
        approved_json = json.dumps(example.expected_fields, default=str)
        messages.extend(
            [
                {
                    "role": "user",
                    "content": (
                        "Example invoice text:\n"
                        f"{example_text}\n\n"
                        "Extract the invoice fields from the example above."
                    ),
                },
                {
                    "role": example_role,
                    "content": approved_json,
                },
            ]
        )

    if template_example is not None:
        _append_example(template_example)
    for example in few_shots or []:
        _append_example(example)

    messages.append(
        {
            "role": "user",
            "content": (
                "Extract invoice fields from the following text:\n"
                f"{invoice_text}"
            ),
        }
    )
    return messages


def get_template_example(
    db: Session,
    template_id: Any,
) -> Optional[TemplateExample]:
    """Return the most recent few-shot example for a template, if any."""
    stmt = (
        select(TemplateExample)
        .where(TemplateExample.template_id == template_id)
        .order_by(TemplateExample.created_at.desc())
        .limit(1)
    )
    return db.execute(stmt).scalar_one_or_none()


def extract_invoice_from_text(
    invoice_text: str,
    *,
    template_example: Optional[TemplateExample] = None,
    directives: Optional[Sequence[str]] = None,
    few_shots: Optional[Sequence[Any]] = None,
    model: str | None = None,
    instructor_client: Any | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> InvoiceExtraction:
    """Call the configured LLM provider to extract structured invoice data."""
    client = instructor_client or create_instructor_client()
    resolved_model = model or get_default_model()
    provider = get_llm_provider()
    messages = _build_messages(
        invoice_text,
        template_example,
        provider=provider,
        directives=directives,
        few_shots=few_shots,
    )
    create_kwargs: dict[str, Any] = {
        "model": resolved_model,
        "messages": messages,
        "response_model": InvoiceExtraction,
    }
    # google-genai does not accept OpenAI-style timeout kwargs.
    if provider != "gemini":
        create_kwargs["timeout"] = timeout_seconds

    try:
        return client.chat.completions.create(**create_kwargs)
    except MissingLLMApiKeyError:
        raise
    except Exception as exc:
        if is_llm_timeout(exc):
            raise ExtractionTimeoutError(
                f"LLM extraction timed out after {timeout_seconds} seconds"
            ) from exc
        raise ExtractionLLMError(
            f"LLM extraction failed via {get_llm_provider()}: {exc}"
        ) from exc


def extract_invoice_from_pdf(
    pdf_bytes: bytes,
    *,
    template_example: Optional[TemplateExample] = None,
    model: str | None = None,
    instructor_client: Any | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> InvoiceExtraction:
    """Read a PDF and extract structured invoice data."""
    invoice_text = read_pdf_text(pdf_bytes)
    return extract_invoice_from_text(
        invoice_text,
        template_example=template_example,
        model=model,
        instructor_client=instructor_client,
        timeout_seconds=timeout_seconds,
    )
