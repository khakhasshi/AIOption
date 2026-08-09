# --- Stage 1: build frontend ---
FROM node:22-slim AS frontend
WORKDIR /app/web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
# Vite's persistent cache can survive a Docker COPY layer invalidation when the
# package.json hash is unchanged, producing a stale build that omits new source
# changes. Purge it before every container build so new JS/CSS always reflects
# the current workspace.
RUN rm -rf node_modules/.vite && npm run build

# --- Stage 2: runtime ---
FROM python:3.12-slim

# System deps
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Application code
COPY ai_option_scanner/ ai_option_scanner/

# Frontend build artifacts
COPY --from=frontend /app/web/dist web/dist

# Persistent volumes:
#   /app/data          -> legacy SQLite data / migration source
#   /app/.longbridge_accounts -> Longbridge account profiles

# Keep the legacy SQLite path available for ad-hoc fallback and migrations.
RUN addgroup --system aioption && adduser --system --ingroup aioption aioption && \
    mkdir -p /app/data /app/.longbridge_accounts && \
    chown -R aioption:aioption /app
RUN ln -sf /app/data/ai_option_scanner.sqlite3 /app/ai_option_scanner.sqlite3

USER aioption

EXPOSE 7001

ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1

CMD ["python", "-m", "uvicorn", "ai_option_scanner.web_api:app", \
     "--host", "0.0.0.0", "--port", "7001", \
     "--workers", "1", "--timeout-keep-alive", "30"]
