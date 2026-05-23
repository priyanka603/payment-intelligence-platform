"""
Stripe webhook handler.

Security model:
  Stripe signs every webhook with HMAC-SHA256 using your endpoint secret.
  We MUST verify this signature before processing anything.
  Without verification, any attacker can POST fake charge.succeeded events
  to your endpoint and get their payment marked as paid for free.

Idempotency model:
  Stripe retries webhooks for up to 72 hours on non-2xx responses.
  We store processed event IDs in webhook_events table.
  Check before processing → prevents double-crediting on retry.
"""
import json
import stripe
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.models.webhook_event import WebhookEvent
from app.services.stripe.payment_service import PaymentService
from app.schemas.payment import PaymentStatus

logger = get_logger(__name__)
settings = get_settings()


class WebhookHandler:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.payment_service = PaymentService(db)

    def verify_and_parse(self, payload: bytes, sig_header: str) -> stripe.Event:
        """
        Verify Stripe webhook signature.
        
        Stripe's signature scheme:
          1. Stripe timestamps the payload and computes HMAC-SHA256(timestamp.payload)
          2. The Stripe-Signature header contains: t=timestamp,v1=signature
          3. We recompute with our webhook secret and compare
          4. Tolerance check: reject events older than 300 seconds (replay protection)
        
        stripe.Webhook.construct_event() does all of this — never skip it.
        """
        try:
            event = stripe.Webhook.construct_event(
                payload,
                sig_header,
                settings.stripe_webhook_secret_value,
                tolerance=300,  # 5-minute replay window
            )
            logger.info(
                "webhook_signature_verified",
                event_type=event.type,
                event_id=event.id,
            )
            return event
        except stripe.SignatureVerificationError as e:
            logger.warning("webhook_signature_invalid", error=str(e))
            raise
        except ValueError as e:
            logger.warning("webhook_payload_invalid", error=str(e))
            raise

    async def process_event(self, event: stripe.Event) -> dict:
        """
        Idempotently process a Stripe event.
        
        Pattern:
          1. Check webhook_events table for this event_id
          2. If already processed → return cached ack (still 200, not 4xx)
          3. If new → process + record in same transaction
        """
        # ── Idempotency check ─────────────────────────────────
        existing = await self._find_event(event.id)
        if existing is not None:
            logger.info("webhook_already_processed", event_id=event.id)
            return {"status": "already_processed", "event_id": event.id}

        # ── Route to handler ──────────────────────────────────
        result = await self._route_event(event)

        # ── Record event (same transaction as business logic) ─
        await self._record_event(event)

        return result

    async def _route_event(self, event: stripe.Event) -> dict:
        """Dispatch to the right handler based on event type."""
        handlers = {
            "payment_intent.succeeded": self._handle_payment_succeeded,
            "payment_intent.payment_failed": self._handle_payment_failed,
            "payment_intent.canceled": self._handle_payment_cancelled,
            "charge.dispute.created": self._handle_dispute_created,
        }

        handler = handlers.get(event.type)
        if handler is None:
            logger.info("webhook_event_unhandled", event_type=event.type)
            return {"status": "unhandled", "event_type": event.type}

        return await handler(event)

    async def _handle_payment_succeeded(self, event: stripe.Event) -> dict:
        intent = event.data.object
        await self.payment_service.update_payment_status(
            stripe_intent_id=intent.id,
            new_status=PaymentStatus.SUCCEEDED,
            stripe_charge_id=intent.get("latest_charge"),
        )
        logger.info("payment_succeeded_processed", intent_id=intent.id)
        return {"status": "processed", "event_type": event.type}

    async def _handle_payment_failed(self, event: stripe.Event) -> dict:
        intent = event.data.object
        await self.payment_service.update_payment_status(
            stripe_intent_id=intent.id,
            new_status=PaymentStatus.FAILED,
        )
        logger.info("payment_failed_processed", intent_id=intent.id)
        return {"status": "processed", "event_type": event.type}

    async def _handle_payment_cancelled(self, event: stripe.Event) -> dict:
        intent = event.data.object
        await self.payment_service.update_payment_status(
            stripe_intent_id=intent.id,
            new_status=PaymentStatus.CANCELLED,
        )
        return {"status": "processed", "event_type": event.type}

    async def _handle_dispute_created(self, event: stripe.Event) -> dict:
        """
        Dispute (chargeback) — this is where the AI layer integrates later.
        For now, log it for review.
        """
        dispute = event.data.object
        logger.warning(
            "dispute_created",
            dispute_id=dispute.id,
            amount=dispute.amount,
            reason=dispute.reason,
        )
        return {"status": "dispute_logged", "dispute_id": dispute.id}

    async def _find_event(self, event_id: str) -> WebhookEvent | None:
        result = await self.db.execute(
            select(WebhookEvent).where(WebhookEvent.stripe_event_id == event_id)
        )
        return result.scalar_one_or_none()

    async def _record_event(self, event: stripe.Event) -> None:
        record = WebhookEvent(
            stripe_event_id=event.id,
            event_type=event.type,
            payload_json=json.dumps(dict(event)),
        )
        self.db.add(record)
        await self.db.flush()
