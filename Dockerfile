# Base image version MUST match the pinned playwright version in
# requirements.txt — the image ships the matching browser build. A mismatch
# fails at runtime with "Executable doesn't exist at .../chrome-headless-shell".
FROM mcr.microsoft.com/playwright/python:v1.45.0-jammy

WORKDIR /app

ENV PYTHONUNBUFFERED=1
ENV WORLDPANEL_HEADLESS=true

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Guarantee the chromium build for the installed playwright version is present,
# regardless of what the base image shipped.
RUN python -m playwright install chromium

COPY . .

EXPOSE 8000

# Render injects $PORT; bind to it (default 8000 for local docker run).
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
