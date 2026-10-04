FROM node:24-slim AS web
WORKDIR /web
COPY apps/web/package.json apps/web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY apps/web/ ./
RUN npm test && npm run build

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends poppler-utils tesseract-ocr tesseract-ocr-eng && rm -rf /var/lib/apt/lists/*
COPY requirements.lock ./requirements.lock
RUN pip install --no-cache-dir -r requirements.lock && python -m playwright install --with-deps chromium
COPY . .
COPY --from=web /web/dist /app/apps/web/dist
RUN useradd --create-home --uid 10001 mjobs && mkdir -p /data/documents && mv /root/.cache/ms-playwright /opt/playwright && chown -R mjobs:mjobs /data /app /opt/playwright
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
USER mjobs
