# Payment Intelligence Platform

A production-grade payment processing and fraud intelligence backend built with FastAPI, Stripe, and PostgreSQL. Demonstrates core payments engineering fundamentals — idempotency, webhook verification, ACID transactions, and retry logic — with an AI-powered fraud detection layer built on LangChain and Groq.

Built as a portfolio project targeting payments engineering roles.

---

## Architecture
┌─────────────────────────────────────────────────────────┐
│                      FastAPI Layer                       │
│         POST /payments   GET /payments/:id   /health     │
└──────────────────┬──────────────────────────────────────┘
                   │
       ┌───────────┴───────────┐
       │                       │
┌──────▼───────┐     ┌─────────▼────────┐
│ PaymentService│     │ WebhookHandler   │
│               │     │                  │
│ • Idempotency │     │ • Sig verify     │
│ • Retry logic │     │ • Event routing  │
│ • Stripe SDK  │     │ • Deduplication  │
└──────┬────────┘     └─────────┬────────┘
       │                        │
       └───────────┬────────────┘
                   │
       ┌───────────┴───────────┐
       │                       │
┌──────▼───────┐     ┌─────────▼────────┐
│  PostgreSQL  │     │   Stripe API     │
│              │     │                  │
│ • payments   │     │ • PaymentIntent  │
│ • webhooks   │     │ • Webhooks       │
└──────────────┘     └──────────────────┘
```

---

## Key Engineering Decisions

### Idempotency — three layers of defence
Every payment operation is idempotent at three levels:
1. **Application layer** — check our DB for `idempotency_key` before calling Stripe
2. **Stripe layer** — pass the same key to `PaymentIntent.create()` so Stripe deduplicates too
3. **Database layer** — `UNIQUE` constraint on `idempotency_key` catches any race condition

Same request fired 10 times = exactly 1 Stripe charge. Safe for client retries.

### Money as integers — never floats
All amounts stored as integers in the smallest currency unit (cents). `1000` = €10.00.
Floating point arithmetic is not associative: `0.1 + 0.2 = 0.30000000000000004`. In financial systems that difference accumulates into real money.

### Webhook signature verification
Every incoming webhook is verified using HMAC-SHA256 before processing. Without this, any attacker can POST a fake `charge.succeeded` event and mark orders as paid for free.

### Exponential backoff with jitter
Stripe API retries use `sleep(random(0, min(cap, base * 2^attempt)))`. The jitter (random component) prevents thundering herd — without it, all concurrent retries fire at the same moment after a rate limit, making the problem worse.

### ACID transactions
All financial writes go through SQLAlchemy async sessions with explicit commit/rollback. The `get_db()` dependency commits on success and rolls back on any exception — atomically.

### AI as enhancement, never dependency
The fraud scoring layer wraps all LLM calls in try/except with automatic fallback to rule-based scoring. If Groq is unavailable, payments still process — the AI failure is logged and the fallback score is flagged with `rule_based_fallback` so it's always visible which path ran.

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| API framework | FastAPI + Uvicorn |
| Payments | Stripe Python SDK v7 |
| Database | PostgreSQL 16 + SQLAlchemy 2.0 async |
| AI / ML | LangChain + Groq (Llama3) + FAISS |
| Validation | Pydantic v2 |
| Logging | structlog (JSON in prod, pretty in dev) |
| Infra | Docker + Docker Compose |
| CI/CD | GitHub Actions |
| Testing | pytest + pytest-asyncio |

---

## Running Locally

### Prerequisites
- Docker Desktop
- Stripe test account ([dashboard.stripe.com](https://dashboard.stripe.com))
- Groq API key ([console.groq.com](https://console.groq.com))

### Setup

```bash
# 1. Clone the repo
git clone https://github.com/priyanka603/payment-intelligence-platform
cd payment-intelligence-platform

# 2. Create your .env file
cp .env.template .env
# Fill in your Stripe test keys and Groq API key

