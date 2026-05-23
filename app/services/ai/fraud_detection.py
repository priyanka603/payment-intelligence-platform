import json
import traceback

from langchain_core.prompts import ChatPromptTemplate
from langchain_google_genai import ChatGoogleGenerativeAI
from pydantic import BaseModel

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)
settings = get_settings()


class FraudAssessment(BaseModel):
    risk_score: float
    risk_level: str
    flags: list[str]
    recommendation: str


FRAUD_PROMPT = ChatPromptTemplate.from_messages([
    (
        "system",
        """You are a payments fraud detection system. Analyze the payment and return ONLY a JSON object.

Rules:
- risk_score: float between 0.0 (no risk) and 1.0 (certain fraud)
- risk_level: "low" (0.0-0.3), "medium" (0.31-0.7), "high" (0.71-1.0)
- flags: list of specific risk signals observed, empty list if none
- recommendation: one of "approve", "review", "block"

Respond ONLY with valid JSON, no explanation, no markdown, no backticks.

Example:
{{"risk_score": 0.15, "risk_level": "low", "flags": [], "recommendation": "approve"}}""",
    ),
    (
        "human",
        """Analyze this payment for fraud risk:

Amount: {amount} {currency}
Customer ID: {customer_id}
Description: {description}
Metadata: {metadata}

Consider:
- Unusually high amounts for the currency
- Missing or suspicious customer ID
- Suspicious patterns in description or metadata
- Round numbers that suggest card testing

Return JSON only.""",
    ),
])


class FraudDetectionService:
    def __init__(self) -> None:
        self.llm = ChatGoogleGenerativeAI(
            model="gemini-2.0-flash",
            google_api_key=settings.google_key,
            temperature=0,
        )
        self.chain = FRAUD_PROMPT | self.llm

    async def score_payment(
        self,
        amount: int,
        currency: str,
        customer_id: str | None = None,
        description: str | None = None,
        metadata: dict | None = None,
    ) -> FraudAssessment:
        """
        Score a payment for fraud risk using Gemini.
        Falls back to safe default on any error so payments
        are never blocked by AI layer failure.
        """
        log = logger.bind(
            amount=amount,
            currency=currency,
            customer_id=customer_id,
        )

        try:
            response = await self.chain.ainvoke({
                "amount": amount / 100,
                "currency": currency.upper(),
                "customer_id": customer_id or "anonymous",
                "description": description or "none provided",
                "metadata": json.dumps(metadata) if metadata else "{}",
            })

            raw = response.content.strip()

            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
                raw = raw.strip()

            parsed = json.loads(raw)

            assessment = FraudAssessment(
                risk_score=float(parsed["risk_score"]),
                risk_level=parsed["risk_level"],
                flags=parsed.get("flags", []),
                recommendation=parsed.get("recommendation", "approve"),
            )

            log.info(
                "fraud_score_computed",
                risk_score=assessment.risk_score,
                risk_level=assessment.risk_level,
                flags=assessment.flags,
            )

            return assessment

        except Exception as e:
            traceback.print_exc()
            log.warning("fraud_scoring_failed", error=str(e))
            return FraudAssessment(
                risk_score=0.0,
                risk_level="low",
                flags=["scoring_unavailable"],
                recommendation="approve",
            )