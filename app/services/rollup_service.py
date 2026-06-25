"""Rollup uptime NVR theo ngày: gộp nvr_status_logs -> daily_nvr_uptime.

Chạy đêm (trước/độc lập với retention) để giữ lịch sử uptime dài hạn kể cả khi log
thô đã bị dọn theo `log_retention_days`. Tách hàm thuần (nhận ngày cụ thể) để test.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import case, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DailyNvrUptime, NVRStatusLog
from app.enums import NVRStatus


async def rollup_nvr_day(session: AsyncSession, day: date) -> int:
    """Gộp uptime NVR cho 1 ngày (UTC) vào daily_nvr_uptime. Trả số NVR đã ghi.

    Xóa-rồi-ghi-lại các dòng của ngày đó (idempotent: chạy lại cho cùng ngày an toàn).
    KHÔNG commit — caller chịu trách nhiệm.
    """
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    end = start + timedelta(days=1)
    online_expr = func.sum(
        case((NVRStatusLog.status == NVRStatus.ONLINE.value, 1), else_=0)
    )
    stmt = (
        select(NVRStatusLog.nvr_id, func.count(NVRStatusLog.id), online_expr)
        .where(NVRStatusLog.checked_at >= start, NVRStatusLog.checked_at < end)
        .group_by(NVRStatusLog.nvr_id)
    )

    await session.execute(delete(DailyNvrUptime).where(DailyNvrUptime.day == day))
    n = 0
    for nvr_id, total, online in (await session.execute(stmt)).all():
        total = int(total or 0)
        online = int(online or 0)
        session.add(
            DailyNvrUptime(
                day=day,
                nvr_id=nvr_id,
                total_checks=total,
                online_checks=online,
                uptime_pct=round(online / total * 100, 1) if total else 0.0,
            )
        )
        n += 1
    return n


async def rollup_yesterday(session: AsyncSession) -> int:
    """Gộp uptime của NGÀY HÔM QUA (UTC) — gọi trong job đêm."""
    yesterday = (datetime.now(UTC) - timedelta(days=1)).date()
    return await rollup_nvr_day(session, yesterday)
