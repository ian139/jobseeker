FROM python:3.12-slim

WORKDIR /app
RUN PIP_DEFAULT_TIMEOUT=120 pip install --no-cache-dir 'beautifulsoup4>=4.12' 'httpx>=0.27'
COPY src ./src
COPY .env.example ./
ENV PYTHONPATH=/app/src
ENTRYPOINT ["python", "-m", "jobs_assistant.cli"]
CMD ["--help"]
