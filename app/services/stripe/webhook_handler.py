import json

import stripe
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.models.webhook_event import WebhookEvent
from app.schemas.payment import PaymentStatus
from app.services.stripe.payment_service import PaymentService

logger = get_logger(__name__)
settings = get_settings()


class WebhookHandler:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.payment_service = PaymentService(db)

    def verify_and_parse(self, payload: bytes, sig_header: str) -> stripe.Event:
        try:
            event = stripe.Webhook.construct_event(
                payload,
                sig_header,
                settings.stripe_webhook_secret_value,
                tolerance=300,
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
        existing = await self._find_event(event.id)
        if existing is not None:
            logger.info("webhook_already_processed", event_id=event.id)
            return {"status": "already_processed", "event_id": event.id}

        result = await self._route_event(event)
        await self._record_event(event)
        return result

    async def _route_event(self, event: stripe.Event) -> dict:
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
        return {"status": "processed", "event_type": event.type}

    async def _handle_payment_failed(self, event: stripe.Event) -> dict:
        intent = event.data.object
        await self.payment_service.update_payment_status(
            stripe_intent_id=intent.id,
            new_status=PaymentStatus.FAILED,
        )
        return {"status": "processed", "event_type": event.type}

    async def _handle_payment_cancelled(self, event: stripe.Event) -> dict:
        intent = event.data.object
        await self.payment_service.update_payment_status(
            stripe_intent_id=intent.id,
            new_status=PaymentStatus.CANCELLED,
        )
        return {"status": "processed", "event_type": event.type}

    async def _handle_dispute_created(self, event: stripe.Event) -> dict:
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