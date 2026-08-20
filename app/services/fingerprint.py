import re
from typing import Optional

_MAX_SIGNATURE_LENGTH = 2048

_MONEY_VALUE = r"(?:[$€£¥₹]\s*)?\d{1,3}(?:,\d{3})*(?:\.\d{2})?"

_EMAIL_PATTERN = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
)
_PHONE_PATTERN = re.compile(
    r"(?:\+?1[-.\s])?\(?\d{3}\)[-.\s]\d{3}[-.\s]\d{4}\b|"
    r"\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b"
)
_MONTH_NAME = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?)"
)
_DATETIME_PATTERN = re.compile(
    r"\b\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}\s+"
    r"\d{1,2}:\d{2}(?::\d{2})?(?:\s*[AP]M)?\b",
    re.IGNORECASE,
)
_ISO_DATE_PATTERN = re.compile(r"\b\d{4}[-/.]\d{1,2}[-/.]\d{1,2}\b")
_US_DATE_PATTERN = re.compile(r"\b\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}\b")
_EU_DATE_PATTERN = re.compile(r"\b\d{1,2}\.\d{1,2}\.\d{2,4}\b")
_NAMED_DATE_PATTERN = re.compile(
    rf"\b(?:{_MONTH_NAME}\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}|"
    rf"\d{{1,2}}(?:st|nd|rd|th)?\s+{_MONTH_NAME},?\s+\d{{4}}|"
    rf"{_MONTH_NAME}\s+\d{{4}})"
    r"(?:\s+at\s+\d{1,2}:\d{2}(?::\d{2})?(?:\s*[AP]M)?)?\b",
    re.IGNORECASE,
)
_TIME_PATTERN = re.compile(
    r"\b(?:at\s+)?\d{1,2}:\d{2}(?::\d{2})?(?:\s*[AP]M)?\b",
    re.IGNORECASE,
)
_NEXT_FIELD = (
    r"(?=\s*(?:"
    r"delivery\s+address|restaurant\s+name|restaurant\s+address|"
    r"delivery\s+partner|\n\s*Item\s*\n|item\s+quantity|order\s+time|order\s+id|"
    r"customer\s+name|taxes?\b|total\b|subtotal\b|coupon\b|"
    r"free\s+delivery|platform\s+fee|delivery\s+charge|packaging|"
    r"terms\b|invoice\s+date|due\s+date|"
    # Stable identifier / GST fields so name/address captures stop cleanly and
    # do not swallow the vendor identifiers we rely on for anchoring.
    r"(?:restaurant\s+)?gstin|(?:restaurant\s+)?fssai|pan\b|cin\b|"
    r"invoice\s+no|hsn(?:\s+code)?|state(?:\s+name)?|place\s+of\s+supply|"
    r"service\s+description|supply\s+description|legal\s+entity|sr\.?\s*no|"
    r"particulars|authoriz(?:s|z)?ed\s+signatory"
    r")\b|\Z)"
)
_NAME_LABELS = (
    r"(?:customer(?:\s+name)?|ordered\s+by|recipient(?:\s+name)?|"
    r"rider(?:\s+name)?|delivery\s+partner(?:'s)?\s+name|"
    r"restaurant(?:\s+name)?|account\s+name|user\s+name|name)"
)
_NAME_FIELD_PATTERN = re.compile(
    rf"(?im)(?P<label>\b{_NAME_LABELS}\s*:\s*)(?P<value>[\s\S]+?{_NEXT_FIELD})"
)
_ADDRESS_LABELS = (
    r"(?:delivery\s+address|deliver(?:y)?(?:\s+address)?\s+to|deliver\s+to|"
    r"shipping\s+address|ship\s+to|billing\s+address|bill\s+to|"
    r"restaurant\s+address|drop[\s-]?off(?:\s+location)?|pickup\s+address|address)"
)
_ADDRESS_FIELD_PATTERN = re.compile(
    rf"(?im)(?P<label>\b{_ADDRESS_LABELS}\s*:\s*)(?P<value>[\s\S]+?{_NEXT_FIELD})"
)
_SUMMARY_LINE_PREFIX = (
    r"(?:subtotal|total|taxes?|tip|fees?|delivery\s+(?:charge|fee)|service\s+fee|"
    r"booking\s+fee|platform\s+fee|packaging(?:\s+charges?)?|"
    r"restaurant\s+packaging(?:\s+charges?)?|balance\s+due|amount\s+due|"
    r"grand\s+total|order\s+total|paid|refund)"
)
_SUMMARY_LINE_START = re.compile(
    rf"(?im)^\s*(?:{_SUMMARY_LINE_PREFIX})\b"
)
_VERTICAL_TABLE_HEADER_PATTERN = re.compile(
    r"(?im)^Item\s*\n\s*Quantity\s*\n\s*Unit\s+Price\s*\n\s*Total\s+Price\s*\n"
)
_MONEY_ONLY_LINE_PATTERN = re.compile(rf"(?im)^\s*{_MONEY_VALUE}\s*$")
_DISCOUNT_LINE_PREFIX = (
    r"(?:delivery\s+charge|delivery\s+fee|service\s+fee|platform\s+fee|taxes?)"
)
_DISCOUNT_LINE_PATTERN = re.compile(
    rf"(?im)"
    rf"^(?!\s*(?:{_DISCOUNT_LINE_PREFIX})\b)"
    rf".*\b(?:coupon|promo(?:tion)?|free\s+delivery|discount|savings|"
    rf"membership|gold|cashback|offer)\b.*$"
)
_TABLE_HEADER_PATTERN = re.compile(
    r"(?im)^\s*item\s+quantity\s+unit\s+price\s+total\s+price\s*$"
)
_LINE_ITEM_PATTERN = re.compile(
    rf"(?im)"
    rf"^(?!\s*(?:{_SUMMARY_LINE_PREFIX})\b)"
    rf"(?!\s*item\s+quantity\b)"
    rf"(?!\s*{_MONEY_VALUE}\s*$)"
    rf"(?:"
    rf"(?P<multi>.+?(?:{_MONEY_VALUE})\s+(?:{_MONEY_VALUE})\s*)"
    rf"|"
    rf"(?P<symbol>.+[$€£¥₹]\s*\d{{1,3}}(?:,\d{{3}})*(?:\.\d{{2}})?(?:\s+[$€£¥₹]\s*\d{{1,3}}(?:,\d{{3}})*(?:\.\d{{2}})?)*)"
    rf"|"
    rf"(?P<decimal>.+\d+\.\d{{2}}\s*)"
    rf")$"
)
# Mask amounts with a currency symbol OR any decimal fraction. The decimal branch
# accepts one-or-more fractional digits so non-standard totals (e.g. GST's 17.582)
# do not leak into the signature as variable data.
_CURRENCY_PATTERN = re.compile(
    r"(?:[$€£¥₹]\s*\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+\.\d+)\b"
)
_URL_PATTERN = re.compile(r"https?://[^\s)]+")
_PROMO_CODE_PATTERN = re.compile(r"\([A-Z0-9][A-Z0-9_-]{2,}\)")
_ORDER_ID_PATTERN = re.compile(r"(?i)\border\s+id\s*:\s*[\w-]+")
# Mask the identifier after Invoice No / Invoice No. / Invoice Number / Invoice #
# (and bill/receipt variants) while keeping the label for structural stability.
_INVOICE_NUMBER_PATTERN = re.compile(
    r"(?im)(?P<label>\b(?:invoice|inv|bill|receipt)\s*(?:no\.?|number|#)\s*[:.\-]?\s*)"
    r"[A-Za-z0-9][\w\-/]*"
)
_REFERENCE_PATTERN = re.compile(
    r"\b(?:invoice|inv|po)\s*#\s*[\w-]+\b",
    re.IGNORECASE,
)
# Amount rendered in words, e.g. "Two Hundred Forty One Rupees And Forty Five Paisa Only".
_AMOUNT_IN_WORDS_PATTERN = re.compile(
    r"(?i)\b[A-Za-z][A-Za-z \-]*?\b(?:rupees?|dollars?|euros?|pounds?)\b"
    r"[A-Za-z \-]*?\bonly\b"
)
_LONG_NUMBER_PATTERN = re.compile(r"\b\d{5,}\b")
# Bare integer amounts/quantities (e.g. a GST row printed as "150 0 150" instead
# of "150.00 0.00 150.00") would otherwise leak as variable data.
_BARE_NUMBER_PATTERN = re.compile(r"\b\d+\b")
# Collapse consecutive identical placeholders so that the *number* of line items
# or amount columns (variable content) does not change the layout signature.
_REPEATED_TOKEN_PATTERN = re.compile(r"(<[A-Z_]+>)(?:\s+\1)+")
_WHITESPACE_PATTERN = re.compile(r"\s+")

