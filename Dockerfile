FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY src/ ./src/
COPY ds_review.toml ./

ENV PYTHONUNBUFFERED=1

# Default entrypoint supports both action_runner and the webhook server command.
ENTRYPOINT ["uv", "run", "python", "-m"]
CMD ["src.action_runner"]
