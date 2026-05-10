FROM python:3.11-slim

# System deps for pypdf / openpyxl image handling -- keep minimal
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code (data/ is excluded via .dockerignore -- comes from volume)
COPY . .

# Seed config files into a non-volume path. The persistent volume mounts at
# /app/data and would otherwise hide ledger_schema.json + accounting_rules.json
# baked into the image. app.py copies them into /app/data/ on startup if missing.
RUN mkdir -p /app/seed \
    && cp data/ledger_schema.json /app/seed/ \
    && cp data/accounting_rules.json /app/seed/

# Fly.io maps the persistent volume to /app/data
RUN mkdir -p /app/data/intake /app/data/vies_cache

ENV PORT=8080
ENV FLASK_DEBUG=false
ENV PYTHONUNBUFFERED=1

EXPOSE 8080

# 2 workers x 120s timeout -- handles VIES lookups + LLM round-trips
CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:8080", "--workers", "2", "--timeout", "120", "--access-logfile", "-"]
