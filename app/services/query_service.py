"""Truy vấn dữ liệu cho dashboard: tổng quan, danh sách NVR, chi tiết NVR."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Alert, CameraChannel, NVRDevice, NVRStatusLog
from app.enums import AlertStatus, CameraStatus, NVRStatus
from app.schemas import SystemOverview


async def get_overview(session: AsyncSession) -> SystemOverview:
    """Số liệu khối tổng quan toàn hệ thống."""
    # Đếm NVR theo trạng thái.
    nvr_rows = (
        await session.execute(
            select(NVRDevice.current_status, func.count()).group_by(
                NVRDevice.current_status
            )
        )
    ).all()
    nvr_counts = {status: n for status, n in nvr_rows}

    # Tổng số camera (mọi trạng thái).
    camera_total = (
        await session.scalar(select(func.count()).select_from(CameraChannel))
    ) or 0

    # Camera CHỈ tính Online khi bản thân nó Online VÀ NVR cha đang Online.
    # Camera thuộc NVR không-Online có dữ liệu đóng băng (job camera ngừng quét) nên
    # không đáng tin -> coi như offline (xem nvr_detail.html). Tránh thổi phồng
    # "Camera Online"/uptime khi NVR đã rớt nhưng camera còn hiện trạng thái Online cũ.
    camera_online = (
        await session.scalar(
            select(func.count())
            .select_from(CameraChannel)
            .join(NVRDevice, CameraChannel.nvr_id == NVRDevice.id)
            .where(
                CameraChannel.current_status == CameraStatus.ONLINE.value,
                NVRDevice.current_status == NVRStatus.ONLINE.value,
            )
        )
    ) or 0
    camera_offline = camera_total - camera_online
    uptime = round(camera_online / camera_total * 100, 1) if camera_total else 0.0

    return SystemOverview(
        nvr_total=sum(nvr_counts.values()),
        nvr_online=nvr_counts.get(NVRStatus.ONLINE.value, 0),
        nvr_offline=nvr_counts.get(NVRStatus.OFFLINE.value, 0),
        nvr_warning=nvr_counts.get(NVRStatus.WARNING.value, 0),
        camera_total=camera_total,
        camera_online=camera_online,
        camera_offline=camera_offline,
        uptime_ratio=uptime,
    )


async def list_offline_cameras(
    session: AsyncSession,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[dict]:
    """Camera đang mất tín hiệu (Offline / No Signal) kèm đầu ghi để nối tắt.

    Dùng cho khối "camera mất" trên trang Tổng quan: mỗi dòng biết thuộc NVR nào và
    link thẳng tới trang chi tiết NVR đó.
    """
    bad_statuses = (CameraStatus.OFFLINE.value, CameraStatus.NO_SIGNAL.value)
    ref_ts = func.coalesce(CameraChannel.offline_since, CameraChannel.last_checked_at)
    stmt = (
        select(CameraChannel, NVRDevice.name, NVRDevice.area)
        .join(NVRDevice, CameraChannel.nvr_id == NVRDevice.id)
        .where(CameraChannel.current_status.in_(bad_statuses))
        .order_by(NVRDevice.name, CameraChannel.channel_no)
    )
    if start is not None:
        stmt = stmt.where(ref_ts >= start)
    if end is not None:
        stmt = stmt.where(ref_ts <= end)
    return [
        {"camera": cam, "nvr_name": nvr_name, "nvr_area": nvr_area}
        for cam, nvr_name, nvr_area in (await session.execute(stmt)).all()
    ]


async def list_nvrs(
    session: AsyncSession,
    *,
    area: str | None = None,
    status: str | None = None,
) -> list[dict]:
    """Danh sách NVR + số camera online/offline, có lọc theo khu vực/trạng thái."""
    stmt = select(NVRDevice).order_by(NVRDevice.name)
    if area:
        stmt = stmt.where(NVRDevice.area == area)
    if status:
        stmt = stmt.where(NVRDevice.current_status == status)
    nvrs = (await session.scalars(stmt)).all()

    # Đếm camera online/offline theo từng NVR (1 query gộp).
    cam_stmt = select(
        CameraChannel.nvr_id,
        CameraChannel.current_status,
        func.count(),
    ).group_by(CameraChannel.nvr_id, CameraChannel.current_status)
    cam_map: dict[int, dict[str, int]] = {}
    for nvr_id, st, n in (await session.execute(cam_stmt)).all():
        cam_map.setdefault(nvr_id, {})[st] = n

    result = []
    for nvr in nvrs:
        counts = cam_map.get(nvr.id, {})
        cam_total = sum(counts.values())
        # NVR không Online -> dữ liệu camera đã đóng băng, coi toàn bộ là offline để
        # nhất quán với khối tổng quan (không hiện "Online" cũ gây hiểu nhầm).
        if nvr.current_status == NVRStatus.ONLINE.value:
            cam_online = counts.get(CameraStatus.ONLINE.value, 0)
        else:
            cam_online = 0
        result.append(
            {
                "nvr": nvr,
                "cam_online": cam_online,
                "cam_offline": cam_total - cam_online,
                "cam_total": cam_total,
            }
        )
    return result


async def get_nvr_detail(session: AsyncSession, nvr_id: int) -> dict | None:
    """Chi tiết 1 NVR: thông tin thiết bị + danh sách camera + log gần đây."""
    nvr = await session.get(NVRDevice, nvr_id)
    if nvr is None:
        return None

    cameras = (
        await session.scalars(
            select(CameraChannel)
            .where(CameraChannel.nvr_id == nvr_id)
            .order_by(CameraChannel.channel_no)
        )
    ).all()

    recent_logs = (
        await session.scalars(
            select(NVRStatusLog)
            .where(NVRStatusLog.nvr_id == nvr_id)
            .order_by(NVRStatusLog.checked_at.desc())
            .limit(20)
        )
    ).all()

    return {"nvr": nvr, "cameras": cameras, "logs": recent_logs}


async def list_alerts(
    session: AsyncSession, *, only_open: bool = True, limit: int = 100
) -> list[dict]:
    """Danh sách cảnh báo kèm tên NVR liên quan, mới nhất trước."""
    stmt = (
        select(Alert, NVRDevice.name)
        .outerjoin(NVRDevice, Alert.nvr_id == NVRDevice.id)
        .order_by(Alert.created_at.desc())
        .limit(limit)
    )
    if only_open:
        stmt = stmt.where(Alert.status == AlertStatus.OPEN.value)
    return [
        {"alert": alert, "nvr_name": name}
        for alert, name in (await session.execute(stmt)).all()
    ]


def get_distinct_areas_stmt():
    """Statement lấy danh sách khu vực phân biệt (cho bộ lọc)."""
    return select(NVRDevice.area).where(NVRDevice.area.isnot(None)).distinct()
