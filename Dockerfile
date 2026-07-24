FROM ghcr.io/astral-sh/uv:0.9.16 AS uv-runtime
FROM oven/bun:1.3.14 AS bun-runtime
FROM node:22.14.0-bookworm-slim AS node-runtime

FROM python:3.12-slim-bookworm
COPY --from=uv-runtime /uv /uvx /usr/local/bin/
COPY --from=bun-runtime /usr/local/bin/bun /usr/local/bin/bun
COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=node-runtime /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

ENV PUPPETEER_SKIP_DOWNLOAD=true \
    PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium-headless-shell \
    JOBS_ASSISTANT_PUPPETEER_ROOT=/app/node_modules \
    JOBS_ASSISTANT_OMP_EXECUTABLE=/app/node_modules/@oh-my-pi/pi-coding-agent/dist/cli.js \
    HOME=/home/app \
    XDG_CONFIG_HOME=/home/app/.config \
    XDG_CACHE_HOME=/home/app/.cache \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH=/app/.venv/bin:$PATH

WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends chromium-headless-shell \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml uv.lock package.json package-lock.json ./
COPY src ./src
RUN uv sync --frozen --no-dev \
    && npm ci
COPY .env.example ./
RUN useradd --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /app/data /app/resume /home/app/.config /home/app/.cache \
    && chown -R app:app /app/data /app/resume /home/app
USER app
ENTRYPOINT ["/app/.venv/bin/python", "-m", "jobs_assistant.cli"]
CMD ["--help"]
