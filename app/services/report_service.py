"""Báo cáo & thống kê uptime nâng cao từ bảng *_status_logs.

Uptime ở đây = tỷ lệ số lần kiểm tra ghi nhận **Online** trên tổng số lần kiểm tra
trong khoảng thời gian, dựa trên `nvr_status_logs` / `camera_status_logs`. Vì mỗi
chu kỳ quét đều ghi một bản ghi log, đây là xấp xỉ tốt cho % thời gian hoạt động.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import Integer, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CameraChannel, CameraStatusLog, NVRDevice, NVRStatusLog
from app.enums import CameraStatus, NVRStatus


def _cutoff(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


def _window(
    days: int, start: datetime | None, end: datetime | None
) -> tuple[datetime, datetime | None]:
    """Khoảng thời gian truy vấn dạng (start, end).

    Nếu có `start`/`end` (giờ UTC) thì ưu tiên khoảng tuỳ chọn; ngược lại dùng
    cửa sổ tương đối `days` ngày gần nhất (end = None = đến hiện tại).
    """
    if start is not None or end is not None:
        end_dt = end if end is not None else datetime.now(timezone.utc)
        start_dt = start if start is not None else (end_dt - timedelta(days=days))
        return start_dt, end_dt
    return _cutoff(days), None


def _pct(online: int, total: int) -> float:
    return round(online / total * 100, 1) if total else 0.0


@dataclass
class NVRUptimeRow:
    nvr_id: int
    name: str
    area: str | None
    total_checks: int
    online_checks: int
    uptime_pct: float
    current_status: str | None = None

    @property
    def recovered(self) -> bool:
        """Đang Online trở lại sau khi từng có downtime trong kỳ (uptime < 100%)."""
        return self.current_status == NVRStatus.ONLINE.value and self.uptime_pct < 100


@dataclass
class CameraDowntimeRow:
    camera_id: int
    nvr_name: str
    channel_no: int
    name: str | None
    offline_checks: int
    total_checks: int
    uptime_pct: float
    current_status: str | None = None

    @property
    def recovered(self) -> bool:
        """Đang Online trở lại (từng offline trong kỳ — nằm trong danh sách này)."""
        return self.current_status == CameraStatus.ONLINE.value


@dataclass
class UptimeReport:
    days: int
    nvr_rows: list[NVRUptimeRow]
    worst_cameras: list[CameraDowntimeRow]
    system_nvr_uptime: float
    system_camera_uptime: float
    start: datetime | None = None
    end: datetime | None = None

    @property
    def is_custom_range(self) -> bool:
        return self.start is not None or self.end is not None


async def nvr_uptime_rows(
    session: AsyncSession,
    days: int,
    *,
    area: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[NVRUptimeRow]:
    """Uptime từng NVR trong khoảng thời gian, sắp xếp uptime tăng dần (tệ nhất trước)."""
    start_dt, end_dt = _window(days, start, end)
    join_cond = (NVRStatusLog.nvr_id == NVRDevice.id) & (
        NVRStatusLog.checked_at >= start_dt
    )
    if end_dt is not None:
        join_cond &= NVRStatusLog.checked_at <= end_dt
    online_expr = func.sum(
        case((NVRStatusLog.status == NVRStatus.ONLINE.value, 1), else_=0)
    )
    stmt = (
        select(
            NVRDevice.id,
            NVRDevice.name,
            NVRDevice.area,
            func.count(NVRStatusLog.id),
            online_expr,
            NVRDevice.current_status,
        )
        .select_from(NVRDevice)
        .outerjoin(NVRStatusLog, join_cond)
        .group_by(
            NVRDevice.id, NVRDevice.name, NVRDevice.area, NVRDevice.current_status
        )
        .order_by(NVRDevice.name)
    )
    if area:
        stmt = stmt.where(NVRDevice.area == area)
    rows = [
        NVRUptimeRow(
            nvr_id=nid,
            name=name,
            area=area,
            total_checks=int(total or 0),
            online_checks=int(online or 0),
            uptime_pct=_pct(int(online or 0), int(total or 0)),
            current_status=cur_status,
        )
        for nid, name, area, total, online, cur_status in (
            await session.execute(stmt)
        ).all()
    ]
    rows.sort(key=lambda r: (r.uptime_pct, -r.total_checks))
    return rows


async def worst_cameras(
    session: AsyncSession,
    days: int,
    *,
    limit: int = 15,
    area: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[CameraDowntimeRow]:
    """Top camera mất tín hiệu nhiều nhất (nhiều lần ghi Offline nhất)."""
    start_dt, end_dt = _window(days, start, end)
    offline_expr = func.sum(
        case((CameraStatusLog.status == CameraStatus.OFFLINE.value, 1), else_=0)
    ).cast(Integer)
    online_expr = func.sum(
        case((CameraStatusLog.status == CameraStatus.ONLINE.value, 1), else_=0)
    )
    stmt = (
        select(
            CameraChannel.id,
            NVRDevice.name,
            CameraChannel.channel_no,
            CameraChannel.name,
            func.count(CameraStatusLog.id),
            offline_expr,
            online_expr,
            CameraChannel.current_status,
        )
        .select_from(CameraStatusLog)
        .join(CameraChannel, CameraStatusLog.camera_id == CameraChannel.id)
        .join(NVRDevice, CameraChannel.nvr_id == NVRDevice.id)
        .where(CameraStatusLog.checked_at >= start_dt)
        .group_by(
            CameraChannel.id, NVRDevice.name, CameraChannel.channel_no,
            CameraChannel.name, CameraChannel.current_status,
        )
        .having(offline_expr > 0)
        .order_by(offline_expr.desc())
        .limit(limit)
    )
    if end_dt is not None:
        stmt = stmt.where(CameraStatusLog.checked_at <= end_dt)
    if area:
        stmt = stmt.where(NVRDevice.area == area)
    return [
        CameraDowntimeRow(
            camera_id=cid,
            nvr_name=nvr_name,
            channel_no=ch_no,
            name=cam_name,
            offline_checks=int(offline or 0),
            total_checks=int(total or 0),
            uptime_pct=_pct(int(online or 0), int(total or 0)),
            current_status=cur_status,
        )
        for cid, nvr_name, ch_no, cam_name, total, offline, online, cur_status in (
            await session.execute(stmt)
        ).all()
    ]


def _nvr_system_uptime(rows: list[NVRUptimeRow]) -> float:
    """Uptime NVR toàn hệ thống = gộp số lần Online / tổng số lần kiểm tra các NVR."""
    total = sum(r.total_checks for r in rows)
    online = sum(r.online_checks for r in rows)
    return _pct(online, total)


async def _camera_system_uptime(
    session: AsyncSession,
    days: int,
    *,
    area: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> float:
    """Uptime camera toàn hệ thống, có thể lọc theo khu vực (join sang NVR)."""
    start_dt, end_dt = _window(days, start, end)
    base_filters = [CameraStatusLog.checked_at >= start_dt]
    if end_dt is not None:
        base_filters.append(CameraStatusLog.checked_at <= end_dt)

    def _count(extra=()):
        stmt = select(func.count()).select_from(CameraStatusLog)
        if area:
            stmt = stmt.join(
                CameraChannel, CameraStatusLog.camera_id == CameraChannel.id
            ).join(NVRDevice, CameraChannel.nvr_id == NVRDevice.id).where(
                NVRDevice.area == area
            )
        return stmt.where(*base_filters, *extra)

    total = (await session.scalar(_count())) or 0
    online = (
        await session.scalar(
            _count((CameraStatusLog.status == CameraStatus.ONLINE.value,))
        )
    ) or 0
    return _pct(int(online), int(total))


async def build_uptime_report(
    session: AsyncSession,
    days: int = 7,
    *,
    area: str | None = None,
    worst_limit: int = 15,
    start: datetime | None = None,
    end: datetime | None = None,
) -> UptimeReport:
    """Gộp toàn bộ số liệu cho trang báo cáo (có thể lọc theo khu vực).

    `worst_limit` giới hạn số camera tệ nhất; bản xuất Excel truyền giá trị lớn để
    lấy toàn bộ camera có lỗi thay vì chỉ top hiển thị trên web.

    Khi truyền `start`/`end` (giờ UTC) thì báo cáo dùng đúng khoảng đó thay vì cửa
    sổ tương đối `days` — phục vụ lọc & xuất Excel theo từ ngày tới ngày.
    """
    nvr_rows = await nvr_uptime_rows(session, days, area=area, start=start, end=end)
    cams = await worst_cameras(
        session, days, area=area, limit=worst_limit, start=start, end=end
    )
    sys_nvr = _nvr_system_uptime(nvr_rows)
    sys_cam = await _camera_system_uptime(
        session, days, area=area, start=start, end=end
    )
    return UptimeReport(
        days=days,
        nvr_rows=nvr_rows,
        worst_cameras=cams,
        system_nvr_uptime=sys_nvr,
        system_camera_uptime=sys_cam,
        start=start,
        end=end,
    )
