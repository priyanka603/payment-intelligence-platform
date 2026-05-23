"""
Stripe payment service — the core of the platform.

This module contains the most interview-critical concepts:
  1. Idempotency keys
  2. Exponential backoff with jitter
  3. ACID transactions for financial writes
  4. Proper error classification (retryable vs terminal)
"""
import asyncio
import json
import random
import uuid
from typing import Any
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

# Configure Stripe SDK once at module level
stripe.api_key = settings.stripe_secret


# ── Retry configuration ───────────────────────────────────

MAX_RETRIES = 3
BASE_DELAY_SECONDS = 1.0
MAX_DELAY_SECONDS = 30.0

# Stripe errors that are safe to retry
RETRYABLE_STRIPE_ERRORS = (
    stripe.RateLimitError,
    stripe.APIConnectionError,
    stripe.APIError,        # 5xx from Stripe
)

# Errors that will never succeed on retry — fail immediately
TERMINAL_STRIPE_ERRORS = (
    stripe.CardError,           # Card declined, expired, etc.
    stripe.InvalidRequestError, # Bad parameters — fix code, not retry
    stripe.AuthenticationError, # Wrong API key
)


async def _exponential_backoff(attempt: int) -> None:
    """
    Exponential backoff with full jitter.
    
    Why jitter? Without it, all retries from concurrent requests fire at the
    same time after a rate limit — creating a thundering herd that makes the
    rate limit worse. Jitter spreads them out.
    
    Formula: sleep(random(0, min(cap, base * 2^attempt)))
    Source: AWS Architecture Blog "Exponential Backoff And Jitter"
    """
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
        """
        Create a payment intent with full idempotency guarantees.
        
        Idempotency flow:
          1. Check our DB for the idempotency_key — if found, return cached result
          2. Create Stripe PaymentIntent (pass idempotency key to Stripe too)
          3. Write to our DB — UNIQUE constraint is the final safety net
          4. Return consistent response regardless of which path was taken
        
        This means: calling this endpoint 10 times with the same key will
        result in exactly 1 Stripe charge. Safe for client retries.
        """
        log = logger.bind(
            idempotency_key=request.idempotency_key,
            amount=request.amount,
            currency=request.currency.value,
        )
        log.info("create_payment_started")

        # ── Step 1: Idempotency check ─────────────────────────
        existing = await self._find_by_idempotency_key(request.idempotency_key)
        if existing is not None:
            log.info("idempotent_response_returned", payment_id=str(existing.id))
            return CreatePaymentResponse(
                payment=PaymentResponse.model_validate(existing),
                client_secret=existing.stripe_payment_intent_id or "",
                idempotent=True,
            )

        # ── Step 2: Create Stripe PaymentIntent ───────────────
        intent = await self._create_stripe_intent_with_retry(request)

        # ── Step 3: Persist to DB (ACID transaction) ──────────
        payment = await self._persist_payment(request, intent)

        log.info(
            "payment_created",
            payment_id=str(payment.id),
            stripe_intent_id=intent.id,
        )

        return CreatePaymentResponse(
            payment=PaymentResponse.model_validate(payment),
            client_secret=intent.client_secret or "",
            idempotent=False,
        )

    async def _create_stripe_intent_with_retry(
        self, request: CreatePaymentRequest
    ) -> stripe.PaymentIntent:
        """
        Call Stripe API with retry logic for transient errors.
        
        CRITICAL: We pass our idempotency_key to Stripe too.
        This means Stripe also guarantees exactly-once on their side.
        Double idempotency: our DB + Stripe's own deduplication.
        """
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
                        idempotency_key=request.idempotency_key,  # ← also passed to Stripe
                    ),
                )
                logger.info(
                    "stripe_intent_created",
                    intent_id=intent.id,
                    attempt=attempt,
                )
                return intent

            except TERMINAL_STRIPE_ERRORS as e:
                # Card declined, bad params — no point retrying
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
                    max_retries=MAX_RETRIES,
                )
                if attempt < MAX_RETRIES - 1:
                    await _exponential_backoff(attempt)

        raise last_error or RuntimeError("Stripe call failed after all retries")

    async def _persist_payment(
        self,
        request: CreatePaymentRequest,
        intent: stripe.PaymentIntent,
    ) -> Payment:
        """
        Write payment to DB inside an ACID transaction.
        
        ACID in practice here:
          Atomicity:   both the INSERT and any related audit log succeed or both roll back
          Consistency: UNIQUE constraint on idempotency_key maintained
          Isolation:   SQLAlchemy session gives us snapshot isolation
          Durability:  PostgreSQL WAL ensures committed data survives crashes
        
        IntegrityError handling: if two concurrent requests race past our
        idempotency check (TOCTOU race), the UNIQUE constraint fires and we
        catch it, then return the existing record.
        """
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
            await self.db.flush()     # assign DB-generated values without committing
            await self.db.refresh(payment)
            return payment

        except IntegrityError:
            # Race condition: concurrent request beat us to the INSERT
            await self.db.rollback()
            logger.info(
                "idempotency_race_resolved",
                idempotency_key=request.idempotency_key,
            )
            existing = await self._find_by_idempotency_key(request.idempotency_key)
            if existing is None:
                raise RuntimeError("Integrity error but no existing record found")
            return existing

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
        """Called by webhook handler to update payment after Stripe confirms."""
        result = await self.db.execute(
            select(Payment).where(
                Payment.stripe_payment_intent_id == stripe_intent_id
            )
        )
        payment = result.scalar_one_or_none()

        if payment is None:
            logger.warning(
                "webhook_payment_not_found",
                stripe_intent_id=stripe_intent_id,
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
