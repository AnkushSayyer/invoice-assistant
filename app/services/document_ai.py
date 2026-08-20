"""Google Document AI Invoice Parser integration (optional extraction backend).

This is an *additive* perception backend for the agent. When a Document AI
processor is configured via environment variables, the agent prefers it over the
PyMuPDF + LLM path because it OCRs scanned/photographed invoices and returns
structured fields with per-field confidence. When it is not configured (or the
client library is unavailable), callers fall back to the existing LLM extractor.

No Google client library is imported at module load, so the rest of the app runs
unchanged whether or not ``google-cloud-documentai`` is installed.
"""

import os
from datetime import date as Date
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

from app.schemas.extraction import InvoiceExtraction, LineItem

# Document AI Invoice Parser entity type -> our field. Multiple fee-like entities
# are summed into ``fees``. See cloud.google.com/document-ai/docs/fields.
_SUPPLIER_FIELDS = ("supplier_name",)
_INVOICE_ID_FIELDS = ("invoice_id",)
_DATE_FIELDS = ("invoice_date",)
_SUBTOTAL_FIELDS = ("net_amount",)
_TAX_FIELDS = ("total_tax_amount", "vat/tax_amount")
_TOTAL_FIELDS = ("total_amount",)
_FEE_FIELDS = ("freight_amount",)
_VENDOR_TAX_ID_FIELDS = ("supplier_tax_id", "supplier_registration")


class DocumentAINotConfigured(Exception):
    """Raised when Document AI env vars are absent, so callers can fall back."""


class DocumentAIError(Exception):
    """Raised when a configured Document AI call fails."""


def is_document_ai_configured() -> bool:
    """True only when all required Document AI settings are present."""
    return all(
        os.getenv(name, "").strip()
        for name in ("DOCAI_PROJECT_ID", "DOCAI_LOCATION", "DOCAI_PROCESSOR_ID")
    )


