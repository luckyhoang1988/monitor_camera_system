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
from app.enums import (
    NVR_DOWN_STATES,
    AlertSeverity,
    AlertStatus,
    AlertType,
    NVRStatus,
)
from app.enums import StorageStatus
from app.services.status_service import (
    CameraEvent,
    CameraScanOutcome,
    NVRHealthOutcome,
    StorageScanOutcome,
)
from app.services.telegram_notifier import queue_alert

logger = logging.getLogger("chek_nvr.alert")

# Trạng thái coi là "đã chốt chết" của NVR (dùng chung toàn hệ thống — xem enums).
_DOWN_STATES = NVR_DOWN_STATES


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
    notify: bool = True,
) -> bool:
    """Tạo alert (kèm tùy chọn xếp hàng Telegram). Trả về True nếu thực sự tạo mới.

    `camera_id` -> alert cấp camera (dedupe theo camera). `is_event=True` cho các
    thông báo tức thời (vd: recovery): bỏ qua dedupe theo alert OPEN và ghi thẳng
    RESOLVED — vì đây là sự kiện, không phải trạng thái lỗi kéo dài. Nếu giữ OPEN
    + dedupe thì từ lần recovery thứ 2 trở đi sẽ bị chặn, không đẩy được Telegram.

    `notify=False`: chỉ ghi alert vào DB, KHÔNG tự xếp Telegram — dùng khi caller
    muốn gộp nhiều alert thành một tin nhắn (xem `process_camera_alerts`). Trả về
    True/False để caller biết alert nào mới để đưa vào tin gộp.
    """
    if not is_event and await _has_open_alert(
        session, nvr_id, alert_type, camera_id=camera_id
    ):
        return False
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
    if notify:
        queue_alert(session, severity=severity.value, message=message)
    return True


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


def _group_message(nvr_name: str, events: list[CameraEvent], suffix: str) -> str:
    """Gộp nhiều camera cùng NVR thành 1 nội dung liệt kê danh sách kênh.

    Vd: "NVR 'Khu A' — 3 camera offline quá 10 phút:\n• kênh 3 (Cổng chính)\n..."
    """
    lines = "\n".join(f"• {_camera_label(ev)}" for ev in events)
    return f"NVR '{nvr_name}' — {len(events)} camera {suffix}:\n{lines}"


async def process_camera_alerts(
    session: AsyncSession,
    nvr_id: int,
    nvr_name: str,
    outcome: CameraScanOutcome,
) -> None:
    """Tạo/resolve alert offline + recovery THEO TỪNG CAMERA, gộp Telegram theo NVR.

    Gọi ở job camera cho NVR đang Online. Alert ghi vào DB vẫn CHI TIẾT từng camera
    (trang Cảnh báo thấy kênh nào, tên gì), nhưng Telegram được GỘP: mỗi chu kỳ chỉ
    1 tin cho nhóm camera vừa offline và 1 tin cho nhóm vừa online lại — tránh spam
    khi nhiều camera cùng NVR rớt/lên một lúc.
    - `outcome.offline_alertable`: camera offline đủ lâu -> alert riêng (dedupe theo
      camera_id). Chỉ những camera MỚI báo lần này mới đưa vào tin gộp.
    - `outcome.recovered`: camera vừa online lại -> resolve alert camera đó + báo
      recovery (chỉ khi trước đó thực sự có alert offline mở, tránh spam blip).
    """
    settings = get_settings()

    # Offline: tạo alert từng camera (im lặng), gom các camera MỚI báo -> 1 tin.
    newly_offline: list[CameraEvent] = []
    for ev in outcome.offline_alertable:
        created = await _create_alert(
            session,
            nvr_id=nvr_id,
            camera_id=ev.camera_id,
            alert_type=AlertType.CAMERA_OFFLINE,
            severity=AlertSeverity.WARNING,
            message=(
                f"NVR '{nvr_name}' — Camera {_camera_label(ev)} offline "
                f"quá {settings.camera_offline_alert_min} phút."
            ),
            notify=False,
        )
        if created:
            newly_offline.append(ev)
    if newly_offline:
        queue_alert(
            session,
            severity=AlertSeverity.WARNING.value,
            message=_group_message(
                nvr_name,
                newly_offline,
                f"offline quá {settings.camera_offline_alert_min} phút",
            ),
        )

    # Recovery: resolve + alert sự kiện từng camera (im lặng), gom kênh -> 1 tin.
    recovered_now: list[CameraEvent] = []
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
                notify=False,
            )
            recovered_now.append(ev)
    if recovered_now:
        queue_alert(
            session,
            severity=AlertSeverity.INFO.value,
            message=_group_message(nvr_name, recovered_now, "đã online trở lại"),
        )


async def process_storage_alerts(
    session: AsyncSession,
    nvr_id: int,
    nvr_name: str,
    outcome: StorageScanOutcome,
) -> None:
    """Tạo/resolve alert lưu trữ (ổ lỗi / dung lượng đầy / hồi phục) theo chuyển trạng thái.

    Tái dùng nguyên helper dedupe/resolve. Hai loại alert riêng để phân biệt nguyên nhân:
    - `HDD_ERROR` (CRITICAL): ổ error/unformatted hoặc không ghi được hình.
    - `HDD_FULL`: dung lượng vượt ngưỡng (CRITICAL khi >= crit_pct, WARNING khi >= warn_pct).
    Khi lưu trữ về Healthy -> resolve cả hai + báo sự kiện `STORAGE_RECOVERED`.
    Chỉ gọi khi `outcome.ok` (dữ liệu tin cậy) — caller đã đảm bảo.
    """
    reason = outcome.reason or ""

    # 1. Ổ lỗi / mất ghi hình.
    if outcome.has_disk_error:
        await _create_alert(
            session,
            nvr_id=nvr_id,
            alert_type=AlertType.HDD_ERROR,
            severity=AlertSeverity.CRITICAL,
            message=f"NVR '{nvr_name}' — sự cố ổ cứng: {reason}.",
        )
    else:
        await _resolve_open_alerts(session, nvr_id, AlertType.HDD_ERROR)

    # 2. Dung lượng theo ngưỡng.
    if outcome.new_status == StorageStatus.CRITICAL and outcome.is_full_critical:
        await _create_alert(
            session,
            nvr_id=nvr_id,
            alert_type=AlertType.HDD_FULL,
            severity=AlertSeverity.CRITICAL,
            message=f"NVR '{nvr_name}' — dung lượng tới hạn: {reason}.",
        )
    elif outcome.new_status == StorageStatus.WARNING and outcome.used_pct is not None:
        await _create_alert(
            session,
            nvr_id=nvr_id,
            alert_type=AlertType.HDD_FULL,
            severity=AlertSeverity.WARNING,
            message=f"NVR '{nvr_name}' — cảnh báo lưu trữ: {reason}.",
        )
    elif outcome.new_status == StorageStatus.HEALTHY:
        await _resolve_open_alerts(session, nvr_id, AlertType.HDD_FULL)

    # 3. Hồi phục: vừa từ trạng thái lỗi/cảnh báo trở lại Healthy.
    if (
        outcome.new_status == StorageStatus.HEALTHY
        and outcome.prev_status in (StorageStatus.WARNING, StorageStatus.CRITICAL)
    ):
        await _create_alert(
            session,
            nvr_id=nvr_id,
            alert_type=AlertType.STORAGE_RECOVERED,
            severity=AlertSeverity.INFO,
            message=f"NVR '{nvr_name}' — lưu trữ đã trở lại bình thường.",
            is_event=True,
        )
