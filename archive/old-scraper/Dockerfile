FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY pyproject.toml ./
COPY src ./src
COPY tests ./tests
RUN python -m pip install --no-cache-dir -e '.[dev]'

CMD ["job-sync", "dry-run"]
