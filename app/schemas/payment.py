from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


class PaymentStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"


class Currency(StrEnum):
    EUR = "eur"
    USD = "usd"
    GBP = "gbp"


class CreatePaymentRequest(BaseModel):
    amount: int = Field(
        ..., gt=0, le=99_999_999,
        description="Amount in smallest currency unit (1000 = €10.00)",
        examples=[1000],
    )
    currency: Currency = Currency.EUR
    idempotency_key: str = Field(..., min_length=1, max_length=255)
    customer_id: str | None = Field(None, max_length=255)
    description: str | None = Field(None, max_length=1000)
    metadata: dict[str, str] | None = None

    @model_validator(mode="after")
    def validate_minimum_charge(self) -> "CreatePaymentRequest":
        minimums = {"eur": 50, "usd": 50, "gbp": 30}
        min_amount = minimums.get(self.currency.value, 50)
        if self.amount < min_amount:
            raise ValueError(
                f"Minimum charge for {self.currency.value.upper()} "
                f"is {min_amount} smallest currency units"
            )
        return self


class PaymentResponse(BaseModel):
    id: UUID
    idempotency_key: str
    stripe_payment_intent_id: str | None
    amount: int
    currency: str
    status: PaymentStatus
    customer_id: str | None
    description: str | None
    risk_score: float | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class CreatePaymentResponse(BaseModel):
    payment: PaymentResponse
    client_secret: str
    idempotent: bool = False


class PaymentListResponse(BaseModel):
    payments: list[PaymentResponse]
    total: int
    page: int
    page_size: int


class RiskScoreRequest(BaseModel):
    payment_id: UUID
    amount: int
    currency: str
    customer_id: str | None = None
    metadata: dict[str, str] | None = None


class RiskScoreResponse(BaseModel):
    payment_id: UUID
    risk_score: float = Field(..., ge=0.0, le=1.0)
    risk_level: str
    flags: list[str]
    recommendation: str