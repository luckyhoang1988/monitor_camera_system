"""Dọn log cũ (retention) — giữ DB gọn khi log tăng nhanh.

720 camera × quét mỗi 5–10 phút ≈ ~200k dòng/ngày (xem CLAUDE.md §7). Cần xóa
định kỳ các bản ghi `*_status_logs` cũ hơn ngưỡng `log_retention_days`. Cũng dọn
các alert đã `resolved` quá hạn để bảng alerts không phình theo thời gian.

Tách riêng hàm thuần (nhận cutoff) khỏi I/O scheduler để dễ test bằng mock/DB.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Alert, CameraStatusLog, NVRStatusLog
from app.enums import AlertStatus


@dataclass
class PurgeResult:
    """Số dòng đã xóa cho mỗi bảng (phục vụ log/report)."""

    nvr_logs: int = 0
    camera_logs: int = 0
    resolved_alerts: int = 0

    @property
    def total(self) -> int:
        return self.nvr_logs + self.camera_logs + self.resolved_alerts


async def purge_old_logs(
    session: AsyncSession, *, retention_days: int
) -> PurgeResult:
    """Xóa log trạng thái cũ hơn `retention_days` và alert đã resolved quá hạn.

    KHÔNG commit — caller chịu trách nhiệm commit/rollback (giống các service khác).
    Trả về số dòng đã xóa từng bảng.
    """
    if retention_days <= 0:
        # 0/âm = tắt retention (giữ toàn bộ log).
        return PurgeResult()

    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)

    nvr_res = await session.execute(
        delete(NVRStatusLog).where(NVRStatusLog.checked_at < cutoff)
    )
    cam_res = await session.execute(
        delete(CameraStatusLog).where(CameraStatusLog.checked_at < cutoff)
    )
    # Chỉ dọn alert đã đóng (resolved) và đã đóng trước cutoff. Alert đang open
    # luôn được giữ để không mất cảnh báo còn hiệu lực.
    alert_res = await session.execute(
        delete(Alert).where(
            Alert.status == AlertStatus.RESOLVED.value,
            Alert.resolved_at.is_not(None),
            Alert.resolved_at < cutoff,
        )
    )

    return PurgeResult(
        nvr_logs=nvr_res.rowcount or 0,
        camera_logs=cam_res.rowcount or 0,
        resolved_alerts=alert_res.rowcount or 0,
    )


async def purge_logs_in_range(
    session: AsyncSession,
    *,
    start: datetime,
    end: datetime,
) -> PurgeResult:
    """Xóa thủ công log trạng thái trong khoảng [start, end] (UTC, bao gồm 2 đầu).

    Dùng cho nút "xóa tay" trên trang Cảnh báo khi disk báo đầy — chọn từ ngày tới
    ngày để giải phóng dung lượng. Chỉ đụng vào `*_status_logs` (phần chiếm chỗ
    nhiều nhất); KHÔNG xóa alert để tránh mất lịch sử cảnh báo.

    `start`/`end` phải là datetime timezone-aware. KHÔNG commit — caller chịu trách
    nhiệm commit/rollback. Trả về số dòng đã xóa từng bảng.
    """
    if start > end:
        raise ValueError("start phải <= end")

    nvr_res = await session.execute(
        delete(NVRStatusLog).where(
            NVRStatusLog.checked_at >= start,
            NVRStatusLog.checked_at <= end,
        )
    )
    cam_res = await session.execute(
        delete(CameraStatusLog).where(
            CameraStatusLog.checked_at >= start,
            CameraStatusLog.checked_at <= end,
        )
    )

    return PurgeResult(
        nvr_logs=nvr_res.rowcount or 0,
        camera_logs=cam_res.rowcount or 0,
    )
