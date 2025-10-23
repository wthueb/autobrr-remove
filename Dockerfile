FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0

WORKDIR /app

COPY uv.lock pyproject.toml .
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-install-project --no-dev

COPY . /app
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev

FROM python:3.12-slim-bookworm

RUN groupadd --system --gid 999 nonroot && useradd --system --gid 999 --uid 999 --create-home nonroot

WORKDIR /app
COPY --from=builder --chown=nonroot:nonroot /app .

ENV PATH="/app/.venv/bin:$PATH"

USER nonroot

ENV LOG_LEVEL=INFO
ENV INTERVAL_SECONDS=60

CMD ["autobrr-remove"]