# Long legal / terms boilerplate carries no vendor-discriminating structure and
# dilutes trigram similarity, so it is dropped from the signature (only).
_BOILERPLATE_START = re.compile(
    r"(?im)^\s*(?:terms\s*(?:&|and)?\s*conditions?|terms\s+of\s+service|"
    r"please\s+refer\s+to|please\s+note\s+that|for\s+food\s+safety|"
    r"disclaimer|this\s+is\s+a\s+(?:computer|system)\s+generated)"
)

# Stable vendor identifiers used as an exact-match anchor for template matching.
_GSTIN_RE = re.compile(r"\b\d{2}[A-Z]{5}\d{4}[A-Z][0-9A-Z]Z[0-9A-Z]\b")
_PAN_RE = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")
_CIN_RE = re.compile(r"\b[A-Z]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}\b")
_URL_HOST_RE = re.compile(r"https?://([^/\s)]+)", re.IGNORECASE)


def _insert_line_breaks(text: str) -> str:
    """Split common receipt sections so line-based masks work on PDF text."""
    normalized = text
    break_before = [
        r"Item\s+Quantity",
        r"Taxes?\b",
        r"Delivery\s+charge",
        r"Restaurant\s+Packaging",
        r"Platform\s+fee",
        r"Free\s+Delivery",
        r"Coupon\b",
        r"Total\b",
        r"Terms\b",
    ]
    for pattern in break_before:
        normalized = re.sub(rf"(?i)\s+(?={pattern})", "\n", normalized)
    normalized = re.sub(r"(?i)(Total\s+Price)\s+", r"\1\n", normalized)
    return normalized


