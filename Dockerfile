FROM python:3.12-slim

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY src/ src/
COPY alembic.ini ./

# Shell form for ${PORT} expansion
CMD uv run uvicorn src.app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1