def _to_decimal(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    # Parenthesised negatives -> negative, strip currency symbols.
    negative = text.startswith("(") and text.endswith(")")
    cleaned = "".join(ch for ch in text if ch.isdigit() or ch in ".-")
    if not cleaned or cleaned in {".", "-"}:
        return None
    try:
        amount = Decimal(cleaned)
    except InvalidOperation:
        return None
    return -amount if negative else amount


def _entity_value(entity: Any) -> Any:
    """Prefer Document AI's normalized value, else the raw mention text."""
    normalized = getattr(entity, "normalized_value", None)
    if normalized is not None:
        text = getattr(normalized, "text", None)
        if text:
            return text
    return getattr(entity, "mention_text", None)


def _normalized_date(entity: Any) -> Optional[Date]:
    normalized = getattr(entity, "normalized_value", None)
    dv = getattr(normalized, "date_value", None) if normalized is not None else None
    if dv is not None and getattr(dv, "year", 0):
        try:
            return Date(dv.year, dv.month or 1, dv.day or 1)
        except ValueError:
            return None
    raw = getattr(entity, "mention_text", None)
    if raw:
        try:
            return Date.fromisoformat(raw.strip()[:10])
        except ValueError:
            return None
    return None


def _line_item_from_properties(properties: List[Any]) -> Optional[LineItem]:
    description: Optional[str] = None
    amount: Optional[Decimal] = None
    quantity: Optional[Decimal] = None
    unit_price: Optional[Decimal] = None
    for prop in properties:
        ptype = getattr(prop, "type_", "") or getattr(prop, "type", "")
        if ptype == "line_item/description":
            description = str(_entity_value(prop) or "").strip() or None
        elif ptype == "line_item/amount":
            amount = _to_decimal(_entity_value(prop))
        elif ptype == "line_item/quantity":
            quantity = _to_decimal(_entity_value(prop))
        elif ptype == "line_item/unit_price":
            unit_price = _to_decimal(_entity_value(prop))
    if description is None and amount is None:
        return None
    return LineItem(
        description=description or "Item",
        amount=amount if amount is not None else Decimal("0"),
        quantity=quantity if quantity is not None else Decimal("1"),
        unit_price=unit_price,
    )


def map_document_to_extraction(
    document: Any,
) -> Tuple[InvoiceExtraction, Dict[str, float], Optional[str]]:
    """Map a Document AI ``document`` into our schema plus per-field confidence.

    Returns ``(extraction, per_field_confidence, vendor_tax_id)`` where the tax id
    (GSTIN/VAT registration) is used by perception to build a stable vendor key.

    ``document`` is duck-typed: it needs an ``entities`` iterable whose items expose
    ``type_``/``type``, ``mention_text``, ``confidence``, ``normalized_value`` and
    (for line items) ``properties``. This keeps the mapper unit-testable without the
    Google client library.
    """
    vendor: Optional[str] = None
    invoice_number: Optional[str] = None
    inv_date: Optional[Date] = None
    subtotal: Optional[Decimal] = None
    tax = Decimal("0")
    total: Optional[Decimal] = None
    fees = Decimal("0")
    vendor_tax_id: Optional[str] = None
    line_items: List[LineItem] = []
    confidences: Dict[str, float] = {}

    def _record(field: str, entity: Any) -> None:
        conf = getattr(entity, "confidence", None)
        if conf is not None:
            confidences[field] = float(conf)

    for entity in getattr(document, "entities", []) or []:
        etype = getattr(entity, "type_", "") or getattr(entity, "type", "")
        if etype in _SUPPLIER_FIELDS:
            vendor = str(_entity_value(entity) or "").strip() or vendor
            _record("vendor", entity)
        elif etype in _INVOICE_ID_FIELDS:
            invoice_number = str(_entity_value(entity) or "").strip() or invoice_number
            _record("invoice_number", entity)
        elif etype in _DATE_FIELDS:
            inv_date = _normalized_date(entity) or inv_date
            _record("date", entity)
        elif etype in _SUBTOTAL_FIELDS:
            subtotal = _to_decimal(_entity_value(entity))
            _record("subtotal", entity)
        elif etype in _TAX_FIELDS:
            value = _to_decimal(_entity_value(entity))
            if value is not None:
                tax += value
            _record("tax", entity)
        elif etype in _TOTAL_FIELDS:
            total = _to_decimal(_entity_value(entity))
            _record("total", entity)
        elif etype in _FEE_FIELDS:
            value = _to_decimal(_entity_value(entity))
            if value is not None:
                fees += value
            _record("fees", entity)
        elif etype in _VENDOR_TAX_ID_FIELDS:
            vendor_tax_id = str(_entity_value(entity) or "").strip() or vendor_tax_id
        elif etype == "line_item":
            item = _line_item_from_properties(getattr(entity, "properties", []) or [])
            if item is not None:
                line_items.append(item)

    # Fall back sensibly when the parser omits a subtotal/total.
    if subtotal is None:
        if line_items:
            subtotal = sum((item.amount for item in line_items), Decimal("0"))
        elif total is not None:
            subtotal = total - tax - fees
        else:
            subtotal = Decimal("0")
    if total is None:
        total = subtotal + tax + fees

    extraction = InvoiceExtraction(
        vendor=vendor or "Unknown vendor",
        invoice_number=invoice_number or "UNKNOWN",
        date=inv_date or Date.today(),
        line_items=line_items,
        discounts=[],
        subtotal=subtotal,
        tax=tax,
        fees=fees,
        tip=Decimal("0"),
        total=total,
    )
    return extraction, confidences, vendor_tax_id


def overall_confidence(confidences: Dict[str, float]) -> float:
    """Confidence gate = the weakest of the money-critical fields."""
    critical = [
        confidences[key]
        for key in ("total", "subtotal", "tax")
        if key in confidences
    ]
    if not critical:
        return min(confidences.values()) if confidences else 0.0
    return min(critical)


def extract_with_document_ai(
    pdf_bytes: bytes,
) -> Tuple[InvoiceExtraction, Dict[str, float], float, Optional[str]]:
    """Call the configured Document AI processor and map the result.

    Returns ``(extraction, per_field_confidence, overall_confidence, vendor_tax_id)``.
    Raises ``DocumentAINotConfigured`` when settings are missing so the caller can
    fall back, or ``DocumentAIError`` when a configured call fails.
    """
    if not is_document_ai_configured():
        raise DocumentAINotConfigured(
            "Set DOCAI_PROJECT_ID, DOCAI_LOCATION and DOCAI_PROCESSOR_ID to enable "
            "Document AI extraction"
        )
    try:
        from google.api_core.client_options import ClientOptions
        from google.cloud import documentai
    except ImportError as exc:  # pragma: no cover - depends on optional dependency
        raise DocumentAIError(
            "google-cloud-documentai is not installed"
        ) from exc

    project_id = os.environ["DOCAI_PROJECT_ID"].strip()
    location = os.environ["DOCAI_LOCATION"].strip()
    processor_id = os.environ["DOCAI_PROCESSOR_ID"].strip()

    try:
        opts = ClientOptions(api_endpoint=f"{location}-documentai.googleapis.com")
        client = documentai.DocumentProcessorServiceClient(client_options=opts)
        name = client.processor_path(project_id, location, processor_id)
        raw_document = documentai.RawDocument(
            content=pdf_bytes, mime_type="application/pdf"
        )
        request = documentai.ProcessRequest(name=name, raw_document=raw_document)
        result = client.process_document(request=request)
    except Exception as exc:  # pragma: no cover - network/credentials dependent
        raise DocumentAIError(f"Document AI processing failed: {exc}") from exc

    extraction, confidences, vendor_tax_id = map_document_to_extraction(result.document)
    return extraction, confidences, overall_confidence(confidences), vendor_tax_id