def _join_summary_amount_lines(text: str) -> str:
    """Merge Zomato-style summary labels with amounts on the following line."""
    lines = text.split("\n")
    merged: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if (
            _SUMMARY_LINE_START.match(stripped + " ")
            and index + 1 < len(lines)
            and _MONEY_ONLY_LINE_PATTERN.match(lines[index + 1].strip())
        ):
            merged.append(f"{stripped} {lines[index + 1].strip()}")
            index += 2
            continue
        merged.append(line)
        index += 1
    return "\n".join(merged)


def _mask_vertical_item_table(text: str) -> str:
    """Mask item rows when Zomato prints the table in vertical columns."""
    match = _VERTICAL_TABLE_HEADER_PATTERN.search(text)
    if match is None:
        return text

    tail = text[match.end() :]
    summary = _SUMMARY_LINE_START.search(tail)
    if summary is None:
        return text[: match.start()] + "<TABLE_HEADER>\n<LINE_ITEM>\n" + tail

    return (
        text[: match.start()]
        + "<TABLE_HEADER>\n<LINE_ITEM>\n"
        + tail[summary.start() :]
    )


def _mask_name_fields(text: str) -> str:
    return _NAME_FIELD_PATTERN.sub(r"\g<label><NAME>", text)


def _mask_address_fields(text: str) -> str:
    return _ADDRESS_FIELD_PATTERN.sub(r"\g<label><ADDRESS>", text)


def _mask_discount_lines(text: str) -> str:
    return _DISCOUNT_LINE_PATTERN.sub("<DISCOUNT>", text)


def _mask_table_headers(text: str) -> str:
    return _TABLE_HEADER_PATTERN.sub("<TABLE_HEADER>", text)


def _mask_line_items(text: str) -> str:
    return _LINE_ITEM_PATTERN.sub("<LINE_ITEM>", text)


