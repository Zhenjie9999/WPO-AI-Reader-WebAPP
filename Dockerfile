FROM mcr.microsoft.com/playwright/python:v1.45.0-jammy

WORKDIR /app

ENV PYTHONUNBUFFERED=1
ENV WORLDPANEL_HEADLESS=true

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
