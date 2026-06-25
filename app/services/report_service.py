"""Báo cáo & thống kê uptime nâng cao từ bảng *_status_logs.

Uptime ở đây = tỷ lệ số lần kiểm tra ghi nhận **Online** trên tổng số lần kiểm tra
trong khoảng thời gian, dựa trên `nvr_status_logs` / `camera_status_logs`. Vì mỗi
chu kỳ quét đều ghi một bản ghi log, đây là xấp xỉ tốt cho % thời gian hoạt động.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import Integer, case, func, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CameraChannel, CameraStatusLog, NVRDevice, NVRStatusLog
from app.enums import CameraStatus, NVRStatus


def _cutoff(days: int) -> datetime:
    return datetime.now(UTC) - timedelta(days=days)


def _window(
    days: int, start: datetime | None, end: datetime | None
) -> tuple[datetime, datetime | None]:
    """Khoảng thời gian truy vấn dạng (start, end).

    Nếu có `start`/`end` (giờ UTC) thì ưu tiên khoảng tuỳ chọn; ngược lại dùng
    cửa sổ tương đối `days` ngày gần nhất (end = None = đến hiện tại).
    """
    if start is not None or end is not None:
        end_dt = end if end is not None else datetime.now(UTC)
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
class NVRRecoveryEvent:
    nvr_id: int
    name: str
    area: str | None
    recovered_at: datetime
    from_status: str | None


@dataclass
class CameraRecoveryEvent:
    camera_id: int
    nvr_id: int
    nvr_name: str
    area: str | None
    channel_no: int
    name: str | None
    recovered_at: datetime
    from_status: str | None


@dataclass
class UptimeReport:
    days: int
    nvr_rows: list[NVRUptimeRow]
    worst_cameras: list[CameraDowntimeRow]
    nvr_recoveries: list[NVRRecoveryEvent]
    camera_recoveries: list[CameraRecoveryEvent]
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


async def nvr_recovery_events(
    session: AsyncSession,
    days: int,
    *,
    area: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[NVRRecoveryEvent]:
    """Lấy toàn bộ sự kiện NVR online trở lại trong kỳ lọc.

    Tối ưu cho DB lớn: gom xử lý ở SQL bằng window function `lag`, chỉ quét:
    - log trong khoảng lọc
    - + 1 log gần nhất trước `start_dt` cho mỗi NVR (để seed trạng thái trước kỳ)
    """
    start_dt, end_dt = _window(days, start, end)
    nvr_scope_stmt = select(
        NVRDevice.id.label("nvr_id"),
        NVRDevice.name.label("name"),
        NVRDevice.area.label("area"),
    )
    if area:
        nvr_scope_stmt = nvr_scope_stmt.where(NVRDevice.area == area)
    nvr_scope = nvr_scope_stmt.subquery("nvr_scope")

    in_range_stmt = (
        select(
            NVRStatusLog.nvr_id.label("nvr_id"),
            NVRStatusLog.status.label("status"),
            NVRStatusLog.checked_at.label("checked_at"),
        )
        .join(nvr_scope, NVRStatusLog.nvr_id == nvr_scope.c.nvr_id)
        .where(NVRStatusLog.checked_at >= start_dt)
    )
    if end_dt is not None:
        in_range_stmt = in_range_stmt.where(NVRStatusLog.checked_at <= end_dt)

    prev_ranked = (
        select(
            NVRStatusLog.nvr_id.label("nvr_id"),
            NVRStatusLog.status.label("status"),
            NVRStatusLog.checked_at.label("checked_at"),
            func.row_number()
            .over(
                partition_by=NVRStatusLog.nvr_id,
                order_by=NVRStatusLog.checked_at.desc(),
            )
            .label("rn"),
        )
        .join(nvr_scope, NVRStatusLog.nvr_id == nvr_scope.c.nvr_id)
        .where(NVRStatusLog.checked_at < start_dt)
        .subquery("nvr_prev_ranked")
    )
    prev_seed_stmt = select(
        prev_ranked.c.nvr_id,
        prev_ranked.c.status,
        prev_ranked.c.checked_at,
    ).where(prev_ranked.c.rn == 1)

    base_logs = union_all(in_range_stmt, prev_seed_stmt).subquery("nvr_logs_for_recovery")
    nvr_lagged = (
        select(
            base_logs.c.nvr_id,
            base_logs.c.status,
            base_logs.c.checked_at,
            func.lag(base_logs.c.status)
            .over(
                partition_by=base_logs.c.nvr_id,
                order_by=base_logs.c.checked_at,
            )
            .label("prev_status"),
        ).subquery("nvr_lagged")
    )

    event_stmt = (
        select(
            nvr_lagged.c.nvr_id,
            nvr_scope.c.name,
            nvr_scope.c.area,
            nvr_lagged.c.checked_at,
            nvr_lagged.c.prev_status,
        )
        .join(nvr_scope, nvr_lagged.c.nvr_id == nvr_scope.c.nvr_id)
        .where(
            nvr_lagged.c.checked_at >= start_dt,
            nvr_lagged.c.status == NVRStatus.ONLINE.value,
            nvr_lagged.c.prev_status.is_not(None),
            nvr_lagged.c.prev_status != NVRStatus.ONLINE.value,
        )
        .order_by(nvr_lagged.c.checked_at.desc())
    )
    if end_dt is not None:
        event_stmt = event_stmt.where(nvr_lagged.c.checked_at <= end_dt)

    return [
        NVRRecoveryEvent(
            nvr_id=nid,
            name=name,
            area=nvr_area,
            recovered_at=recovered_at,
            from_status=prev_status,
        )
        for nid, name, nvr_area, recovered_at, prev_status in (
            await session.execute(event_stmt)
        ).all()
    ]


async def camera_recovery_events(
    session: AsyncSession,
    days: int,
    *,
    area: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[CameraRecoveryEvent]:
    """Lấy toàn bộ sự kiện camera online trở lại trong kỳ lọc.

    Tối ưu cho DB lớn: dùng `lag` ở SQL và chỉ kéo:
    - log trong khoảng lọc
    - + 1 log gần nhất trước `start_dt` cho mỗi camera
    """
    start_dt, end_dt = _window(days, start, end)
    camera_scope_stmt = (
        select(
            CameraChannel.id.label("camera_id"),
            CameraChannel.nvr_id.label("nvr_id"),
            NVRDevice.name.label("nvr_name"),
            NVRDevice.area.label("area"),
            CameraChannel.channel_no.label("channel_no"),
            CameraChannel.name.label("camera_name"),
        )
        .select_from(CameraChannel)
        .join(NVRDevice, CameraChannel.nvr_id == NVRDevice.id)
    )
    if area:
        camera_scope_stmt = camera_scope_stmt.where(NVRDevice.area == area)
    camera_scope = camera_scope_stmt.subquery("camera_scope")

    in_range_stmt = (
        select(
            CameraStatusLog.camera_id.label("camera_id"),
            CameraStatusLog.status.label("status"),
            CameraStatusLog.checked_at.label("checked_at"),
        )
        .join(camera_scope, CameraStatusLog.camera_id == camera_scope.c.camera_id)
        .where(CameraStatusLog.checked_at >= start_dt)
    )
    if end_dt is not None:
        in_range_stmt = in_range_stmt.where(CameraStatusLog.checked_at <= end_dt)

    prev_ranked = (
        select(
            CameraStatusLog.camera_id.label("camera_id"),
            CameraStatusLog.status.label("status"),
            CameraStatusLog.checked_at.label("checked_at"),
            func.row_number()
            .over(
                partition_by=CameraStatusLog.camera_id,
                order_by=CameraStatusLog.checked_at.desc(),
            )
            .label("rn"),
        )
        .join(camera_scope, CameraStatusLog.camera_id == camera_scope.c.camera_id)
        .where(CameraStatusLog.checked_at < start_dt)
        .subquery("camera_prev_ranked")
    )
    prev_seed_stmt = select(
        prev_ranked.c.camera_id,
        prev_ranked.c.status,
        prev_ranked.c.checked_at,
    ).where(prev_ranked.c.rn == 1)

    base_logs = union_all(in_range_stmt, prev_seed_stmt).subquery(
        "camera_logs_for_recovery"
    )
    cam_lagged = (
        select(
            base_logs.c.camera_id,
            base_logs.c.status,
            base_logs.c.checked_at,
            func.lag(base_logs.c.status)
            .over(
                partition_by=base_logs.c.camera_id,
                order_by=base_logs.c.checked_at,
            )
            .label("prev_status"),
        ).subquery("camera_lagged")
    )

    event_stmt = (
        select(
            cam_lagged.c.camera_id,
            camera_scope.c.nvr_id,
            camera_scope.c.nvr_name,
            camera_scope.c.area,
            camera_scope.c.channel_no,
            camera_scope.c.camera_name,
            cam_lagged.c.checked_at,
            cam_lagged.c.prev_status,
        )
        .join(camera_scope, cam_lagged.c.camera_id == camera_scope.c.camera_id)
        .where(
            cam_lagged.c.checked_at >= start_dt,
            cam_lagged.c.status == CameraStatus.ONLINE.value,
            cam_lagged.c.prev_status.is_not(None),
            # Bỏ qua Unknown: log Unknown là trạng thái tổng hợp khi NVR offline (xem
            # log_cameras_unreachable). Coi Unknown->Online là "hồi phục camera" sẽ làm
            # ngập danh sách recovery mỗi lần NVR sống lại. Chỉ tính hồi phục từ lỗi
            # thực cấp camera (Offline/No Signal/Auth Failed).
            cam_lagged.c.prev_status.not_in(
                (CameraStatus.ONLINE.value, CameraStatus.UNKNOWN.value)
            ),
        )
        .order_by(cam_lagged.c.checked_at.desc())
    )
    if end_dt is not None:
        event_stmt = event_stmt.where(cam_lagged.c.checked_at <= end_dt)

    return [
        CameraRecoveryEvent(
            camera_id=camera_id,
            nvr_id=nvr_id,
            nvr_name=nvr_name,
            area=cam_area,
            channel_no=channel_no,
            name=cam_name,
            recovered_at=recovered_at,
            from_status=prev_status,
        )
        for (
            camera_id,
            nvr_id,
            nvr_name,
            cam_area,
            channel_no,
            cam_name,
            recovered_at,
            prev_status,
        ) in (await session.execute(event_stmt)).all()
    ]


async def build_uptime_report(
    session: AsyncSession,
    days: int = 7,
    *,
    area: str | None = None,
    worst_limit: int = 15,
    start: datetime | None = None,
    end: datetime | None = None,
    camera_recovery_start: datetime | None = None,
    camera_recovery_end: datetime | None = None,
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
    nvr_recoveries = await nvr_recovery_events(
        session, days, area=area, start=start, end=end
    )
    cam_start = camera_recovery_start if camera_recovery_start is not None else start
    cam_end = camera_recovery_end if camera_recovery_end is not None else end
    cam_recoveries = await camera_recovery_events(
        session, days, area=area, start=cam_start, end=cam_end
    )
    sys_nvr = _nvr_system_uptime(nvr_rows)
    sys_cam = await _camera_system_uptime(
        session, days, area=area, start=start, end=end
    )
    return UptimeReport(
        days=days,
        nvr_rows=nvr_rows,
        worst_cameras=cams,
        nvr_recoveries=nvr_recoveries,
        camera_recoveries=cam_recoveries,
        system_nvr_uptime=sys_nvr,
        system_camera_uptime=sys_cam,
        start=start,
        end=end,
    )
