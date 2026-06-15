"""Áp state machine, cập nhật trạng thái NVR/camera vào DB và ghi *_status_logs.

Tách riêng phần I/O DB khỏi logic kiểm tra (checker/camera_checker) để dễ test.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.collector.camera_checker import evaluate_cameras
from app.collector.checker import apply_state_machine, check_nvr
from app.db.models import CameraChannel, CameraStatusLog, NVRDevice, NVRStatusLog
from app.enums import CameraStatus, NVRStatus
from app.security import decrypt_password


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class NVRUpdateOutcome:
    """Kết quả sau khi cập nhật 1 NVR — phục vụ sinh alert (bước 8)."""

    nvr_id: int
    prev_status: NVRStatus
    new_status: NVRStatus
    response_time_ms: int | None
    camera_offline_count: int = 0


async def check_and_update_nvr(
    session: AsyncSession,
    nvr: NVRDevice,
    *,
    fail_threshold: int,
    timeout: int,
) -> NVRUpdateOutcome:
    """Kiểm tra 1 NVR, áp state machine, cập nhật DB + ghi log NVR & camera."""
    prev_status = NVRStatus(nvr.current_status)
    password = decrypt_password(nvr.password_enc)

    result = await check_nvr(
        host=nvr.host,
        username=nvr.username,
        password=password,
        port=nvr.http_port,
        use_https=nvr.use_https,
        timeout=timeout,
    )

    new_status, new_fail_count = apply_state_machine(
        result.raw_status, nvr.fail_count, fail_threshold
    )

    # Cập nhật bản ghi NVR.
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

    # Chỉ cập nhật camera khi NVR Online (mới có dữ liệu kênh đáng tin).
    camera_offline = 0
    if new_status == NVRStatus.ONLINE and result.channels:
        camera_offline = await _update_cameras(session, nvr.id, result.channels)

    return NVRUpdateOutcome(
        nvr_id=nvr.id,
        prev_status=prev_status,
        new_status=new_status,
        response_time_ms=result.response_time_ms,
        camera_offline_count=camera_offline,
    )


async def _update_cameras(
    session: AsyncSession, nvr_id: int, channels: list
) -> int:
    """Upsert camera_channels theo (nvr_id, channel_no), ghi camera_status_logs.

    Trả về số camera đang offline.
    """
    existing = {
        c.channel_no: c
        for c in (
            await session.scalars(
                select(CameraChannel).where(CameraChannel.nvr_id == nvr_id)
            )
        ).all()
    }

    offline_count = 0
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
        row.last_checked_at = _now()
        row.last_error = cam.error
        rows.append((row, cam))
        if cam.status == CameraStatus.OFFLINE:
            offline_count += 1

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

    return offline_count
