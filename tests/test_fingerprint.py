import pytest

from app.services.fingerprint import extract_vendor_key, generate_signature


def test_generate_signature_empty_string() -> None:
    assert generate_signature("") == ""


def test_generate_signature_masks_currency() -> None:
    signature = generate_signature("Total Due: $1,234.56")

    assert "$1,234.56" not in signature
    assert "<AMOUNT>" in signature
    assert "Total Due:" in signature


def test_generate_signature_masks_us_dates() -> None:
    signature = generate_signature("Invoice Date: 01/15/2024")

    assert "01/15/2024" not in signature
    assert "<DATE>" in signature
    assert "Invoice Date:" in signature


def test_generate_signature_masks_iso_dates() -> None:
    signature = generate_signature("Due Date: 2024-01-15")

    assert "2024-01-15" not in signature
    assert "<DATE>" in signature


def test_generate_signature_masks_invoice_references() -> None:
    signature = generate_signature("Invoice #INV-2024-00123")

    assert "INV-2024-00123" not in signature
    assert "<REF>" in signature


def test_generate_signature_masks_email_and_phone() -> None:
    signature = generate_signature(
        "Contact: billing@acme.com or (555) 123-4567"
    )

    assert "billing@acme.com" not in signature
    assert "(555) 123-4567" not in signature
    assert "<EMAIL>" in signature
    assert "<PHONE>" in signature


def test_generate_signature_masks_long_numeric_ids() -> None:
    signature = generate_signature("Account Number: 9876543210")

    assert "9876543210" not in signature
    assert "<NUM>" in signature


def test_generate_signature_normalizes_whitespace() -> None:
    signature = generate_signature("INVOICE\n\n  Total:   $10.00")

    assert "\n" not in signature
    assert "  " not in signature
    assert signature == "INVOICE Total: <AMOUNT>"


def test_generate_signature_masks_named_dates() -> None:
    signature = generate_signature("Ordered on January 15, 2024")

    assert "January 15, 2024" not in signature
    assert "<DATE>" in signature


def test_generate_signature_masks_eu_dates() -> None:
    signature = generate_signature("Delivery: 15.01.2024")

    assert "15.01.2024" not in signature
    assert "<DATE>" in signature


def test_generate_signature_masks_times() -> None:
    signature = generate_signature("Pickup at 2:45 PM from store")

    assert "2:45 PM" not in signature
    assert "<TIME>" in signature


def test_generate_signature_masks_datetime() -> None:
    signature = generate_signature("Placed 01/15/2024 14:30")

    assert "01/15/2024 14:30" not in signature
    assert "<DATE>" in signature


def test_generate_signature_masks_month_year() -> None:
    signature = generate_signature("Statement period: March 2024")

    assert "March 2024" not in signature
    assert "<DATE>" in signature


def test_generate_signature_masks_customer_name() -> None:
    signature = generate_signature("Customer name: Jane Smith")

    assert "Jane Smith" not in signature
    assert "Customer name: <NAME>" in signature


def test_generate_signature_masks_name_field() -> None:
    signature = generate_signature("Name: John Doe")

    assert "John Doe" not in signature
    assert "Name: <NAME>" in signature


def test_generate_signature_masks_vertical_zomato_item_table() -> None:
    raw_text = (
        "Delivery partner's Name:\n"
        "Sahil Kumar\n"
        "Item\n"
        "Quantity\n"
        "Unit Price\n"
        "Total Price\n"
        "Hot Chicken Wings - 4 pcs\n"
        "1\n"
        "₹189\n"
        "₹189\n"
        "Taxes\n"
        "₹14.27"
    )

    signature = generate_signature(raw_text)

    assert "Hot Chicken Wings" not in signature
    assert "Sahil Kumar" not in signature
    assert "<TABLE_HEADER>" in signature
    assert "<LINE_ITEM>" in signature
    assert "Taxes <AMOUNT>" in signature


def test_generate_signature_masks_single_line_delivery_address() -> None:
    signature = generate_signature(
        "Delivery address: 123 Main St, Boston, MA 02101"
    )

    assert "123 Main St" not in signature
    assert "Delivery address: <ADDRESS>" in signature


def test_generate_signature_masks_multiline_delivery_address() -> None:
    raw_text = (
        "Delivery Address:\n"
        "104, Parimala Elite, Swami Vivekanand Road, Floor 2, Parimala\n"
        "Elite, ITPL Main Road, Whitefield, Bengaluru\n"
        "Restaurant Name: Theobroma"
    )

    signature = generate_signature(raw_text)

    assert "Parimala Elite" not in signature
    assert "Whitefield" not in signature
    assert "Bengaluru" not in signature
    assert "Delivery Address: <ADDRESS>" in signature
    assert "Restaurant Name: <NAME>" in signature


