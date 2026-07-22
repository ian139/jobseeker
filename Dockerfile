FROM python:3.12-slim-bookworm
ENV PUPPETEER_SKIP_DOWNLOAD=true \
    PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium-headless-shell \
    JOBS_ASSISTANT_PUPPETEER_ROOT=/app/node_modules \
    HOME=/home/app \
    XDG_CONFIG_HOME=/home/app/.config \
    XDG_CACHE_HOME=/home/app/.cache

WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs npm chromium-headless-shell texlive-latex-base texlive-latex-extra texlive-fonts-recommended \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml package.json package-lock.json ./
COPY src ./src
RUN PIP_DEFAULT_TIMEOUT=120 pip install --no-cache-dir . \
    && npm ci
COPY .env.example ./
RUN useradd --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /app/data /app/resume /home/app/.config /home/app/.cache \
    && chown -R app:app /app /home/app
USER app
ENTRYPOINT ["python", "-m", "jobs_assistant.cli"]
CMD ["--help"]
