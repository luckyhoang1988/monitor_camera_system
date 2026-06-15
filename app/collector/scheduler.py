"""APScheduler: quét NVR/camera định kỳ với giới hạn song song (Semaphore batch).

Hiện gộp kiểm tra NVR + camera trong cùng một job `scan_all_nvrs` (camera được cập
nhật khi NVR Online). Có thể tách tần suất riêng ở giai đoạn sau nếu cần.
"""

from __future__ import annotations

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from app.config import get_settings
from app.db.base import AsyncSessionLocal
from app.db.models import NVRDevice
from app.services.alert_service import process_outcome
from app.services.retention_service import purge_old_logs
from app.services.status_service import check_and_update_nvr

logger = logging.getLogger("chek_nvr.scheduler")

_scheduler: AsyncIOScheduler | None = None


async def _check_one(nvr_id: int, sem: asyncio.Semaphore) -> None:
    """Kiểm tra 1 NVR trong session riêng (mỗi NVR 1 transaction độc lập)."""
    settings = get_settings()
    async with sem:
        async with AsyncSessionLocal() as session:
            nvr = await session.get(NVRDevice, nvr_id)
            if nvr is None or not nvr.enabled:
                return
            try:
                nvr_name = nvr.name
                outcome = await check_and_update_nvr(
                    session,
                    nvr,
                    fail_threshold=settings.fail_threshold,
                    timeout=settings.request_timeout,
                )
                await process_outcome(session, outcome, nvr_name)
                await session.commit()
                logger.info(
                    "NVR %s: %s -> %s", nvr_id, outcome.prev_status.value, outcome.new_status.value
                )
            except Exception:  # noqa: BLE001 - 1 NVR lỗi không được dừng cả batch
                await session.rollback()
                logger.exception("Lỗi khi kiểm tra NVR %s", nvr_id)


async def scan_all_nvrs() -> None:
    """Quét toàn bộ NVR đang bật, giới hạn song song bằng Semaphore."""
    settings = get_settings()
    sem = asyncio.Semaphore(settings.max_concurrency)

    async with AsyncSessionLocal() as session:
        ids = (
            await session.scalars(
                select(NVRDevice.id).where(NVRDevice.enabled.is_(True))
            )
        ).all()

    if not ids:
        logger.info("Không có NVR nào để quét.")
        return

    logger.info("Bắt đầu quét %d NVR (song song tối đa %d)", len(ids), settings.max_concurrency)
    await asyncio.gather(*(_check_one(i, sem) for i in ids))


async def purge_logs_job() -> None:
    """Dọn log cũ theo `log_retention_days` (chạy mỗi ngày)."""
    settings = get_settings()
    async with AsyncSessionLocal() as session:
        try:
            result = await purge_old_logs(
                session, retention_days=settings.log_retention_days
            )
            await session.commit()
        except Exception:  # noqa: BLE001 - lỗi retention không được làm sập app
            await session.rollback()
            logger.exception("Lỗi khi dọn log cũ")
            return
    logger.info(
        "Retention: đã xóa %d log NVR, %d log camera, %d alert đã đóng (giữ %d ngày)",
        result.nvr_logs,
        result.camera_logs,
        result.resolved_alerts,
        settings.log_retention_days,
    )


def start_scheduler() -> AsyncIOScheduler:
    """Khởi tạo và chạy scheduler. Gọi trong lifespan của FastAPI."""
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    settings = get_settings()
    scheduler = AsyncIOScheduler(timezone=settings.timezone)
    scheduler.add_job(
        scan_all_nvrs,
        "interval",
        seconds=settings.nvr_check_interval,
        id="scan_all_nvrs",
        max_instances=1,
        coalesce=True,
    )
    # Dọn log cũ mỗi ngày lúc 03:00 (giờ thấp điểm theo timezone cấu hình).
    scheduler.add_job(
        purge_logs_job,
        "cron",
        hour=3,
        minute=0,
        id="purge_logs",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    _scheduler = scheduler
    logger.info(
        "Scheduler đã chạy: quét NVR mỗi %ds, dọn log 03:00 (giữ %d ngày)",
        settings.nvr_check_interval,
        settings.log_retention_days,
    )
    return scheduler


def shutdown_scheduler() -> None:
    """Dừng scheduler khi app shutdown."""
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
