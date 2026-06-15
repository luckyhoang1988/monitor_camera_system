# Chek_NVR — image production cho FastAPI + APScheduler
FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Ho_Chi_Minh

WORKDIR /app

# Cài dependency trước (tận dụng cache layer khi chỉ đổi code).
COPY requirements.txt ./
RUN pip install -r requirements.txt

# Copy toàn bộ source.
COPY . .

# Chạy bằng user không phải root cho an toàn.
RUN useradd --create-home --uid 1000 appuser \
    && chmod +x docker/entrypoint.sh \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8080

# entrypoint: chờ DB sẵn sàng -> alembic upgrade head -> chạy uvicorn.
ENTRYPOINT ["./docker/entrypoint.sh"]