# 3. Generate a secret key
python -c "import secrets; print(secrets.token_hex(32))"
# Paste output as SECRET_KEY in .env

# 4. Start the full stack
docker compose up --build
```

### Verify it's running

```bash
curl http://localhost:8000/health
# {"status": "healthy", "environment": "development", "version": "0.1.0"}
```

Open Swagger UI: http://localhost:8000/docs

---

## AI Fraud Detection

Every payment is scored in real time using Llama3 via Groq. The model analyses the payment and returns a structured risk assessment:

```json
{
  "risk_score": 0.92,
  "risk_level": "high",
  "flags": ["unusually high amount", "missing customer ID", "suspicious description"],
  "recommendation": "block"
}
```

Legitimate payment:
```json
{
  "risk_score": 0.05,
  "risk_level": "low",
  "flags": [],
  "recommendation": "approve"
}
```

If the AI layer fails for any reason, scoring falls back to a rule-based engine automatically. The payment always goes through — AI is an enhancement, never a single point of failure.

---

## API Endpoints

### Create a payment
```bash
POST /api/v1/payments
Content-Type: application/json

{
  "amount": 1000,
  "currency": "eur",
  "idempotency_key": "order-abc123-attempt-1",
  "customer_id": "cust_123",
  "description": "Premium subscription"
}
```

Response:
```json
{
  "payment": {
    "id": "f53e1459-7e37-46cf-b953-a2dd59c420ff",
    "stripe_payment_intent_id": "pi_3TaF7ZGhCLd2EJ9Y06us6xeG",
    "amount": 1000,
    "currency": "eur",
    "status": "pending",
    "risk_score": 0.05
  },
  "client_secret": "pi_3TaF7ZGhCLd2EJ9Y...",
  "idempotent": false
}
```

Repeat the same request with the same `idempotency_key` — you get `"idempotent": true` and no second Stripe charge.

### Get payment status
```bash
GET /api/v1/payments/{payment_id}
```

### Health check
```bash
GET /health
```

---

## Test Cards

This project runs in Stripe **test mode only**. No real money is ever moved.

| Card number | Scenario |
|-------------|----------|
| `4242 4242 4242 4242` | Payment succeeds |
| `4000 0000 0000 0002` | Card declined |
| `4000 0000 0000 9995` | Insufficient funds |

Use any future expiry date and any 3-digit CVC.

---

## Project Structure
```
payment-intelligence-platform/
├── app/
│   ├── api/routes/         # FastAPI route handlers
│   ├── core/               # Config, logging
│   ├── db/
│   │   ├── models/         # SQLAlchemy ORM models
│   │   └── migrations/     # Alembic migrations
│   ├── schemas/            # Pydantic request/response models
│   └── services/
│       ├── stripe/         # Payment processing, webhook handling
│       ├── ai/             # Fraud detection, dispute RAG
│       └── reconciliation/ # Payment reconciliation engine
├── tests/
│   ├── unit/               # Mocked unit tests
│   └── integration/        # Tests against real DB + Stripe test mode
├── infra/postgres/         # DB init scripts
├── docker-compose.yml
├── Dockerfile
└── pyproject.toml
```

---

## Running Tests

```bash
# Inside the container
docker compose exec app pytest tests/ -v --cov=app

# Or locally with a test DB
DATABASE_URL=postgresql+asyncpg://... pytest tests/ -v
```

---

## Environment Variables

| Variable | Description |
|----------|-------------|
| `STRIPE_SECRET_KEY` | Stripe secret key (`sk_test_...`) |
| `STRIPE_PUBLISHABLE_KEY` | Stripe publishable key (`pk_test_...`) |
| `STRIPE_WEBHOOK_SECRET` | Webhook signing secret (`whsec_...`) |
| `GROQ_API_KEY` | Groq API key for Llama3 fraud scoring |
| `DATABASE_URL` | PostgreSQL connection string |
| `SECRET_KEY` | 32-byte hex for HMAC signing |

Never commit `.env`. Use `.env.template` as reference.
