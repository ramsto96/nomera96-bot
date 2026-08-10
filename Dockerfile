FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN python -m playwright install --with-deps chromium

COPY bot.py .
RUN mkdir -p /app/data /app/output

CMD ["python", "bot.py"]