def test_generate_signature_masks_line_item_rows() -> None:
    signature = generate_signature(
        "Uber Eats\nPad Thai $14.00\nSpring Rolls $6.00\nSubtotal $20.00"
    )

    assert "Pad Thai" not in signature
    assert "$14.00" not in signature
    assert "Spring Rolls" not in signature
    assert "<LINE_ITEM>" in signature
    assert "Subtotal <AMOUNT>" in signature


def test_generate_signature_is_stable_for_different_customers_and_items() -> None:
    invoice_a = (
        "Uber Eats\n"
        "Name: Alice Johnson\n"
        "Delivery address: 10 Oak Ave, Cambridge MA\n"
        "Pad Thai $14.00\n"
        "Subtotal $14.00\n"
        "Total $18.00"
    )
    invoice_b = (
        "Uber Eats\n"
        "Name: Bob Lee\n"
        "Delivery address: 99 Pine Rd, Boston MA\n"
        "Burger $12.00\n"
        "Subtotal $12.00\n"
        "Total $16.00"
    )

    assert generate_signature(invoice_a) == generate_signature(invoice_b)


def test_generate_signature_is_stable_for_variable_values() -> None:
    invoice_a = (
        "INVOICE\n"
        "Vendor: Acme Corp\n"
        "Invoice #INV-1001\n"
        "Date: January 15, 2024 at 2:30 PM\n"
        "Total: $100.00"
    )
    invoice_b = (
        "INVOICE\n"
        "Vendor: Acme Corp\n"
        "Invoice #INV-9999\n"
        "Date: 12/31/2025 11:59 PM\n"
        "Total: $9,999.99"
    )

    assert generate_signature(invoice_a) == generate_signature(invoice_b)


def test_generate_signature_truncates_to_max_length() -> None:
    raw_text = "WORD " * 800

    signature = generate_signature(raw_text)

    assert len(signature) == 2048


def test_generate_signature_masks_zomato_style_receipt() -> None:
    raw_text = (
        "Zomato Food Order: Summary and Receipt "
        "Order ID: 1234567890 "
        "Order Time: January 15, 2024, 2:30 PM "
        "Customer Name: Jane Doe "
        "Delivery Address: 123 Main St, Bengaluru "
        "Restaurant Name: Spice Kitchen "
        "Restaurant Address: 456 Market Road "
        "Delivery partner's Name: Raj Kumar "
        "Item Quantity Unit Price Total Price "
        "Hot Chicken Wings - 4 pcs 1 ₹189 ₹189 "
        "Taxes ₹9 "
        "Delivery charge subtotal ₹48 "
        "Restaurant Packaging Charges ₹20 "
        "Platform fee ₹5 "
        "Free Delivery with Gold (₹48) "
        "Coupon - (AMZNPAY3) () "
        "Total ₹271 "
        "Terms & Conditions (https://www.zomato.com/terms)"
    )

    signature = generate_signature(raw_text)

    assert "Hot Chicken Wings" not in signature
    assert "Spice Kitchen" not in signature
    assert "Raj Kumar" not in signature
    assert "AMZNPAY3" not in signature
    assert "Free Delivery with Gold" not in signature
    assert "₹189" not in signature
    assert "<LINE_ITEM>" in signature
    assert "<DISCOUNT>" in signature
    assert "<TABLE_HEADER>" in signature
    assert "Customer Name: <NAME>" in signature
    assert "Restaurant Name: <NAME>" in signature


def test_generate_signature_masks_integer_rupee_amounts() -> None:
    signature = generate_signature("Platform fee ₹48")

    assert "₹48" not in signature
    assert "Platform fee <AMOUNT>" in signature


def test_generate_signature_masks_invoice_number_label() -> None:
    signature = generate_signature("Invoice No.: 26LXV6R800002820")

    assert "26LXV6R800002820" not in signature
    assert "Invoice No.: <REF>" in signature


def test_generate_signature_masks_invoice_number_without_dot() -> None:
    signature = generate_signature("Invoice No: Z27KAOT025058172")

    assert "Z27KAOT025058172" not in signature
    assert "<REF>" in signature


