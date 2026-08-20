from datetime import date as Date
from decimal import Decimal
from typing import Annotated, Any, List, Optional

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field


def _to_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float, str)):
        return Decimal(str(value))
    raise TypeError(f"Cannot convert {type(value).__name__} to Decimal")


def _to_date(value: Any) -> Date:
    if isinstance(value, Date):
        return value
    if isinstance(value, str):
        return Date.fromisoformat(value)
    raise TypeError(f"Cannot convert {type(value).__name__} to date")


Money = Annotated[Decimal, BeforeValidator(_to_decimal)]
InvoiceDate = Annotated[Date, BeforeValidator(_to_date)]


class LineItem(BaseModel):
    """A single line on a food order, delivery, or ride-hailing receipt."""

    model_config = ConfigDict(extra="forbid")

    description: str = Field(..., description="Item or fare component name")
    quantity: Money = Field(default=Decimal("1"), ge=0, description="Quantity ordered")
    unit_price: Optional[Money] = Field(
        default=None, ge=0, description="Per-unit price when shown on the receipt"
    )
    amount: Money = Field(..., ge=0, description="Line total amount")


class Discount(BaseModel):
    """A discount, promotion, or coupon applied to the order."""

    model_config = ConfigDict(extra="forbid")

    description: str = Field(..., description="Discount or promotion label")
    code: Optional[str] = Field(
        default=None, description="Coupon or promo code when printed on the receipt"
    )
    amount: Money = Field(
        ..., ge=0, description="Discount amount as a positive number"
    )


class InvoiceExtraction(BaseModel):
    """Structured invoice fields returned by the LLM extractor."""

    model_config = ConfigDict(extra="forbid")

    vendor: str = Field(
        ..., description="Vendor, restaurant, or platform name (e.g. Uber Eats, DoorDash)"
    )
    invoice_number: str = Field(
        ..., description="Order, trip, or receipt identifier"
    )
    date: InvoiceDate = Field(..., description="Invoice or order date")
    line_items: List[LineItem] = Field(
        default_factory=list,
        description="Food items, fare components, or other per-line charges",
    )
    discounts: List[Discount] = Field(
        default_factory=list,
        description="Coupons, promos, and other discounts applied to the order",
    )
    subtotal: Money = Field(
        ...,
        ge=0,
        description="Subtotal before discounts and before tax, fees, and tip",
    )
    tax: Money = Field(default=Decimal("0"), ge=0, description="Tax amount")
    fees: Money = Field(
        default=Decimal("0"),
        ge=0,
        description="Delivery, service, booking, or platform fees",
    )
    tip: Money = Field(
        default=Decimal("0"), ge=0, description="Tip or gratuity amount"
    )
    total: Money = Field(..., ge=0, description="Grand total amount charged")
