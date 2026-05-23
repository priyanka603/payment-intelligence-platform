import asyncio
import json
import random
import uuid

import stripe
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.models.payment import Payment
from app.schemas.payment import (
    CreatePaymentRequest,
    CreatePaymentResponse,
    PaymentResponse,
    PaymentStatus,
)

logger = get_logger(__name__)
settings = get_settings()

stripe.api_key = settings.stripe_secret

MAX_RETRIES = 3
BASE_DELAY_SECONDS = 1.0
MAX_DELAY_SECONDS = 30.0

RETRYABLE_STRIPE_ERRORS = (
    stripe.RateLimitError,
    stripe.APIConnectionError,
    stripe.APIError,
)

TERMINAL_STRIPE_ERRORS = (
    stripe.CardError,
    stripe.InvalidRequestError,
    stripe.AuthenticationError,
)


async def _exponential_backoff(attempt: int) -> None:
    delay = min(MAX_DELAY_SECONDS, BASE_DELAY_SECONDS * (2 ** attempt))
    jitter = random.uniform(0, delay)
    logger.info("retry_backoff", attempt=attempt, delay_seconds=round(jitter, 2))
    await asyncio.sleep(jitter)


class PaymentService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def create_payment(
        self, request: CreatePaymentRequest
    ) -> CreatePaymentResponse:
        log = logger.bind(
            idempotency_key=request.idempotency_key,
            amount=request.amount,
            currency=request.currency.value,
        )
        log.info("create_payment_started")

        existing = await self._find_by_idempotency_key(request.idempotency_key)
        if existing is not None:
            log.info("idempotent_response_returned", payment_id=str(existing.id))
            return CreatePaymentResponse(
                payment=PaymentResponse.model_validate(existing),
                client_secret=existing.stripe_payment_intent_id or "",
                idempotent=True,
            )

        intent = await self._create_stripe_intent_with_retry(request)
        payment = await self._persist_payment(request, intent)

        await self._apply_fraud_score(payment, request)

        log.info(
            "payment_created",
            payment_id=str(payment.id),
            stripe_intent_id=intent.id,
            risk_score=payment.risk_score,
        )

        return CreatePaymentResponse(
            payment=PaymentResponse.model_validate(payment),
            client_secret=intent.client_secret or "",
            idempotent=False,
        )

    async def _create_stripe_intent_with_retry(
        self, request: CreatePaymentRequest
    ) -> stripe.PaymentIntent:
        last_error: Exception | None = None

        for attempt in range(MAX_RETRIES):
            try:
                intent = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: stripe.PaymentIntent.create(
                        amount=request.amount,
                        currency=request.currency.value,
                        metadata={
                            "customer_id": request.customer_id or "",
                            "description": request.description or "",
                            **(request.metadata or {}),
                        },
                        idempotency_key=request.idempotency_key,
                    ),
                )
                logger.info(
                    "stripe_intent_created", intent_id=intent.id, attempt=attempt
                )
                return intent

            except TERMINAL_STRIPE_ERRORS as e:
                logger.warning(
                    "stripe_terminal_error",
                    error_type=type(e).__name__,
                    message=str(e),
                )
                raise

            except RETRYABLE_STRIPE_ERRORS as e:
                last_error = e
                logger.warning(
                    "stripe_retryable_error",
                    error_type=type(e).__name__,
                    attempt=attempt,
                )
                if attempt < MAX_RETRIES - 1:
                    await _exponential_backoff(attempt)

        raise last_error or RuntimeError("Stripe call failed after all retries")

    async def _persist_payment(
        self,
        request: CreatePaymentRequest,
        intent: stripe.PaymentIntent,
    ) -> Payment:
        payment = Payment(
            idempotency_key=request.idempotency_key,
            stripe_payment_intent_id=intent.id,
            amount=request.amount,
            currency=request.currency.value,
            status=PaymentStatus.PENDING.value,
            customer_id=request.customer_id,
            description=request.description,
            metadata_json=json.dumps(request.metadata) if request.metadata else None,
        )

        try:
            self.db.add(payment)
            await self.db.flush()
            await self.db.refresh(payment)
            return payment

        except IntegrityError as e:
            await self.db.rollback()
            logger.info(
                "idempotency_race_resolved",
                idempotency_key=request.idempotency_key,
            )
            existing = await self._find_by_idempotency_key(request.idempotency_key)
            if existing is None:
                raise RuntimeError(
                    "Integrity error but no existing record found"
                ) from e
            return existing

    async def _apply_fraud_score(
        self,
        payment: Payment,
        request: CreatePaymentRequest,
    ) -> None:
        """
        Score the payment for fraud and store the result.
        Never raises — fraud scoring failure must never block a payment.
        """
        from app.services.ai.fraud_detection import FraudDetectionService

        fraud_service = FraudDetectionService()
        assessment = await fraud_service.score_payment(
            amount=request.amount,
            currency=request.currency.value,
            customer_id=request.customer_id,
            description=request.description,
            metadata=request.metadata,
        )

        payment.risk_score = assessment.risk_score
        payment.risk_flags = json.dumps(assessment.flags)

        await self.db.flush()
        await self.db.refresh(payment)

    async def _find_by_idempotency_key(self, key: str) -> Payment | None:
        result = await self.db.execute(
            select(Payment).where(Payment.idempotency_key == key)
        )
        return result.scalar_one_or_none()

    async def get_payment(self, payment_id: uuid.UUID) -> Payment | None:
        result = await self.db.execute(
            select(Payment).where(Payment.id == payment_id)
        )
        return result.scalar_one_or_none()

    async def update_payment_status(
        self,
        stripe_intent_id: str,
        new_status: PaymentStatus,
        stripe_charge_id: str | None = None,
    ) -> Payment | None:
        result = await self.db.execute(
            select(Payment).where(
                Payment.stripe_payment_intent_id == stripe_intent_id
            )
        )
        payment = result.scalar_one_or_none()

        if payment is None:
            logger.warning(
                "webhook_payment_not_found", stripe_intent_id=stripe_intent_id
            )
            return None

        payment.status = new_status.value
        if stripe_charge_id:
            payment.stripe_charge_id = stripe_charge_id

        logger.info(
            "payment_status_updated",
            payment_id=str(payment.id),
            new_status=new_status.value,
        )
        return payment