import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.payment import CreatePaymentRequest, Currency, PaymentStatus
from app.services.stripe.payment_service import PaymentService


@pytest.fixture
def mock_db():
    db = AsyncMock(spec=AsyncSession)
    db.execute = AsyncMock()
    db.flush = AsyncMock()
    db.refresh = AsyncMock()
    db.add = MagicMock()
    db.rollback = AsyncMock()
    return db


@pytest.fixture
def payment_request():
    return CreatePaymentRequest(
        amount=1000,
        currency=Currency.EUR,
        idempotency_key=f"test-{uuid.uuid4()}",
        customer_id="cust_test_123",
        description="Test payment",
    )


class TestCreatePayment:
    async def test_returns_existing_on_duplicate_key(
        self, mock_db, payment_request
    ):
        from app.db.models.payment import Payment

        now = datetime.now(UTC)

        existing_payment = Payment(
            id=uuid.uuid4(),
            idempotency_key=payment_request.idempotency_key,
            stripe_payment_intent_id="pi_existing_123",
            amount=1000,
            currency="eur",
            status=PaymentStatus.PENDING.value,
            created_at=now,
            updated_at=now,
        )
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing_payment
        mock_db.execute.return_value = mock_result

        with patch(
            "app.services.stripe.payment_service.stripe.PaymentIntent.create"
        ) as mock_create:
            service = PaymentService(mock_db)
            response = await service.create_payment(payment_request)

        mock_create.assert_not_called()
        assert response.idempotent is True

    async def test_minimum_amount_validation(self):
        with pytest.raises(ValueError, match="Minimum charge"):
            CreatePaymentRequest(
                amount=10,
                currency=Currency.EUR,
                idempotency_key="test-key",
            )