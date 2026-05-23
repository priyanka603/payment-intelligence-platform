FROM python:3.13-slim AS base
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc g++ build-essential \
    && rm -rf /var/lib/apt/lists/*

FROM base AS deps
COPY pyproject.toml .
RUN pip install --no-cache-dir --upgrade pip setuptools wheel
RUN pip install --no-cache-dir \
    stripe \
    fastapi \
    "uvicorn[standard]" \
    sqlalchemy \
    asyncpg \
    alembic \
    langchain \
    langchain-core \
    langchain-openai \
    langchain-community \
    langchain-google-genai \
    openai \
    pydantic \
    pydantic-settings \
    python-dotenv \
    httpx \
    structlog \
    prometheus-client
RUN pip install --no-cache-dir faiss-cpu || \
    pip install --no-cache-dir faiss-cpu --no-build-isolation || \
    echo "faiss-cpu install failed, continuing without it"
RUN pip install --no-cache-dir \
    pytest \
    pytest-asyncio \
    pytest-cov \
    ruff \
    mypy

FROM deps AS development
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

FROM deps AS production
COPY . .
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
RUN adduser --disabled-password --gecos "" appuser \
    && chown -R appuser:appuser /app
USER appuser
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]