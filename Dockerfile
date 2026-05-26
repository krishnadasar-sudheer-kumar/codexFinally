# syntax=docker/dockerfile:1.7

FROM node:20-slim AS frontend-build

ENV NEXT_TELEMETRY_DISABLED=1

WORKDIR /work

RUN --mount=type=bind,source=.,target=/src,readonly \
    set -eux; \
    mkdir -p /frontend-out; \
    if [ -f /src/frontend/package.json ]; then \
        mkdir -p /work/frontend; \
        cp -a /src/frontend/. /work/frontend/; \
        cd /work/frontend; \
        if [ -f package-lock.json ]; then npm ci; else npm install; fi; \
        npm run build; \
        test -d out; \
        cp -a out/. /frontend-out/; \
    else \
        printf '%s\n' \
            '<!doctype html>' \
            '<html lang="en">' \
            '<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>FinAlly</title></head>' \
            '<body><main><h1>FinAlly</h1><p>Frontend static export has not been built yet.</p></main></body>' \
            '</html>' > /frontend-out/index.html; \
    fi

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:/root/.local/bin:$PATH"

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && curl -LsSf https://astral.sh/uv/install.sh | sh

COPY backend/ /app/
RUN uv sync --frozen --no-dev

COPY --from=frontend-build /frontend-out/ /app/static/

RUN mkdir -p /app/db

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
