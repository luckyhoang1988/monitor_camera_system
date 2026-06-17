"""Áp state machine, cập nhật trạng thái NVR/camera vào DB và ghi *_status_logs.

Tách riêng phần I/O DB khỏi logic kiểm tra (checker/camera_checker) để dễ test.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.collector.camera_checker import evaluate_cameras
from app.collector.checker import apply_state_machine, check_nvr, fetch_nvr_channels
from app.config import get_settings
from app.db.models import CameraChannel, CameraStatusLog, NVRDevice, NVRStatusLog
from app.enums import CameraStatus, NVRStatus
from app.security import decrypt_password

logger = logging.getLogger("chek_nvr.status")

# Trạng thái camera coi là ĐÁNG TIN & không-offline -> được phép xóa mốc offline_since.
# Các trạng thái còn lại (UNKNOWN/AUTH_FAILED/NO_SIGNAL) là không chắc chắn: giữ nguyên
# offline_since để không reset oan đồng hồ đếm thời gian offline.
_CAMERA_UP_STATES = {CameraStatus.ONLINE, CameraStatus.DISABLED}

# Trạng thái NVR coi là "đang lỗi" — log ở mức warning khi chuyển vào.
_NVR_BAD_STATES = {
    NVRStatus.OFFLINE,
    NVRStatus.AUTH_ERROR,
    NVRStatus.NETWORK_ERROR,
    NVRStatus.WARNING,
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class NVRHealthOutcome:
    """Kết quả kiểm tra sức khỏe 1 NVR (job health) — phục vụ sinh alert NVR."""

    nvr_id: int
    prev_status: NVRStatus
    new_status: NVRStatus
    response_time_ms: int | None


async def check_and_update_nvr_health(
    session: AsyncSession,
    nvr: NVRDevice,
    *,
    fail_threshold: int,
    timeout: int,
) -> NVRHealthOutcome:
    """Kiểm tra sức khỏe NVR (ping/port/deviceInfo), áp state machine, ghi log NVR.

    KHÔNG đụng camera — camera được quét ở job riêng (`update_nvr_cameras`).
    """
    prev_status = NVRStatus(nvr.current_status)
    password = decrypt_password(nvr.password_enc)
    settings = get_settings()

    result = await check_nvr(
        host=nvr.host,
        username=nvr.username,
        password=password,
        port=nvr.http_port,
        use_https=nvr.use_https,
        timeout=timeout,
        tls_fingerprint=nvr.tls_fingerprint,
        retries=settings.request_retries,
        retry_backoff_base=settings.retry_backoff_base,
        fetch_channels=False,
    )

    new_status, new_fail_count = apply_state_machine(
        result.raw_status, nvr.fail_count, fail_threshold
    )

    nvr.current_status = new_status.value
    nvr.fail_count = new_fail_count
    nvr.last_checked_at = _now()
    nvr.last_error = result.error
    if result.device:
        nvr.model = result.device.model or nvr.model
        nvr.serial = result.device.serial or nvr.serial
        nvr.firmware = result.device.firmware or nvr.firmware

    session.add(
        NVRStatusLog(
            nvr_id=nvr.id,
            status=new_status.value,
            response_time_ms=result.response_time_ms,
            error_msg=result.error,
        )
    )

    if new_status != prev_status:
        log = logger.warning if new_status in _NVR_BAD_STATES else logger.info
        log(
            "NVR %s đổi trạng thái %s -> %s (fail_count=%d): %s",
            nvr.id,
            prev_status.value,
            new_status.value,
            new_fail_count,
            result.error or "-",
        )

    return NVRHealthOutcome(
        nvr_id=nvr.id,
        prev_status=prev_status,
        new_status=new_status,
        response_time_ms=result.response_time_ms,
    )


@dataclass
class CameraScanOutcome:
    """Kết quả quét camera 1 NVR.

    `ok=False` nghĩa là KHÔNG lấy được dữ liệu tin cậy (fetch lỗi/timeout hoặc NVR
    không trả kênh nào) — caller phải BỎ QUA xử lý alert để tránh resolve nhầm.
    Chỉ khi `ok=True` thì `alertable_offline` mới phản ánh trạng thái thật.
    """

    ok: bool
    alertable_offline: int


async def update_nvr_cameras(
    session: AsyncSession,
    nvr: NVRDevice,
    *,
    timeout: int,
) -> CameraScanOutcome:
    """Quét + cập nhật camera của 1 NVR (job camera; gọi cho NVR đang Online).

    Trả về `CameraScanOutcome`. Lỗi fetch chỉ ghi log + báo `ok=False`, KHÔNG đổi
    trạng thái NVR (việc đó thuộc job health) và KHÔNG đụng alert/offline_since.
    """
    settings = get_settings()
    password = decrypt_password(nvr.password_enc)
    channels, error = await fetch_nvr_channels(
        host=nvr.host,
        username=nvr.username,
        password=password,
        port=nvr.http_port,
        use_https=nvr.use_https,
        timeout=timeout,
        tls_fingerprint=nvr.tls_fingerprint,
        retries=settings.request_retries,
        retry_backoff_base=settings.retry_backoff_base,
    )
    if error or not channels:
        if error:
            logger.warning(
                "Camera fetch NVR %s thất bại, giữ nguyên alert/offline_since: %s",
                nvr.id,
                error,
            )
        else:
            logger.info(
                "Camera NVR %s không trả kênh nào — bỏ qua cập nhật camera", nvr.id
            )
        return CameraScanOutcome(ok=False, alertable_offline=0)
    alertable = await _update_cameras(session, nvr.id, channels)
    return CameraScanOutcome(ok=True, alertable_offline=alertable)


async def _update_cameras(
    session: AsyncSession, nvr_id: int, channels: list
) -> int:
    """Upsert camera_channels theo (nvr_id, channel_no), ghi camera_status_logs.

    Theo dõi `offline_since` cho từng camera (set khi chuyển offline, clear khi
    online lại). Trả về **số camera offline liên tục >= `camera_offline_alert_min`
    phút** — đây là tập đủ điều kiện để sinh alert (xem alert_service §5).
    """
    existing = {
        c.channel_no: c
        for c in (
            await session.scalars(
                select(CameraChannel).where(CameraChannel.nvr_id == nvr_id)
            )
        ).all()
    }

    now = _now()
    threshold = timedelta(minutes=get_settings().camera_offline_alert_min)
    alertable_offline = 0
    evaluated = evaluate_cameras(channels)

    # Bước 1: upsert hàng camera (tạo mới nếu chưa có).
    rows: list[tuple[CameraChannel, object]] = []
    for cam in evaluated:
        row = existing.get(cam.channel_no)
        if row is None:
            row = CameraChannel(nvr_id=nvr_id, channel_no=cam.channel_no)
            session.add(row)
        row.name = cam.name or row.name
        row.camera_ip = cam.ip or row.camera_ip
        row.current_status = cam.status.value
        row.last_checked_at = now
        row.last_error = cam.error
        if cam.status == CameraStatus.OFFLINE:
            if row.offline_since is None:
                row.offline_since = now
            if now - row.offline_since >= threshold:
                alertable_offline += 1
        elif cam.status in _CAMERA_UP_STATES:
            # Chỉ xóa mốc offline khi chắc chắn camera đã lên lại (Online) hoặc bị
            # tắt có chủ đích (Disabled). Trạng thái không tin cậy (Unknown/Auth
            # Failed/No Signal) -> giữ nguyên offline_since (không set, không xóa).
            row.offline_since = None
        rows.append((row, cam))

    # Bước 2: flush để hàng mới có id, rồi ghi log theo camera_id.
    await session.flush()
    for row, cam in rows:
        session.add(
            CameraStatusLog(
                camera_id=row.id,
                status=cam.status.value,
                error_msg=cam.error,
            )
        )

    return alertable_offline
