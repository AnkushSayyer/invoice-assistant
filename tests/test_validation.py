from datetime import date
from decimal import Decimal

from app.schemas.extraction import Discount, InvoiceExtraction, LineItem
from app.schemas.validation_rules import (
    B2B_VALIDATION_RULES,
    FOOD_DELIVERY_VALIDATION_RULES,
    GST_TAX_INVOICE_RULES,
    UBER_RIDE_VALIDATION_RULES,
)
from app.services.validation import (
    compute_calculated_total,
    derive_validation_rules,
    validate_invoice_math,
)


def _invoice(**overrides: object) -> InvoiceExtraction:
    defaults = {
        "vendor": "Acme Corp",
        "invoice_number": "INV-1001",
        "date": date(2024, 1, 15),
        "subtotal": Decimal("100.00"),
        "tax": Decimal("8.00"),
        "fees": Decimal("2.00"),
        "total": Decimal("110.00"),
    }
    defaults.update(overrides)
    return InvoiceExtraction(**defaults)


def test_validate_invoice_math_accepts_balanced_totals() -> None:
    result = validate_invoice_math(
        _invoice(),
        B2B_VALIDATION_RULES,
        claimed_amount=Decimal("110.00"),
    )

    assert result.is_valid is True
    assert result.calculated_total == Decimal("110.00")


def test_validate_invoice_math_accepts_zero_tax_and_fees() -> None:
    result = validate_invoice_math(
        _invoice(tax=Decimal("0"), fees=Decimal("0"), total=Decimal("100.00")),
        B2B_VALIDATION_RULES,
        claimed_amount=Decimal("100.00"),
    )

    assert result.is_valid is True


def test_validate_invoice_math_accepts_one_cent_tolerance() -> None:
    result = validate_invoice_math(
        _invoice(total=Decimal("110.01")),
        B2B_VALIDATION_RULES,
        claimed_amount=Decimal("110.01"),
    )

    assert result.is_valid is True


def test_validate_invoice_math_rejects_mismatched_total() -> None:
    result = validate_invoice_math(_invoice(total=Decimal("120.00")), B2B_VALIDATION_RULES)

    assert result.is_valid is False
    assert any("template formula" in error for error in result.errors)


def test_validate_invoice_math_rejects_mismatched_claimed_amount() -> None:
    result = validate_invoice_math(
        _invoice(),
        B2B_VALIDATION_RULES,
        claimed_amount=Decimal("99.00"),
    )

    assert result.is_valid is False
    assert any("claimed amount" in error for error in result.errors)


def test_validate_invoice_math_accepts_food_order_with_line_items() -> None:
    result = validate_invoice_math(
        _invoice(
            vendor="Joe's Diner",
            line_items=[
                LineItem(description="Burger", quantity=Decimal("1"), amount=Decimal("12.00")),
                LineItem(description="Fries", quantity=Decimal("1"), amount=Decimal("4.50")),
            ],
            subtotal=Decimal("16.50"),
            tax=Decimal("1.32"),
            fees=Decimal("2.99"),
            tip=Decimal("3.00"),
            total=Decimal("23.81"),
        ),
        FOOD_DELIVERY_VALIDATION_RULES,
        claimed_amount=Decimal("23.81"),
    )

    assert result.is_valid is True
    assert result.calculated_total == Decimal("23.81")


def test_validate_invoice_math_accepts_discounts_and_coupons() -> None:
    result = validate_invoice_math(
        _invoice(
            vendor="Uber Eats",
            line_items=[
                LineItem(description="Pad Thai", amount=Decimal("14.00")),
                LineItem(description="Spring Rolls", amount=Decimal("6.00")),
            ],
            discounts=[
                Discount(
                    description="Uber One promo",
                    code="EATS25",
                    amount=Decimal("5.00"),
                )
            ],
            subtotal=Decimal("20.00"),
            tax=Decimal("1.20"),
            fees=Decimal("3.49"),
            tip=Decimal("2.00"),
            total=Decimal("21.69"),
        ),
        FOOD_DELIVERY_VALIDATION_RULES,
        claimed_amount=Decimal("21.69"),
    )

    assert result.is_valid is True


