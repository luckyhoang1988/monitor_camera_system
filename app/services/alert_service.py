"""Sinh cảnh báo (web notification) từ kết quả cập nhật NVR.

Nguyên tắc chống spam: alert dựa trên *chuyển trạng thái* + dedupe (không tạo
trùng khi đã có alert OPEN cùng loại cho cùng NVR). Khi NVR/camera hồi phục thì
tự resolve các alert OPEN tương ứng.

Bản đầu chỉ ghi alert vào DB để hiển thị trên dashboard; các kênh ngoài
(Telegram/Email/Teams) sẽ cắm thêm sau qua hàm dispatch riêng.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import Alert
from app.enums import AlertSeverity, AlertStatus, AlertType, NVRStatus
from app.services.status_service import (
    CameraEvent,
    CameraScanOutcome,
    NVRHealthOutcome,
)
from app.services.telegram_notifier import queue_alert

logger = logging.getLogger("chek_nvr.alert")

# Trạng thái coi là "đang lỗi" của NVR.
_DOWN_STATES = {NVRStatus.OFFLINE, NVRStatus.NETWORK_ERROR, NVRStatus.AUTH_ERROR}


async def _has_open_alert(
    session: AsyncSession,
    nvr_id: int,
    alert_type: AlertType,
    *,
    camera_id: int | None = None,
) -> bool:
    """Có alert OPEN cùng loại không? Theo camera nếu `camera_id`, nếu không theo NVR."""
    stmt = select(Alert.id).where(
        Alert.type == alert_type.value,
        Alert.status == AlertStatus.OPEN.value,
    )
    if camera_id is not None:
        stmt = stmt.where(Alert.camera_id == camera_id)
    else:
        stmt = stmt.where(Alert.nvr_id == nvr_id)
    return await session.scalar(stmt) is not None


async def _resolve_open_alerts(
    session: AsyncSession,
    nvr_id: int,
    alert_type: AlertType,
    *,
    camera_id: int | None = None,
) -> None:
    """Resolve alert OPEN cùng loại; theo camera nếu `camera_id`, nếu không theo NVR."""
    cond = [
        Alert.type == alert_type.value,
        Alert.status == AlertStatus.OPEN.value,
    ]
    if camera_id is not None:
        cond.append(Alert.camera_id == camera_id)
    else:
        cond.append(Alert.nvr_id == nvr_id)
    result = await session.execute(
        update(Alert)
        .where(*cond)
        .values(
            status=AlertStatus.RESOLVED.value,
            resolved_at=datetime.now(timezone.utc),
        )
    )
    if result.rowcount:
        logger.info(
            "Resolve %d alert %s (NVR %s, camera %s) — đã hồi phục",
            result.rowcount,
            alert_type.value,
            nvr_id,
            camera_id if camera_id is not None else "-",
        )


async def _create_alert(
    session: AsyncSession,
    *,
    nvr_id: int,
    alert_type: AlertType,
    severity: AlertSeverity,
    message: str,
    camera_id: int | None = None,
    is_event: bool = False,
) -> None:
    """Tạo alert + xếp hàng Telegram.

    `camera_id` -> alert cấp camera (dedupe theo camera). `is_event=True` cho các
    thông báo tức thời (vd: recovery): bỏ qua dedupe theo alert OPEN và ghi thẳng
    RESOLVED — vì đây là sự kiện, không phải trạng thái lỗi kéo dài. Nếu giữ OPEN
    + dedupe thì từ lần recovery thứ 2 trở đi sẽ bị chặn, không đẩy được Telegram.
    """
    if not is_event and await _has_open_alert(
        session, nvr_id, alert_type, camera_id=camera_id
    ):
        return
    logger.info(
        "Tạo alert %s (%s) cho NVR %s camera %s: %s",
        alert_type.value,
        severity.value,
        nvr_id,
        camera_id if camera_id is not None else "-",
        message,
    )
    session.add(
        Alert(
            type=alert_type.value,
            severity=severity.value,
            nvr_id=nvr_id,
            camera_id=camera_id,
            message=message,
            status=(
                AlertStatus.RESOLVED.value if is_event else AlertStatus.OPEN.value
            ),
            resolved_at=datetime.now(timezone.utc) if is_event else None,
        )
    )
    # Xếp hàng đẩy lên Telegram; gửi thật sau khi commit (xem flush ở call site).
    queue_alert(session, severity=severity.value, message=message)


async def process_nvr_alerts(
    session: AsyncSession, outcome: NVRHealthOutcome, nvr_name: str
) -> None:
    """Tạo/resolve alert cấp NVR (offline/auth/recovery/slow) theo chuyển trạng thái."""
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
            is_event=True,
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


def _camera_label(event: CameraEvent) -> str:
    """Nhãn camera chi tiết cho nội dung alert: 'kênh 3 (Cổng chính)'."""
    label = f"kênh {event.channel_no}"
    if event.name:
        label += f" ({event.name})"
    return label


async def process_camera_alerts(
    session: AsyncSession,
    nvr_id: int,
    nvr_name: str,
    outcome: CameraScanOutcome,
) -> None:
    """Tạo/resolve alert offline + báo recovery THEO TỪNG CAMERA (kênh nào, tên gì).

    Gọi ở job camera cho NVR đang Online:
    - `outcome.offline_alertable`: từng camera offline đủ lâu -> alert riêng (dedupe
      theo camera_id nên mỗi camera chỉ báo 1 lần tới khi hồi phục).
    - `outcome.recovered`: từng camera vừa online lại -> resolve alert của camera đó
      và báo recovery (chỉ khi trước đó thực sự có alert offline mở, tránh spam blip).
    """
    settings = get_settings()

    for ev in outcome.offline_alertable:
        await _create_alert(
            session,
            nvr_id=nvr_id,
            camera_id=ev.camera_id,
            alert_type=AlertType.CAMERA_OFFLINE,
            severity=AlertSeverity.WARNING,
            message=(
                f"NVR '{nvr_name}' — Camera {_camera_label(ev)} offline "
                f"quá {settings.camera_offline_alert_min} phút."
            ),
        )

    for ev in outcome.recovered:
        had_alert = await _has_open_alert(
            session, nvr_id, AlertType.CAMERA_OFFLINE, camera_id=ev.camera_id
        )
        await _resolve_open_alerts(
            session, nvr_id, AlertType.CAMERA_OFFLINE, camera_id=ev.camera_id
        )
        if had_alert:
            await _create_alert(
                session,
                nvr_id=nvr_id,
                camera_id=ev.camera_id,
                alert_type=AlertType.CAMERA_RECOVERED,
                severity=AlertSeverity.INFO,
                message=(
                    f"NVR '{nvr_name}' — Camera {_camera_label(ev)} "
                    f"đã online trở lại."
                ),
                is_event=True,
            )
