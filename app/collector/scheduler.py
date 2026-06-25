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
from app.enums import NVR_DOWN_STATES, NVRStatus
from app.services.alert_service import (
    process_camera_alerts,
    process_nvr_alerts,
    process_storage_alerts,
)
from app.services.event_bus import (
    EVENT_ALERT,
    EVENT_CAMERA_CHANGE,
    EVENT_NVR_CHANGE,
    EVENT_STORAGE_CHANGE,
    event_bus,
)
from app.services.retention_service import purge_old_logs
from app.services.telegram_notifier import flush_telegram_notifications
from app.services.status_service import (
    check_and_update_nvr_health,
    log_cameras_unreachable,
    update_nvr_cameras,
    update_nvr_storage,
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
                # Phát SSE SAU commit (dữ liệu đã bền vững) khi NVR đổi trạng thái —
                # đổi trạng thái cũng kéo theo tạo/resolve alert nên báo luôn "alert".
                if outcome.new_status != outcome.prev_status:
                    event_bus.publish(EVENT_NVR_CHANGE, {"nvr_id": nvr_id})
                    event_bus.publish(EVENT_ALERT, {"nvr_id": nvr_id})
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
    - NVR đã chốt chết (Offline/Network/Auth): không fetch được; ghi log camera Unknown
      để báo cáo uptime camera bị trừ đúng downtime (không alert — alert NVR-level đã lo).
    - NVR Warning (chập chờn, chưa kết luận): KHÔNG đụng — giữ camera last-known, không
      ghi log, không trừ uptime (chống flapping, đồng bộ với tầng cảnh báo).
    """
    settings = get_settings()
    async with sem:
        async with AsyncSessionLocal() as session:
            nvr = await session.get(NVRDevice, nvr_id)
            if nvr is None or not nvr.enabled:
                return
            try:
                status = NVRStatus(nvr.current_status)
                outcome = None
                if status == NVRStatus.ONLINE:
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
                elif status in NVR_DOWN_STATES:
                    await log_cameras_unreachable(session, nvr)
                # else: Warning -> không làm gì (giữ last-known).
                await session.commit()
                # Phát SSE SAU commit khi có camera đổi trạng thái / có alert mới.
                if outcome is not None and outcome.ok:
                    if outcome.changed:
                        event_bus.publish(EVENT_CAMERA_CHANGE, {"nvr_id": nvr_id})
                    if outcome.offline_alertable or outcome.recovered:
                        event_bus.publish(EVENT_ALERT, {"nvr_id": nvr_id})
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


async def scan_nvr_fast() -> None:
    """Fast lane: chỉ quét lại NVR đang nghi ngờ/đã chết (mô hình tiered-scrape).

    Tập theo dõi = Warning (chập chờn, chưa kết luận) + các trạng thái đã chốt chết
    (Offline/Auth Error/Network Error). Quét ở nhịp `nvr_check_interval_fast` để:
    - leo thang Warning -> Offline nhanh hơn (xác nhận đủ `fail_threshold` lần sớm),
    - phát hiện NVR hồi phục gần như tức thì (đẩy SSE recovery ngay).
    NVR đang Online không nằm trong tập này -> giảm tải mạng (giống Prometheus chỉ
    scrape dày các target cần chú ý).
    """
    settings = get_settings()
    sem = asyncio.Semaphore(settings.max_concurrency)
    watch_states = {NVRStatus.WARNING.value} | {s.value for s in NVR_DOWN_STATES}

    async with AsyncSessionLocal() as session:
        ids = (
            await session.scalars(
                select(NVRDevice.id).where(
                    NVRDevice.enabled.is_(True),
                    NVRDevice.current_status.in_(watch_states),
                )
            )
        ).all()

    if not ids:
        return  # không có NVR nào cần theo dõi gấp -> im lặng, không log mỗi 30s

    logger.info("Fast lane: quét lại %d NVR đang nghi ngờ/down", len(ids))
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


async def _storage_one(nvr_id: int, sem: asyncio.Semaphore) -> None:
    """Quét sức khỏe lưu trữ 1 NVR (chỉ NVR Online), xử lý alert + phát SSE."""
    settings = get_settings()
    async with sem:
        async with AsyncSessionLocal() as session:
            nvr = await session.get(NVRDevice, nvr_id)
            if nvr is None or not nvr.enabled:
                return
            try:
                if NVRStatus(nvr.current_status) != NVRStatus.ONLINE:
                    return  # NVR không Online -> không lấy được storage; bỏ qua.
                nvr_name = nvr.name
                outcome = await update_nvr_storage(
                    session,
                    nvr,
                    timeout=settings.request_timeout,
                    warn_pct=settings.disk_warn_pct,
                    crit_pct=settings.disk_crit_pct,
                    temp_warn_c=settings.hdd_temp_warn_c,
                )
                if outcome.ok:
                    await process_storage_alerts(session, nvr_id, nvr_name, outcome)
                await session.commit()
                # Phát SSE SAU commit khi trạng thái lưu trữ đổi (kèm alert nếu có).
                if outcome.ok and outcome.changed:
                    event_bus.publish(EVENT_STORAGE_CHANGE, {"nvr_id": nvr_id})
                    event_bus.publish(EVENT_ALERT, {"nvr_id": nvr_id})
                await flush_telegram_notifications(session)
            except Exception:  # noqa: BLE001 - 1 NVR lỗi không được dừng cả batch
                await session.rollback()
                logger.exception("Lỗi khi quét lưu trữ NVR %s", nvr_id)


async def scan_storage() -> None:
    """Quét sức khỏe lưu trữ (HDD/RAID/S.M.A.R.T) cho toàn bộ NVR đang bật.

    Chỉ NVR Online mới đọc được storage; NVR khác bỏ qua (giữ trạng thái last-known).
    Chạy ở tần suất thấp (`storage_check_interval`) vì lưu trữ ít biến động.
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
        return

    logger.info("Quét lưu trữ %d NVR", len(ids))
    await asyncio.gather(*(_storage_one(i, sem) for i in ids))


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
        "Retention: đã xóa %d log NVR, %d log camera, %d log lưu trữ, "
        "%d alert đã đóng (giữ %d ngày)",
        result.nvr_logs,
        result.camera_logs,
        result.storage_logs,
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
        scan_nvr_fast,
        "interval",
        seconds=settings.nvr_check_interval_fast,
        id="scan_nvr_fast",
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
    scheduler.add_job(
        scan_storage,
        "interval",
        seconds=settings.storage_check_interval,
        id="scan_storage",
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
        "Scheduler đã chạy: health mỗi %ds, fast lane mỗi %ds, camera mỗi %ds, "
        "lưu trữ mỗi %ds, dọn log 03:00 (giữ %d ngày)",
        settings.nvr_check_interval,
        settings.nvr_check_interval_fast,
        settings.camera_check_interval,
        settings.storage_check_interval,
        settings.log_retention_days,
    )
    return scheduler


def shutdown_scheduler() -> None:
    """Dừng scheduler khi app shutdown."""
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
