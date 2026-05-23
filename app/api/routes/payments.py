import uuid
import stripe
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.database import get_db
from app.schemas.payment import CreatePaymentRequest, CreatePaymentResponse, PaymentResponse
from app.services.stripe.payment_service import PaymentService
from app.services.stripe.webhook_handler import WebhookHandler

router = APIRouter(prefix="/payments", tags=["payments"])
logger = get_logger(__name__)


@router.post(
    "",
    response_model=CreatePaymentResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_payment(
    request: CreatePaymentRequest,
    db: AsyncSession = Depends(get_db),
) -> CreatePaymentResponse:
    service = PaymentService(db)
    try:
        return await service.create_payment(request)
    except stripe.CardError as e:
        err = e.error
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={"code": err.code, "message": err.message, "decline_code": err.decline_code},
        )
    except stripe.InvalidRequestError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.error("payment_creation_failed", error=str(e), exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Payment creation failed. Please retry.",
        )


@router.get("/{payment_id}", response_model=PaymentResponse)
async def get_payment(
    payment_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> PaymentResponse:
    service = PaymentService(db)
    payment = await service.get_payment(payment_id)
    if payment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Payment {payment_id} not found",
        )
    return PaymentResponse.model_validate(payment)


@router.post("/webhooks", include_in_schema=False, status_code=status.HTTP_200_OK)
async def handle_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
    stripe_signature: str = Header(..., alias="Stripe-Signature"),
) -> dict:
    payload = await request.body()
    handler = WebhookHandler(db)

    try:
        event = handler.verify_and_parse(payload, stripe_signature)
    except stripe.SignatureVerificationError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid signature")

    try:
        return await handler.process_event(event)
    except Exception as e:
        logger.error("webhook_processing_failed", error=str(e), exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Webhook processing failed")