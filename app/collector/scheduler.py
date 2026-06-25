"""APScheduler: quét NVR/camera định kỳ với giới hạn song song (Semaphore batch).

Hai job tách biệt theo đúng ngữ nghĩa config:
- `scan_nvr_health`  : tần suất cao (`nvr_check_interval`)   — chỉ ping/port/deviceInfo.
- `scan_cameras`     : tần suất thấp (`camera_check_interval`) — chỉ NVR đang Online.
"""

from __future__ import annotations

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from app.config import get_settings
from app.db.base import AsyncSessionLocal
from app.db.models import NVRDevice
from app.enums import NVRStatus
from app.services.alert_service import process_camera_alerts, process_nvr_alerts
from app.services.retention_service import purge_old_logs
from app.services.telegram_notifier import flush_telegram_notifications
from app.services.status_service import (
    check_and_update_nvr_health,
    log_cameras_unreachable,
    update_nvr_cameras,
)

logger = logging.getLogger("chek_nvr.scheduler")

_scheduler: AsyncIOScheduler | None = None


async def _health_one(nvr_id: int, sem: asyncio.Semaphore) -> None:
    """Kiểm tra sức khỏe 1 NVR trong session riêng (1 transaction độc lập)."""
    settings = get_settings()
    async with sem:
        async with AsyncSessionLocal() as session:
            nvr = await session.get(NVRDevice, nvr_id)
            if nvr is None or not nvr.enabled:
                return
            try:
                nvr_name = nvr.name
                outcome = await check_and_update_nvr_health(
                    session,
                    nvr,
                    fail_threshold=settings.fail_threshold,
                    timeout=settings.request_timeout,
                )
                await process_nvr_alerts(session, outcome, nvr_name)
                await session.commit()
                await flush_telegram_notifications(session)
                logger.info(
                    "NVR %s: %s -> %s",
                    nvr_id,
                    outcome.prev_status.value,
                    outcome.new_status.value,
                )
            except Exception:  # noqa: BLE001 - 1 NVR lỗi không được dừng cả batch
                await session.rollback()
                logger.exception("Lỗi khi kiểm tra sức khỏe NVR %s", nvr_id)


async def _cameras_one(nvr_id: int, sem: asyncio.Semaphore) -> None:
    """Quét camera của 1 NVR.

    - NVR Online: fetch kênh thật + cập nhật trạng thái + xử lý alert.
    - NVR không Online: không fetch được; ghi log camera Unknown để báo cáo uptime
      camera bị trừ đúng thời gian NVR chết (không alert — alert NVR-level đã lo).
    """
    settings = get_settings()
    async with sem:
        async with AsyncSessionLocal() as session:
            nvr = await session.get(NVRDevice, nvr_id)
            if nvr is None or not nvr.enabled:
                return
            try:
                if NVRStatus(nvr.current_status) == NVRStatus.ONLINE:
                    nvr_name = nvr.name
                    outcome = await update_nvr_cameras(
                        session, nvr, timeout=settings.request_timeout
                    )
                    # Chỉ đụng alert khi có dữ liệu camera tin cậy. Fetch lỗi (ok=False)
                    # -> giữ nguyên alert/offline_since để tránh resolve nhầm khi timeout.
                    if outcome.ok:
                        await process_camera_alerts(
                            session, nvr_id, nvr_name, outcome
                        )
                else:
                    await log_cameras_unreachable(session, nvr)
                await session.commit()
                await flush_telegram_notifications(session)
            except Exception:  # noqa: BLE001 - 1 NVR lỗi không được dừng cả batch
                await session.rollback()
                logger.exception("Lỗi khi quét camera NVR %s", nvr_id)


async def scan_nvr_health() -> None:
    """Quét sức khỏe toàn bộ NVR đang bật, giới hạn song song bằng Semaphore."""
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

    logger.info(
        "Quét sức khỏe %d NVR (song song tối đa %d)", len(ids), settings.max_concurrency
    )
    await asyncio.gather(*(_health_one(i, sem) for i in ids))


async def scan_cameras() -> None:
    """Quét camera cho toàn bộ NVR đang bật.

    NVR Online -> fetch kênh thật; NVR không Online -> ghi log camera Unknown (để
    báo cáo uptime camera phản ánh cả thời gian NVR chết). Chạy ở tần suất thấp hơn
    job health, cùng nhịp cho cả hai nhánh nên tỷ lệ uptime không bị lệch.
    """
    settings = get_settings()
    sem = asyncio.Semaphore(settings.max_concurrency)

    async with AsyncSessionLocal() as session:
        ids = (
            await session.scalars(
                select(NVRDevice.id).where(NVRDevice.enabled.is_(True))
            )
        ).all()

    if not ids:
        logger.info("Không có NVR nào để quét camera.")
        return

    logger.info("Quét camera của %d NVR (Online: fetch; offline: ghi log Unknown)", len(ids))
    await asyncio.gather(*(_cameras_one(i, sem) for i in ids))


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
        scan_nvr_health,
        "interval",
        seconds=settings.nvr_check_interval,
        id="scan_nvr_health",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        scan_cameras,
        "interval",
        seconds=settings.camera_check_interval,
        id="scan_cameras",
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
        "Scheduler đã chạy: health mỗi %ds, camera mỗi %ds, dọn log 03:00 (giữ %d ngày)",
        settings.nvr_check_interval,
        settings.camera_check_interval,
        settings.log_retention_days,
    )
    return scheduler


def shutdown_scheduler() -> None:
    """Dừng scheduler khi app shutdown."""
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
