#!/bin/sh
# Entrypoint container: chờ DB -> chạy migration -> khởi động app.
set -e

echo "[entrypoint] Chờ PostgreSQL sẵn sàng..."
python - <<'PY'
import asyncio
import sys

from sqlalchemy import text

from app.db.base import engine


async def wait_db(retries: int = 30, delay: float = 2.0) -> None:
    for i in range(1, retries + 1):
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            print(f"[entrypoint] DB OK (lần thử {i}).")
            return
        except Exception as exc:  # noqa: BLE001
            print(f"[entrypoint] DB chưa sẵn sàng ({i}/{retries}): {exc}")
            await asyncio.sleep(delay)
    sys.exit("[entrypoint] Không kết nối được DB sau nhiều lần thử.")


asyncio.run(wait_db())
PY

echo "[entrypoint] Chạy migration (alembic upgrade head)..."
alembic upgrade head

echo "[entrypoint] Khởi động Uvicorn trên :8080..."
# 1 tiến trình (KHÔNG nhiều worker): APScheduler chạy in-process, nhiều worker sẽ
# nhân bản job quét -> trùng lặp. Muốn scale phải tách scheduler ra tiến trình riêng.
exec uvicorn app.main:app \
    --host 0.0.0.0 \
    --port 8080 \
    --proxy-headers \
    --forwarded-allow-ips="*"
