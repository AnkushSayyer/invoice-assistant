from typing import List, Literal

from pydantic import BaseModel, ConfigDict, Field

TotalComponent = Literal["subtotal", "tax", "fees", "tip"]


class TemplateValidationRules(BaseModel):
    """Per-template rules for how invoice totals are calculated and validated."""

    model_config = ConfigDict(extra="forbid")

    validate_line_items: bool = Field(
        default=False,
        description="When true, sum(line_items) must equal subtotal (before discounts)",
    )
    total_components: List[TotalComponent] = Field(
        default_factory=lambda: ["subtotal", "tax", "fees", "tip"],
        min_length=1,
        description="Fields summed to produce the calculated claimable total",
    )
    subtract_discounts: bool = Field(
        default=True,
        description="When true, sum(discounts) is subtracted from the component total",
    )
    line_amount_includes_tax: bool = Field(
        default=False,
        description=(
            "When true, each line item's amount already includes its tax, so the "
            "line-item check compares sum(line_items) against subtotal + tax "
            "instead of subtotal alone (common on GST/VAT tax invoices)"
        ),
    )


DEFAULT_VALIDATION_RULES = TemplateValidationRules()

B2B_VALIDATION_RULES = TemplateValidationRules(
    validate_line_items=False,
    total_components=["subtotal", "tax", "fees"],
)

FOOD_DELIVERY_VALIDATION_RULES = TemplateValidationRules(
    validate_line_items=True,
    total_components=["subtotal", "tax", "fees", "tip"],
)

UBER_RIDE_VALIDATION_RULES = TemplateValidationRules(
    validate_line_items=True,
    total_components=["subtotal", "tip"],
)

FOOD_DELIVERY_WITH_DISCOUNTS_RULES = TemplateValidationRules(
    validate_line_items=True,
    total_components=["subtotal", "tax", "fees", "tip"],
    subtract_discounts=True,
)

# GST/VAT tax invoices: net item value in subtotal, taxed fees (e.g. packaging)
# in fees, and CGST+SGST in tax. Line-item totals typically include their own tax,
# so line-item validation is left off in favour of the component-based total check.
GST_TAX_INVOICE_RULES = TemplateValidationRules(
    validate_line_items=False,
    total_components=["subtotal", "tax", "fees"],
    subtract_discounts=False,
)
