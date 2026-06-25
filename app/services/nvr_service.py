"""Thao tác ghi cho NVR: tạo / sửa / xóa / kiểm tra ngay (dùng cho web CRUD)."""

from __future__ import annotations

from sqlalchemy import delete as sa_delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import NVRDevice
from app.enums import NVRStatus
from app.security import encrypt_password
from app.services.alert_service import process_camera_alerts, process_nvr_alerts
from app.services.status_service import (
    check_and_update_nvr_health,
    update_nvr_cameras,
)
from app.services.telegram_notifier import flush_telegram_notifications


async def create_nvr(session: AsyncSession, data: dict) -> NVRDevice:
    """Tạo NVR mới. `data['password']` là plaintext -> mã hóa trước khi lưu."""
    nvr = NVRDevice(
        name=data["name"],
        host=data["host"],
        http_port=data.get("http_port", 80),
        use_https=data.get("use_https", False),
        username=data["username"],
        password_enc=encrypt_password(data["password"]),
        tls_fingerprint=(data.get("tls_fingerprint") or "").strip() or None,
        location=data.get("location") or None,
        area=data.get("area") or None,
        model=data.get("model") or None,
        channel_count=data.get("channel_count") or None,
        note=data.get("note") or None,
        enabled=data.get("enabled", True),
    )
    session.add(nvr)
    await session.commit()
    return nvr


async def update_nvr(session: AsyncSession, nvr_id: int, data: dict) -> NVRDevice | None:
    """Cập nhật NVR. Chỉ đổi mật khẩu khi `data['password']` không rỗng."""
    nvr = await session.get(NVRDevice, nvr_id)
    if nvr is None:
        return None
    nvr.name = data["name"]
    nvr.host = data["host"]
    nvr.http_port = data.get("http_port", 80)
    nvr.use_https = data.get("use_https", False)
    nvr.username = data["username"]
    nvr.tls_fingerprint = (data.get("tls_fingerprint") or "").strip() or None
    nvr.location = data.get("location") or None
    nvr.area = data.get("area") or None
    nvr.model = data.get("model") or None
    nvr.channel_count = data.get("channel_count") or None
    nvr.note = data.get("note") or None
    nvr.enabled = data.get("enabled", True)
    if data.get("password"):
        nvr.password_enc = encrypt_password(data["password"])
    await session.commit()
    return nvr


async def delete_nvr(session: AsyncSession, nvr_id: int) -> bool:
    result = await session.execute(sa_delete(NVRDevice).where(NVRDevice.id == nvr_id))
    await session.commit()
    return bool(result.rowcount)


async def check_nvr_now(session: AsyncSession, nvr_id: int) -> bool:
    """Kiểm tra ngay 1 NVR đầy đủ (health + camera nếu Online). False nếu không có."""
    settings = get_settings()
    nvr = await session.get(NVRDevice, nvr_id)
    if nvr is None:
        return False
    nvr_name = nvr.name
    outcome = await check_and_update_nvr_health(
        session,
        nvr,
        fail_threshold=settings.fail_threshold,
        timeout=settings.request_timeout,
    )
    await process_nvr_alerts(session, outcome, nvr_name)
    # Khi NVR Online thì quét luôn camera để nút "Kiểm tra ngay" phản ánh đầy đủ.
    if outcome.new_status == NVRStatus.ONLINE:
        cam_outcome = await update_nvr_cameras(
            session, nvr, timeout=settings.request_timeout
        )
        # Fetch camera lỗi (ok=False) -> giữ nguyên alert, không resolve nhầm.
        if cam_outcome.ok:
            await process_camera_alerts(
                session, nvr_id, nvr_name, cam_outcome
            )
    await session.commit()
    await flush_telegram_notifications(session)
    return True