def _apply_masks(text: str) -> str:
    masked = _insert_line_breaks(text)
    masked = _join_summary_amount_lines(masked)
    masked = _EMAIL_PATTERN.sub("<EMAIL>", masked)
    masked = _PHONE_PATTERN.sub("<PHONE>", masked)
    masked = _DATETIME_PATTERN.sub("<DATE>", masked)
    masked = _ISO_DATE_PATTERN.sub("<DATE>", masked)
    masked = _US_DATE_PATTERN.sub("<DATE>", masked)
    masked = _EU_DATE_PATTERN.sub("<DATE>", masked)
    masked = _NAMED_DATE_PATTERN.sub("<DATE>", masked)
    masked = _TIME_PATTERN.sub("<TIME>", masked)
    masked = _ORDER_ID_PATTERN.sub("Order ID: <NUM>", masked)
    masked = _INVOICE_NUMBER_PATTERN.sub(r"\g<label><REF>", masked)
    masked = _mask_name_fields(masked)
    masked = _mask_address_fields(masked)
    masked = _mask_vertical_item_table(masked)
    masked = _mask_table_headers(masked)
    masked = _mask_discount_lines(masked)
    masked = _mask_line_items(masked)
    masked = _AMOUNT_IN_WORDS_PATTERN.sub("<AMOUNT>", masked)
    masked = _CURRENCY_PATTERN.sub("<AMOUNT>", masked)
    masked = _PROMO_CODE_PATTERN.sub("(<CODE>)", masked)
    masked = _URL_PATTERN.sub("<URL>", masked)
    masked = _REFERENCE_PATTERN.sub("<REF>", masked)
    # Normalize entity identifiers so invoices sharing a layout but issued for
    # different entities (e.g. different restaurants on the same platform) match.
    # These values are still captured as the vendor anchor from the raw text.
    masked = _GSTIN_RE.sub("<GSTIN>", masked)
    masked = _CIN_RE.sub("<CIN>", masked)
    masked = _PAN_RE.sub("<PAN>", masked)
    masked = _LONG_NUMBER_PATTERN.sub("<NUM>", masked)
    masked = _BARE_NUMBER_PATTERN.sub("<AMOUNT>", masked)
    masked = _WHITESPACE_PATTERN.sub(" ", masked).strip()
    return _REPEATED_TOKEN_PATTERN.sub(r"\1", masked)


def _looks_like_prose(line: str) -> bool:
    """Detect long free-text sentences that add noise but no structural signal."""
    words = line.split()
    if len(words) < 14:
        return False
    if ":" in line or "<" in line:
        return False
    has_sentence_punct = any(char in line for char in ".;")
    has_lowercase = any(char.islower() for char in line)
    return has_sentence_punct and has_lowercase


def _strip_boilerplate(text: str) -> str:
    """Remove legal/terms boilerplate so the signature reflects layout, not legalese."""
    match = _BOILERPLATE_START.search(text)
    if match is not None:
        text = text[: match.start()]
    kept = [line for line in text.split("\n") if not _looks_like_prose(line)]
    return "\n".join(kept)


def extract_vendor_key(text: str) -> Optional[str]:
    """Return a stable vendor identifier for exact-match template anchoring.

    Prefers issuer-level identifiers that stay constant across a vendor's invoices:
    email domain, then URL host, then PAN (entity-wide), then GSTIN. Returns None
    when no reliable identifier is present, so matching falls back to fuzzy similarity.
    """
    if not text:
        return None

    email_match = _EMAIL_PATTERN.search(text)
    if email_match is not None:
        domain = email_match.group(0).rsplit("@", 1)[-1].lower().strip(".")
        return re.sub(r"^www\.", "", domain)

    url_match = _URL_HOST_RE.search(text)
    if url_match is not None:
        host = url_match.group(1).lower()
        return re.sub(r"^www\.", "", host)

    pan_match = _PAN_RE.search(text)
    if pan_match is not None:
        return f"pan:{pan_match.group(0)}"

    gstin_match = _GSTIN_RE.search(text)
    if gstin_match is not None:
        return f"gstin:{gstin_match.group(0)}"

    return None


def mask_invoice_text(raw_text: str) -> str:
    """Return the full masked invoice text without signature truncation."""
    if not raw_text:
        return ""
    return _apply_masks(raw_text)


def generate_signature(raw_text: str) -> str:
    """Mask variable invoice fields and return a stable text signature.

    Legal/terms boilerplate is stripped first so the signature captures the vendor's
    layout structure rather than shared legalese.
    """
    if not raw_text:
        return ""

    signature = _apply_masks(_strip_boilerplate(raw_text))
    return signature[:_MAX_SIGNATURE_LENGTH]
