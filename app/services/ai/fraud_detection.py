import json

from pydantic import BaseModel

from app.core.logging import get_logger

logger = get_logger(__name__)


class FraudAssessment(BaseModel):
    risk_score: float
    risk_level: str
    flags: list[str]
    recommendation: str


class FraudDetectionService:
    """
    Rule-based fraud scoring engine.
    Scores payments deterministically based on amount thresholds,
    missing customer data, and suspicious patterns.
    Designed to be swapped with an LLM-based scorer in production
    — same interface, same FraudAssessment return type.
    """

    async def score_payment(
        self,
        amount: int,
        currency: str,
        customer_id: str | None = None,
        description: str | None = None,
        metadata: dict | None = None,
    ) -> FraudAssessment:
        score = 0.0
        flags = []

        # High amount
        if amount > 1_000_000:
            score += 0.5
            flags.append("high_amount")
        elif amount > 100_000:
            score += 0.2
            flags.append("elevated_amount")

        # Anonymous customer
        if not customer_id:
            score += 0.2
            flags.append("anonymous_customer")

        # Suspicious description
        suspicious_words = ["test", "dummy", "fake", "xxx", "asdf"]
        if description:
            if any(word in description.lower() for word in suspicious_words):
                score += 0.2
                flags.append("suspicious_description")

        # Round number — common in card testing
        if amount % 100_000 == 0:
            score += 0.1
            flags.append("round_amount")

        score = min(round(score, 2), 1.0)

        if score <= 0.3:
            risk_level = "low"
            recommendation = "approve"
        elif score <= 0.7:
            risk_level = "medium"
            recommendation = "review"
        else:
            risk_level = "high"
            recommendation = "block"

        logger.info(
            "fraud_score_computed",
            risk_score=score,
            risk_level=risk_level,
            flags=flags,
        )

        return FraudAssessment(
            risk_score=score,
            risk_level=risk_level,
            flags=flags,
            recommendation=recommendation,
        )