def test_generate_signature_is_stable_across_invoice_numbers() -> None:
    a = generate_signature("Restaurant Service\nInvoice No.: AAA111\nTotal Value 10.00")
    b = generate_signature("Restaurant Service\nInvoice No.: ZZZ999\nTotal Value 99.00")

    assert a == b


def test_generate_signature_masks_three_decimal_amount() -> None:
    signature = generate_signature("Platform fee 14.90 1.34 1.34 17.582")

    assert "17.582" not in signature
    assert "<AMOUNT>" in signature


def test_generate_signature_masks_amount_in_words() -> None:
    signature = generate_signature(
        "Amount (in words): Two Hundred Forty One Rupees And Forty Five Paisa Only"
    )

    assert "Two Hundred" not in signature
    assert "Paisa" not in signature
    assert "<AMOUNT>" in signature


def test_generate_signature_strips_terms_boilerplate() -> None:
    raw_text = (
        "Restaurant Name: KFC\n"
        "Total 261.03\n"
        "Terms & Conditions (https://www.zomato.com/terms) :\n"
        "1. W.e.f. 1 January 2022, for items ordered where Eternal is obligated to "
        "raise a tax invoice on behalf of the Restaurant, it can be downloaded."
    )

    signature = generate_signature(raw_text)

    assert "Terms & Conditions" not in signature
    assert "Eternal is obligated" not in signature
    assert "Restaurant Name: <NAME>" in signature


def test_generate_signature_masks_gstin_but_stops_address_at_it() -> None:
    raw_text = (
        "Restaurant Address: 3/107, Pattandur Agrahara, Whitefield, Bangalore\n"
        "Restaurant GSTIN: 29DATPK6579N1ZC\n"
        "Invoice No.: 26LXV6R800002820"
    )

    signature = generate_signature(raw_text)

    # The address mask stops at GSTIN (label preserved) and the value is normalized.
    assert "Restaurant GSTIN: <GSTIN>" in signature
    assert "29DATPK6579N1ZC" not in signature
    assert "Pattandur" not in signature


def test_generate_signature_masks_pan_and_cin() -> None:
    signature = generate_signature(
        "Eternal PAN: AADCD4946L\nEternal CIN: L93030DL2010PLC198141"
    )

    assert "AADCD4946L" not in signature
    assert "L93030DL2010PLC198141" not in signature
    assert "PAN: <PAN>" in signature
    assert "CIN: <CIN>" in signature


def test_generate_signature_masks_bare_integer_amounts() -> None:
    signature = generate_signature("Item(s) Total 150 0 150")

    assert "150" not in signature
    assert "<AMOUNT>" in signature


def test_generate_signature_is_invariant_to_line_item_count() -> None:
    # Same layout, different order sizes (different item/amount counts) must match.
    one_item = generate_signature(
        "Service Description: Restaurant Service\n"
        "Item A 100.00\n"
        "Item(s) Total 100.00 0.00 100.00"
    )
    two_items = generate_signature(
        "Service Description: Restaurant Service\n"
        "Item A 100.00\n"
        "Item B 50.00\n"
        "Item(s) Total 150.00 0.00 150.00"
    )

    assert one_item == two_items


def test_generate_signature_is_stable_across_different_gstins() -> None:
    # Two different restaurants under the same platform layout must fingerprint alike.
    a = generate_signature(
        "Tax Invoice\nRestaurant GSTIN: 29BCTPA1575L1Z2\nItem(s) Total 10.00"
    )
    b = generate_signature(
        "Tax Invoice\nRestaurant GSTIN: 29DATPK6579N1ZC\nItem(s) Total 99.00"
    )

    assert a == b


def test_extract_vendor_key_prefers_email_domain() -> None:
    assert extract_vendor_key("Email ID: order@zomato.com") == "zomato.com"


def test_extract_vendor_key_uses_url_host_when_no_email() -> None:
    assert (
        extract_vendor_key("Please refer to https://www.zomato.com/conditions")
        == "zomato.com"
    )


def test_extract_vendor_key_falls_back_to_pan() -> None:
    assert extract_vendor_key("Eternal PAN: AADCD4946L") == "pan:AADCD4946L"


def test_extract_vendor_key_falls_back_to_gstin() -> None:
    assert (
        extract_vendor_key("Restaurant GSTIN: 29DATPK6579N1ZC")
        == "gstin:29DATPK6579N1ZC"
    )


def test_extract_vendor_key_returns_none_when_no_identifier() -> None:
    assert extract_vendor_key("Joe's Diner\nBurger 12.00\nTotal 12.00") is None