def test_validate_invoice_math_rejects_line_items_that_do_not_match_subtotal() -> None:
    result = validate_invoice_math(
        _invoice(
            line_items=[
                LineItem(description="Burger", amount=Decimal("12.00")),
            ],
            discounts=[
                Discount(description="Coupon", code="SAVE2", amount=Decimal("2.00")),
            ],
            subtotal=Decimal("20.00"),
            tax=Decimal("0"),
            fees=Decimal("0"),
            total=Decimal("20.00"),
        ),
        FOOD_DELIVERY_VALIDATION_RULES,
    )

    assert result.is_valid is False
    assert any("sum of line items" in error for error in result.errors)


def test_validate_invoice_math_accepts_uber_trip_with_fare_lines() -> None:
    invoice = _invoice(
        vendor="Uber",
        invoice_number="TRIP-8831",
        line_items=[
            LineItem(description="Trip fare", amount=Decimal("18.45")),
            LineItem(description="Booking fee", amount=Decimal("2.00")),
        ],
        subtotal=Decimal("20.45"),
        tax=Decimal("0.00"),
        fees=Decimal("0.00"),
        tip=Decimal("4.00"),
        total=Decimal("24.45"),
    )

    result = validate_invoice_math(
        invoice,
        UBER_RIDE_VALIDATION_RULES,
        claimed_amount=Decimal("24.45"),
    )

    assert result.is_valid is True
    assert compute_calculated_total(invoice, UBER_RIDE_VALIDATION_RULES) == Decimal("24.45")


def test_compute_calculated_total_subtracts_discounts() -> None:
    invoice = _invoice(
        subtotal=Decimal("20.00"),
        tax=Decimal("1.20"),
        fees=Decimal("3.49"),
        tip=Decimal("2.00"),
        discounts=[
            Discount(description="Promo", amount=Decimal("5.00")),
        ],
        total=Decimal("21.69"),
    )

    calculated = compute_calculated_total(invoice, FOOD_DELIVERY_VALIDATION_RULES)

    assert calculated == Decimal("21.69")


def test_compute_calculated_total_can_skip_discount_subtraction() -> None:
    invoice = _invoice(
        subtotal=Decimal("15.00"),
        tax=Decimal("1.20"),
        fees=Decimal("3.49"),
        tip=Decimal("2.00"),
        discounts=[Discount(description="Promo", amount=Decimal("5.00"))],
        total=Decimal("21.69"),
    )
    rules = FOOD_DELIVERY_VALIDATION_RULES.model_copy(update={"subtract_discounts": False})

    calculated = compute_calculated_total(invoice, rules)

    assert calculated == Decimal("21.69")


def test_b2b_rules_exclude_tip_from_calculated_total() -> None:
    invoice = _invoice(tip=Decimal("5.00"), total=Decimal("115.00"))

    calculated = compute_calculated_total(invoice, B2B_VALIDATION_RULES)

    assert calculated == Decimal("110.00")


def _gst_invoice() -> InvoiceExtraction:
    """A GST tax invoice whose line totals already include CGST + SGST."""
    return _invoice(
        vendor="Abooz Cafe",
        invoice_number="26LXV6R800002820",
        line_items=[
            LineItem(description="Egg Kothu Parotta", amount=Decimal("176.40")),
            LineItem(description="Lime Juice", amount=Decimal("53.55")),
        ],
        subtotal=Decimal("219.00"),
        tax=Decimal("10.95"),
        fees=Decimal("0.00"),
        total=Decimal("229.95"),
    )


def test_line_amount_includes_tax_accepts_tax_inclusive_line_totals() -> None:
    rules = FOOD_DELIVERY_VALIDATION_RULES.model_copy(
        update={"line_amount_includes_tax": True, "total_components": ["subtotal", "tax"]}
    )

    result = validate_invoice_math(_gst_invoice(), rules)

    assert result.is_valid is True
    assert result.calculated_total == Decimal("229.95")


