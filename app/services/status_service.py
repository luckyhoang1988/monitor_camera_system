"""Áp state machine, cập nhật trạng thái NVR/camera vào DB và ghi *_status_logs.

Tách riêng phần I/O DB khỏi logic kiểm tra (checker/camera_checker) để dễ test.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.collector.camera_checker import evaluate_cameras
from app.collector.checker import (
    apply_state_machine,
    check_nvr,
    fetch_nvr_channels,
    fetch_nvr_storage,
)
from app.collector.storage_checker import estimate_retention_days, evaluate_storage
from app.config import get_settings
from app.db.models import (
    CameraChannel,
    CameraStatusLog,
    NVRDevice,
    NVRHdd,
    NVRStatusLog,
    NVRStorageLog,
)
from app.enums import CameraStatus, NVRStatus, StorageStatus
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
    return datetime.now(UTC)


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
class CameraEvent:
    """Một camera có chuyển trạng thái đáng báo cáo, kèm thông tin định danh.

    Đủ để dựng nội dung alert chi tiết: camera kênh nào, tên gì, thuộc NVR nào
    (nvr_name do caller biết).
    """

    camera_id: int
    channel_no: int
    name: str | None


@dataclass
class CameraScanOutcome:
    """Kết quả quét camera 1 NVR.

    `ok=False` nghĩa là KHÔNG lấy được dữ liệu tin cậy (fetch lỗi/timeout hoặc NVR
    không trả kênh nào) — caller phải BỎ QUA xử lý alert để tránh resolve nhầm.
    Chỉ khi `ok=True` thì các danh sách sự kiện mới phản ánh trạng thái thật:
    - `offline_alertable`: camera offline liên tục >= ngưỡng phút (đủ điều kiện alert).
    - `recovered`: camera vừa chuyển từ offline -> online ở lần quét này.
    """

    ok: bool
    offline_alertable: list[CameraEvent]
    recovered: list[CameraEvent]
    # True nếu có ít nhất 1 camera đổi current_status ở lần quét này -> báo UI re-fetch.
    changed: bool = False


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
        return CameraScanOutcome(ok=False, offline_alertable=[], recovered=[])
    offline_alertable, recovered, changed = await _update_cameras(
        session, nvr.id, channels
    )
    return CameraScanOutcome(
        ok=True,
        offline_alertable=offline_alertable,
        recovered=recovered,
        changed=changed,
    )


async def log_cameras_unreachable(session: AsyncSession, nvr: NVRDevice) -> None:
    """Ghi camera_status_logs = Unknown cho mọi camera của 1 NVR đang KHÔNG Online.

    Khi NVR rớt, job camera không quét được kênh nào -> trước đây không có log nào,
    nên uptime camera trong báo cáo bị "bỏ trống" (không bị trừ thời gian NVR chết).
    Hàm này ghi log Unknown ("không đo được vì NVR offline") ở ĐÚNG nhịp job camera để
    tỷ lệ uptime cân bằng với lúc NVR online.

    KHÔNG đổi current_status/offline_since và KHÔNG sinh alert (cảnh báo NVR-level đã
    đảm nhiệm) — đây chỉ là dữ liệu thống kê. Dùng Unknown (không phải Offline) để
    không thổi phồng bảng "top camera offline" — vốn phản ánh lỗi cấp camera, không
    phải lỗi NVR.
    """
    cam_ids = (
        await session.scalars(
            select(CameraChannel.id).where(CameraChannel.nvr_id == nvr.id)
        )
    ).all()
    if not cam_ids:
        return
    msg = f"NVR {nvr.current_status} — không quét được camera"
    for cid in cam_ids:
        session.add(
            CameraStatusLog(
                camera_id=cid, status=CameraStatus.UNKNOWN.value, error_msg=msg
            )
        )
    logger.info(
        "NVR %s không Online -> ghi %d log camera Unknown (cho báo cáo uptime)",
        nvr.id,
        len(cam_ids),
    )


@dataclass
class StorageScanOutcome:
    """Kết quả quét lưu trữ 1 NVR (job storage) — phục vụ sinh alert lưu trữ.

    `ok=False`: không lấy được dữ liệu tin cậy (fetch lỗi/timeout) -> caller BỎ QUA
    alert để tránh resolve nhầm (giống CameraScanOutcome).
    """

    ok: bool
    prev_status: StorageStatus
    new_status: StorageStatus
    has_disk_error: bool = False
    reason: str | None = None  # mô tả gộp lý do (để dựng nội dung alert)
    # True nếu trạng thái lưu trữ đổi so với lần trước -> báo UI re-fetch qua SSE.
    changed: bool = False


async def update_nvr_storage(
    session: AsyncSession,
    nvr: NVRDevice,
    *,
    timeout: int,
    temp_warn_c: int,
) -> StorageScanOutcome:
    """Quét + cập nhật sức khỏe lưu trữ của 1 NVR (job storage; gọi cho NVR Online).

    Mô phỏng `update_nvr_cameras`: fetch storage, đánh giá thuần bằng `evaluate_storage`,
    upsert `nvr_hdd` theo (nvr_id, hdd_id), cập nhật cột tóm tắt trên NVRDevice và ghi 1
    dòng `nvr_storage_logs`. Fetch lỗi -> `ok=False`, KHÔNG đổi trạng thái.
    """
    settings = get_settings()
    password = decrypt_password(nvr.password_enc)
    prev_status = StorageStatus(nvr.storage_status)

    # Giảm request: thôi dò S.M.A.R.T nếu đã biết firmware không hỗ trợ; chỉ lấy lại
    # bitrate khi chưa có hoặc đã quá hạn cache (bitrate đổi rất chậm).
    probe_smart = nvr.smart_supported is not False
    fetch_bitrate = nvr.bitrate_checked_at is None or (
        _now() - nvr.bitrate_checked_at
    ) > timedelta(seconds=settings.bitrate_refresh_sec)

    storage, error = await fetch_nvr_storage(
        host=nvr.host,
        username=nvr.username,
        password=password,
        port=nvr.http_port,
        use_https=nvr.use_https,
        timeout=timeout,
        tls_fingerprint=nvr.tls_fingerprint,
        retries=settings.request_retries,
        retry_backoff_base=settings.retry_backoff_base,
        probe_smart=probe_smart,
        fetch_bitrate=fetch_bitrate,
    )
    if error or storage is None:
        if error:
            logger.warning(
                "Storage fetch NVR %s thất bại, giữ nguyên trạng thái lưu trữ: %s",
                nvr.id,
                error,
            )
        nvr.storage_last_checked_at = _now()
        nvr.storage_last_error = error
        return StorageScanOutcome(
            ok=False, prev_status=prev_status, new_status=prev_status
        )

    ev = evaluate_storage(storage, temp_warn_c=temp_warn_c)
    now = _now()

    # Xóa-rồi-ghi-lại toàn bộ ổ của NVR: NVR RAID có volume ảo + đĩa vật lý trùng id
    # nên không upsert theo (nvr_id, hdd_id) được; số ổ ít (<=16) nên delete+insert rẻ.
    await session.execute(delete(NVRHdd).where(NVRHdd.nvr_id == nvr.id))
    await session.flush()
    for hdd in storage.hdds:
        session.add(
            NVRHdd(
                nvr_id=nvr.id,
                hdd_id=hdd.hdd_id,
                hdd_type=hdd.hdd_type,
                name=hdd.name,
                capacity_mb=hdd.capacity_mb,
                free_mb=hdd.free_mb,
                status=hdd.status,
                is_recording=hdd.is_recording,
                smart_health=hdd.smart_health,
                temperature_c=hdd.temperature_c,
                last_checked_at=now,
            )
        )

    # Cập nhật cột tóm tắt trên NVR.
    nvr.storage_status = ev.overall.value
    nvr.storage_total_mb = ev.total_mb
    nvr.storage_free_mb = ev.free_mb
    nvr.storage_used_pct = ev.used_pct
    nvr.hdd_count = ev.hdd_count
    nvr.hdd_healthy_count = ev.hdd_healthy_count
    nvr.raid_status = ev.raid_status
    # Nhớ kết quả dò S.M.A.R.T để vòng sau khỏi dò nếu firmware không hỗ trợ.
    if storage.smart_supported is not None:
        nvr.smart_supported = storage.smart_supported
    # Bitrate: chỉ cập nhật khi vừa lấy mới; ngược lại giữ giá trị cache.
    if fetch_bitrate and storage.total_bitrate_kbps is not None:
        nvr.record_bitrate_kbps = storage.total_bitrate_kbps
        nvr.bitrate_checked_at = now
    # Dự đoán số ngày lưu trữ từ dung lượng + bitrate (cache hoặc vừa lấy).
    nvr.retention_days_est = estimate_retention_days(
        ev.total_mb, nvr.record_bitrate_kbps
    )
    nvr.storage_last_checked_at = now
    nvr.storage_last_error = None

    reason = "; ".join(ev.reasons) if ev.reasons else None
    session.add(
        NVRStorageLog(
            nvr_id=nvr.id,
            overall_status=ev.overall.value,
            total_mb=ev.total_mb,
            free_mb=ev.free_mb,
            used_pct=ev.used_pct,
            hdd_error_count=ev.hdd_error_count,
            error_msg=reason,
        )
    )

    changed = ev.overall != prev_status
    if changed:
        log = (
            logger.warning
            if ev.overall in {StorageStatus.WARNING, StorageStatus.CRITICAL}
            else logger.info
        )
        log(
            "NVR %s lưu trữ %s -> %s: %s",
            nvr.id,
            prev_status.value,
            ev.overall.value,
            reason or "-",
        )

    return StorageScanOutcome(
        ok=True,
        prev_status=prev_status,
        new_status=ev.overall,
        has_disk_error=ev.has_disk_error,
        reason=reason,
        changed=changed,
    )


async def _update_cameras(
    session: AsyncSession, nvr_id: int, channels: list
) -> tuple[list[CameraEvent], list[CameraEvent], bool]:
    """Upsert camera_channels theo (nvr_id, channel_no), ghi camera_status_logs.

    Theo dõi `offline_since` cho từng camera (set khi chuyển offline, clear khi
    online lại). Trả về `(offline_alertable, recovered, changed)`:
    - `offline_alertable`: camera offline liên tục >= `camera_offline_alert_min`
      phút — tập đủ điều kiện sinh alert (xem alert_service §5).
    - `recovered`: camera vừa chuyển từ offline -> online ở lần quét này.
    - `changed`: có ít nhất 1 camera đổi current_status (gồm cả camera mới) -> báo
      UI re-fetch qua SSE.
    Mỗi phần tử trong 2 danh sách là `CameraEvent` (camera_id/channel_no/name) để dựng
    alert chi tiết.
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
    evaluated = evaluate_cameras(channels)

    # Bước 1: upsert hàng camera (tạo mới nếu chưa có) + ghi nhận chuyển trạng thái.
    rows: list[tuple[CameraChannel, object]] = []
    offline_rows: list[CameraChannel] = []
    recovered_rows: list[CameraChannel] = []
    changed = False
    for cam in evaluated:
        row = existing.get(cam.channel_no)
        # Trạng thái ở lần quét trước (None nếu camera mới xuất hiện).
        prev_status = row.current_status if row is not None else None
        if prev_status != cam.status.value:
            changed = True
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
                offline_rows.append(row)
        elif cam.status in _CAMERA_UP_STATES:
            # Chỉ xóa mốc offline khi chắc chắn camera đã lên lại (Online) hoặc bị
            # tắt có chủ đích (Disabled). Trạng thái không tin cậy (Unknown/Auth
            # Failed/No Signal) -> giữ nguyên offline_since (không set, không xóa).
            row.offline_since = None
        # Hồi phục: kênh đang offline ở lần trước nay đã Online lại.
        if cam.status == CameraStatus.ONLINE and prev_status == CameraStatus.OFFLINE.value:
            recovered_rows.append(row)
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

    def _event(row: CameraChannel) -> CameraEvent:
        return CameraEvent(camera_id=row.id, channel_no=row.channel_no, name=row.name)

    return (
        [_event(r) for r in offline_rows],
        [_event(r) for r in recovered_rows],
        changed,
    )
