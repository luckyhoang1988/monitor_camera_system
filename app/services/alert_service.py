"""Sinh cảnh báo (web notification) từ kết quả cập nhật NVR.

Nguyên tắc chống spam: alert dựa trên *chuyển trạng thái* + dedupe (không tạo
trùng khi đã có alert OPEN cùng loại cho cùng NVR). Khi NVR/camera hồi phục thì
tự resolve các alert OPEN tương ứng.

Bản đầu chỉ ghi alert vào DB để hiển thị trên dashboard; các kênh ngoài
(Telegram/Email/Teams) sẽ cắm thêm sau qua hàm dispatch riêng.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import Alert
from app.enums import AlertSeverity, AlertStatus, AlertType, NVRStatus
from app.services.status_service import NVRUpdateOutcome

# Trạng thái coi là "đang lỗi" của NVR.
_DOWN_STATES = {NVRStatus.OFFLINE, NVRStatus.NETWORK_ERROR, NVRStatus.AUTH_ERROR}


async def _has_open_alert(
    session: AsyncSession, nvr_id: int, alert_type: AlertType
) -> bool:
    found = await session.scalar(
        select(Alert.id).where(
            Alert.nvr_id == nvr_id,
            Alert.type == alert_type.value,
            Alert.status == AlertStatus.OPEN.value,
        )
    )
    return found is not None


async def _resolve_open_alerts(
    session: AsyncSession, nvr_id: int, alert_type: AlertType
) -> None:
    await session.execute(
        update(Alert)
        .where(
            Alert.nvr_id == nvr_id,
            Alert.type == alert_type.value,
            Alert.status == AlertStatus.OPEN.value,
        )
        .values(
            status=AlertStatus.RESOLVED.value,
            resolved_at=datetime.now(timezone.utc),
        )
    )


async def _create_alert(
    session: AsyncSession,
    *,
    nvr_id: int,
    alert_type: AlertType,
    severity: AlertSeverity,
    message: str,
) -> None:
    if await _has_open_alert(session, nvr_id, alert_type):
        return
    session.add(
        Alert(
            type=alert_type.value,
            severity=severity.value,
            nvr_id=nvr_id,
            message=message,
        )
    )


async def process_outcome(
    session: AsyncSession, outcome: NVRUpdateOutcome, nvr_name: str
) -> None:
    """Tạo/resolve alert dựa trên chuyển trạng thái của một NVR."""
    settings = get_settings()
    prev, new = outcome.prev_status, outcome.new_status

    # 1. NVR offline (lỗi kết nối đã qua ngưỡng -> Offline).
    if new == NVRStatus.OFFLINE and prev != NVRStatus.OFFLINE:
        await _create_alert(
            session,
            nvr_id=outcome.nvr_id,
            alert_type=AlertType.NVR_OFFLINE,
            severity=AlertSeverity.CRITICAL,
            message=f"NVR '{nvr_name}' offline.",
        )

    # 2. Lỗi xác thực.
    if new == NVRStatus.AUTH_ERROR and prev != NVRStatus.AUTH_ERROR:
        await _create_alert(
            session,
            nvr_id=outcome.nvr_id,
            alert_type=AlertType.AUTH_ERROR,
            severity=AlertSeverity.CRITICAL,
            message=f"NVR '{nvr_name}' sai tài khoản/mật khẩu.",
        )

    # 3. Hồi phục: NVR trở lại Online sau khi đang lỗi -> resolve + báo info.
    if new == NVRStatus.ONLINE and prev in _DOWN_STATES:
        await _resolve_open_alerts(session, outcome.nvr_id, AlertType.NVR_OFFLINE)
        await _resolve_open_alerts(session, outcome.nvr_id, AlertType.AUTH_ERROR)
        await _create_alert(
            session,
            nvr_id=outcome.nvr_id,
            alert_type=AlertType.NVR_RECOVERED,
            severity=AlertSeverity.INFO,
            message=f"NVR '{nvr_name}' đã online trở lại.",
        )

    # 4. Phản hồi chậm.
    if (
        new == NVRStatus.ONLINE
        and outcome.response_time_ms is not None
        and outcome.response_time_ms > settings.slow_response_ms
    ):
        await _create_alert(
            session,
            nvr_id=outcome.nvr_id,
            alert_type=AlertType.SLOW_RESPONSE,
            severity=AlertSeverity.WARNING,
            message=(
                f"NVR '{nvr_name}' phản hồi chậm "
                f"({outcome.response_time_ms} ms > {settings.slow_response_ms} ms)."
            ),
        )
    elif new == NVRStatus.ONLINE:
        await _resolve_open_alerts(session, outcome.nvr_id, AlertType.SLOW_RESPONSE)

    # 5. Camera offline trong NVR.
    if outcome.camera_offline_count > 0:
        await _create_alert(
            session,
            nvr_id=outcome.nvr_id,
            alert_type=AlertType.CAMERA_OFFLINE,
            severity=AlertSeverity.WARNING,
            message=(
                f"NVR '{nvr_name}' có {outcome.camera_offline_count} camera offline."
            ),
        )
    elif new == NVRStatus.ONLINE:
        await _resolve_open_alerts(session, outcome.nvr_id, AlertType.CAMERA_OFFLINE)