def test_line_amount_includes_tax_rejects_when_lines_omit_tax() -> None:
    # Same invoice but flag off: sum(176.40 + 53.55) != subtotal 219.00 -> error.
    rules = FOOD_DELIVERY_VALIDATION_RULES.model_copy(
        update={"line_amount_includes_tax": False, "total_components": ["subtotal", "tax"]}
    )

    result = validate_invoice_math(_gst_invoice(), rules)

    assert result.is_valid is False
    assert any("sum of line items does not match subtotal" in e for e in result.errors)


def test_derive_rules_detects_tax_inclusive_line_items() -> None:
    rules = derive_validation_rules(_gst_invoice())

    assert rules.validate_line_items is True
    assert rules.line_amount_includes_tax is True


def test_derive_rules_marks_net_line_items_as_tax_exclusive() -> None:
    invoice = _invoice(
        line_items=[LineItem(description="Item", amount=Decimal("100.00"))],
        subtotal=Decimal("100.00"),
        tax=Decimal("8.00"),
        fees=Decimal("2.00"),
        total=Decimal("110.00"),
    )

    rules = derive_validation_rules(invoice)

    assert rules.validate_line_items is True
    assert rules.line_amount_includes_tax is False


def test_derive_rules_skips_line_check_when_ambiguous() -> None:
    # Lines match neither subtotal nor subtotal + tax (separately taxed packaging fee),
    # so line validation is disabled and the component total still reconciles.
    invoice = _invoice(
        line_items=[LineItem(description="Item", amount=Decimal("176.40"))],
        subtotal=Decimal("219.00"),
        tax=Decimal("11.50"),
        fees=Decimal("10.95"),
        total=Decimal("241.45"),
    )

    rules = derive_validation_rules(invoice)
    result = validate_invoice_math(invoice, rules)

    assert rules.validate_line_items is False
    assert result.is_valid is True
    assert result.calculated_total == Decimal("241.45")


def test_multi_invoice_rounding_fails_at_default_tolerance() -> None:
    # Combined 3-invoice extraction: components sum to 919.22 but printed total is
    # 919.23 due to per-invoice GST rounding -> rejected at the default 0.01 slack.
    invoice = _invoice(
        line_items=[
            LineItem(description="Veg Manchurian Biryani", amount=Decimal("370")),
            LineItem(description="Paneer Biryani", amount=Decimal("425")),
            LineItem(description="Fee for delivery services", amount=Decimal("30")),
        ],
        subtotal=Decimal("825"),
        tax=Decimal("49.32"),
        fees=Decimal("44.9"),
        total=Decimal("919.232"),
    )

    result = validate_invoice_math(invoice, GST_TAX_INVOICE_RULES)

    assert result.is_valid is False
    assert result.calculated_total == Decimal("919.22")


def test_multi_invoice_rounding_passes_with_scaled_tolerance() -> None:
    invoice = _invoice(
        line_items=[
            LineItem(description="Veg Manchurian Biryani", amount=Decimal("370")),
            LineItem(description="Paneer Biryani", amount=Decimal("425")),
            LineItem(description="Fee for delivery services", amount=Decimal("30")),
        ],
        subtotal=Decimal("825"),
        tax=Decimal("49.32"),
        fees=Decimal("44.9"),
        total=Decimal("919.232"),
    )

    # Three bundled invoices -> tolerance widened to 0.03 absorbs the rounding drift.
    result = validate_invoice_math(
        invoice,
        GST_TAX_INVOICE_RULES,
        claimed_amount=Decimal("919.23"),
        tolerance=Decimal("0.03"),
    )

    assert result.is_valid is True
    assert result.calculated_total == Decimal("919.22")


def test_gst_tax_invoice_rules_validate_subtotal_tax_fees() -> None:
    invoice = _invoice(
        subtotal=Decimal("219.00"),
        tax=Decimal("11.50"),
        fees=Decimal("10.95"),
        total=Decimal("241.45"),
    )

    result = validate_invoice_math(
        invoice,
        GST_TAX_INVOICE_RULES,
        claimed_amount=Decimal("241.45"),
    )

    assert result.is_valid is True
    assert result.calculated_total == Decimal("241.45")
