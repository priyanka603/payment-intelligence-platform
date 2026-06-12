import json
import traceback

from langchain_core.prompts import ChatPromptTemplate
from langchain_groq import ChatGroq
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
        "You are a payments fraud detection system. "
        "Analyze the payment and return ONLY a JSON object.\n\n"
        "Rules:\n"
        "- risk_score: float between 0.0 (no risk) and 1.0 (certain fraud)\n"
        "- risk_level: 'low' (0.0-0.3), 'medium' (0.31-0.7), 'high' (0.71-1.0)\n"
        "- flags: list of specific risk signals observed, empty list if none\n"
        "- recommendation: one of 'approve', 'review', 'block'\n\n"
        "Respond ONLY with valid JSON, no explanation, no markdown, no backticks.\n\n"
        'Example: {{"risk_score": 0.15, "risk_level": "low", '
        '"flags": [], "recommendation": "approve"}}',
    ),
    (
        "human",
        "Analyze this payment for fraud risk:\n\n"
        "Amount: {amount} {currency}\n"
        "Customer ID: {customer_id}\n"
        "Description: {description}\n"
        "Metadata: {metadata}\n\n"
        "Consider:\n"
        "- Unusually high amounts for the currency\n"
        "- Missing or suspicious customer ID\n"
        "- Suspicious patterns in description or metadata\n"
        "- Round numbers that suggest card testing\n\n"
        "Return JSON only.",
    ),
])


class FraudDetectionService:
    def __init__(self) -> None:
        self.llm = ChatGroq(
            model="llama-3.3-70b-versatile",
            api_key=settings.groq_key,
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
        Score a payment for fraud risk using Groq (Llama3).
        Falls back to rule-based scoring on any error so payments
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
            log.warning("fraud_scoring_failed_falling_back", error=str(e))
            return await self._rule_based_fallback(
                amount, currency, customer_id, description
            )

    async def _rule_based_fallback(
        self,
        amount: int,
        currency: str,
        customer_id: str | None,
        description: str | None,
    ) -> FraudAssessment:
        """
        Rule-based fallback when Groq is unavailable.
        Same interface, same return type — payment flow never breaks.
        """
        score = 0.0
        flags = []

        if amount > 1_000_000:
            score += 0.5
            flags.append("high_amount")
        elif amount > 100_000:
            score += 0.2
            flags.append("elevated_amount")

        if not customer_id:
            score += 0.2
            flags.append("anonymous_customer")

        suspicious_words = ["test", "dummy", "fake", "xxx", "asdf"]
        if description and any(
            word in description.lower() for word in suspicious_words
        ):
            score += 0.2
            flags.append("suspicious_description")

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

        flags.append("rule_based_fallback")

        logger.info(
            "fraud_score_fallback_used",
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