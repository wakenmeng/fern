# Fern API image. Built for arm64 (Graviton nodes on EKS):
#   docker buildx build --platform linux/arm64 -t fern-api .
FROM python:3.13-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.21 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# Dependencies first, so code changes don't reinstall them.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY migrations ./migrations
RUN uv sync --frozen --no-dev

FROM python:3.13-slim
RUN useradd --system --uid 10001 --no-create-home fern
WORKDIR /app
COPY --from=build /app /app
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1
USER 10001
EXPOSE 8000
# Migrations and the account pool run as one-off Jobs with the same image (infra/k8s).
CMD ["uvicorn", "fern.api:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
