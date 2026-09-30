FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.lock ./requirements.lock
RUN pip install --no-cache-dir -r requirements.lock && python -m playwright install --with-deps chromium
COPY . .
RUN useradd --create-home --uid 10001 mjobs && mkdir -p /data/documents && mv /root/.cache/ms-playwright /opt/playwright && chown -R mjobs:mjobs /data /app /opt/playwright
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
USER mjobs
