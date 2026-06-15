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


@dataclass
class CameraDowntimeRow:
    camera_id: int
    nvr_name: str
    channel_no: int
    name: str | None
    offline_checks: int
    total_checks: int
    uptime_pct: float


@dataclass
class UptimeReport:
    days: int
    nvr_rows: list[NVRUptimeRow]
    worst_cameras: list[CameraDowntimeRow]
    system_nvr_uptime: float
    system_camera_uptime: float


async def nvr_uptime_rows(session: AsyncSession, days: int) -> list[NVRUptimeRow]:
    """Uptime từng NVR trong `days` ngày, sắp xếp uptime tăng dần (tệ nhất trước)."""
    cutoff = _cutoff(days)
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
        )
        .select_from(NVRDevice)
        .outerjoin(
            NVRStatusLog,
            (NVRStatusLog.nvr_id == NVRDevice.id)
            & (NVRStatusLog.checked_at >= cutoff),
        )
        .group_by(NVRDevice.id, NVRDevice.name, NVRDevice.area)
        .order_by(NVRDevice.name)
    )
    rows = [
        NVRUptimeRow(
            nvr_id=nid,
            name=name,
            area=area,
            total_checks=int(total or 0),
            online_checks=int(online or 0),
            uptime_pct=_pct(int(online or 0), int(total or 0)),
        )
        for nid, name, area, total, online in (await session.execute(stmt)).all()
    ]
    rows.sort(key=lambda r: (r.uptime_pct, -r.total_checks))
    return rows


async def worst_cameras(
    session: AsyncSession, days: int, *, limit: int = 15
) -> list[CameraDowntimeRow]:
    """Top camera mất tín hiệu nhiều nhất (nhiều lần ghi Offline nhất)."""
    cutoff = _cutoff(days)
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
        )
        .select_from(CameraStatusLog)
        .join(CameraChannel, CameraStatusLog.camera_id == CameraChannel.id)
        .join(NVRDevice, CameraChannel.nvr_id == NVRDevice.id)
        .where(CameraStatusLog.checked_at >= cutoff)
        .group_by(
            CameraChannel.id, NVRDevice.name, CameraChannel.channel_no, CameraChannel.name
        )
        .having(offline_expr > 0)
        .order_by(offline_expr.desc())
        .limit(limit)
    )
    return [
        CameraDowntimeRow(
            camera_id=cid,
            nvr_name=nvr_name,
            channel_no=ch_no,
            name=cam_name,
            offline_checks=int(offline or 0),
            total_checks=int(total or 0),
            uptime_pct=_pct(int(online or 0), int(total or 0)),
        )
        for cid, nvr_name, ch_no, cam_name, total, offline, online in (
            await session.execute(stmt)
        ).all()
    ]


async def _system_uptime(session: AsyncSession, model, status_col, online_value, days):
    cutoff = _cutoff(days)
    total = (
        await session.scalar(
            select(func.count()).select_from(model).where(model.checked_at >= cutoff)
        )
    ) or 0
    online = (
        await session.scalar(
            select(func.count())
            .select_from(model)
            .where(model.checked_at >= cutoff, status_col == online_value)
        )
    ) or 0
    return _pct(int(online), int(total))


async def build_uptime_report(session: AsyncSession, days: int = 7) -> UptimeReport:
    """Gộp toàn bộ số liệu cho trang báo cáo."""
    nvr_rows = await nvr_uptime_rows(session, days)
    cams = await worst_cameras(session, days)
    sys_nvr = await _system_uptime(
        session, NVRStatusLog, NVRStatusLog.status, NVRStatus.ONLINE.value, days
    )
    sys_cam = await _system_uptime(
        session, CameraStatusLog, CameraStatusLog.status, CameraStatus.ONLINE.value, days
    )
    return UptimeReport(
        days=days,
        nvr_rows=nvr_rows,
        worst_cameras=cams,
        system_nvr_uptime=sys_nvr,
        system_camera_uptime=sys_cam,
    )
