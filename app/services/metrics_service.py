"""Sinh số liệu Prometheus cho endpoint /metrics.

Mỗi lần scrape build một registry mới từ truy vấn DB (tránh đăng ký trùng global).
Hợp với mô hình tiered-scrape vốn có của dự án: Prometheus kéo /metrics định kỳ.
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Gauge, generate_latest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Alert, NVRDevice
from app.enums import AlertStatus
from app.services.query_service import get_overview


async def render_metrics(session: AsyncSession) -> bytes:
    """Trả về body text Prometheus từ trạng thái hiện tại trong DB."""
    ov = await get_overview(session)
    reg = CollectorRegistry()

    Gauge("chek_nvr_total", "Tổng số NVR", registry=reg).set(ov.nvr_total)

    g_nvr = Gauge(
        "chek_nvr_status", "Số NVR theo trạng thái", ["status"], registry=reg
    )
    g_nvr.labels("Online").set(ov.nvr_online)
    g_nvr.labels("Offline").set(ov.nvr_offline)
    g_nvr.labels("Warning").set(ov.nvr_warning)

    Gauge("chek_nvr_camera_total", "Tổng số camera", registry=reg).set(ov.camera_total)
    g_cam = Gauge(
        "chek_nvr_camera_status", "Số camera theo trạng thái", ["status"], registry=reg
    )
    g_cam.labels("online").set(ov.camera_online)
    g_cam.labels("offline").set(ov.camera_offline)
    Gauge(
        "chek_nvr_camera_uptime_ratio",
        "Tỷ lệ camera online trên tổng (%)",
        registry=reg,
    ).set(ov.uptime_ratio)

    # Sức khỏe lưu trữ theo trạng thái.
    g_storage = Gauge(
        "chek_nvr_storage_status", "Số NVR theo trạng thái lưu trữ", ["status"],
        registry=reg,
    )
    rows = (
        await session.execute(
            select(NVRDevice.storage_status, func.count()).group_by(
                NVRDevice.storage_status
            )
        )
    ).all()
    for status_val, n in rows:
        g_storage.labels(status_val).set(n)

    open_alerts = (
        await session.scalar(
            select(func.count())
            .select_from(Alert)
            .where(Alert.status == AlertStatus.OPEN.value)
        )
    ) or 0
    Gauge("chek_nvr_alerts_open", "Số cảnh báo đang mở", registry=reg).set(open_alerts)

    return generate_latest(reg)
