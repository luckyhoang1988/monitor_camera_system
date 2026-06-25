"""Chuẩn hóa sức khỏe lưu trữ NVR từ dữ liệu ISAPI Storage (xem CLAUDE.md).

Tách phần logic thuần (không I/O) ra đây để test bằng mock dễ dàng: cho 1 `StorageInfo`
+ các ngưỡng -> ra `StorageEvaluation` (trạng thái tổng hợp + số liệu để ghi log/alert).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.collector.isapi_client import HddInfo, StorageInfo
from app.enums import StorageStatus

# Trạng thái ổ (chuỗi ISAPI, thường-hóa) coi là HỎNG -> Critical.
_HDD_BAD = {"error", "unformatted", "abnormal", "smartfailed", "smartbad"}
# Trạng thái ổ coi là BÌNH THƯỜNG (đang hoạt động/ngủ tiết kiệm điện).
_HDD_OK = {"ok", "normal", "idle", "sleeping", "standby"}
# RAID coi là có sự cố (suy giảm/đang rebuild) -> ít nhất Warning.
_RAID_BAD = {"degraded", "rebuilding", "rebuild", "inactive", "offline", "failed"}


@dataclass
class StorageEvaluation:
    """Kết quả đánh giá sức khỏe lưu trữ 1 NVR (thuần, để ghi DB + sinh alert)."""

    overall: StorageStatus
    total_mb: int
    free_mb: int
    used_pct: float | None
    hdd_count: int
    hdd_healthy_count: int
    hdd_error_count: int
    raid_status: str | None
    # Cờ chi tiết để alert giải thích nguyên nhân.
    has_disk_error: bool = False  # ổ error/unformatted hoặc có ổ không ghi được
    is_full_critical: bool = False  # %dùng >= crit_pct
    reasons: list[str] = field(default_factory=list)


def _is_bad(status: str | None) -> bool:
    s = (status or "").strip().lower().replace(" ", "")
    return s in _HDD_BAD


def _is_ok(status: str | None) -> bool:
    s = (status or "").strip().lower().replace(" ", "")
    return s in _HDD_OK


def evaluate_storage(
    storage: StorageInfo,
    *,
    warn_pct: int,
    crit_pct: int,
    temp_warn_c: int,
) -> StorageEvaluation:
    """Map StorageInfo -> StorageEvaluation theo ngưỡng %dùng/nhiệt độ/RAID.

    Quy tắc (ưu tiên Critical > Warning > Healthy):
    - Critical: có ổ error/unformatted, hoặc KHÔNG ổ nào đang ghi (mất ghi hình),
      hoặc %dùng >= crit_pct.
    - Warning : %dùng >= warn_pct, hoặc RAID degraded, hoặc nhiệt độ ổ >= temp_warn_c.
    - Healthy : còn lại (và có ít nhất 1 ổ).
    - Unknown : không có ổ nào (NVR không gắn ổ / chưa đọc được).
    """
    hdds: list[HddInfo] = storage.hdds
    total_mb = sum(h.capacity_mb or 0 for h in hdds)
    free_mb = sum(h.free_mb or 0 for h in hdds)
    used_pct = (
        round((total_mb - free_mb) / total_mb * 100, 1) if total_mb > 0 else None
    )

    hdd_count = len(hdds)
    error_hdds = [h for h in hdds if _is_bad(h.status)]
    healthy_hdds = [h for h in hdds if _is_ok(h.status)]
    hdd_error_count = len(error_hdds)

    raid_bad = (storage.raid_status or "").strip().lower() in _RAID_BAD
    hot_hdds = [
        h for h in hdds if h.temperature_c is not None and h.temperature_c >= temp_warn_c
    ]
    # "Mất ghi hình": có ổ nhưng không ổ nào đang ghi (R/W). Bỏ qua khi firmware
    # không báo property (tất cả None) để tránh báo nhầm.
    recording_known = any(h.is_recording is not None for h in hdds)
    none_recording = recording_known and not any(h.is_recording for h in hdds)

    reasons: list[str] = []
    has_disk_error = bool(error_hdds) or none_recording
    is_full_critical = used_pct is not None and used_pct >= crit_pct

    if hdd_count == 0:
        return StorageEvaluation(
            overall=StorageStatus.UNKNOWN,
            total_mb=0,
            free_mb=0,
            used_pct=None,
            hdd_count=0,
            hdd_healthy_count=0,
            hdd_error_count=0,
            raid_status=storage.raid_status,
            reasons=["NVR không có ổ cứng hoặc không đọc được danh sách ổ"],
        )

    # --- Critical ---
    if error_hdds:
        reasons.append(
            f"{len(error_hdds)} ổ lỗi: "
            + ", ".join(f"{h.name or h.hdd_id}={h.status}" for h in error_hdds)
        )
    if none_recording:
        reasons.append("không ổ nào đang ghi hình (R/W)")
    if is_full_critical:
        reasons.append(f"dung lượng đã dùng {used_pct}% ≥ {crit_pct}%")

    if has_disk_error or is_full_critical:
        overall = StorageStatus.CRITICAL
    else:
        # --- Warning ---
        warn_reasons: list[str] = []
        if used_pct is not None and used_pct >= warn_pct:
            warn_reasons.append(f"dung lượng đã dùng {used_pct}% ≥ {warn_pct}%")
        if raid_bad:
            warn_reasons.append(f"RAID {storage.raid_status}")
        if hot_hdds:
            warn_reasons.append(
                "nhiệt độ ổ cao: "
                + ", ".join(f"{h.name or h.hdd_id}={h.temperature_c}°C" for h in hot_hdds)
            )
        if warn_reasons:
            overall = StorageStatus.WARNING
            reasons.extend(warn_reasons)
        else:
            overall = StorageStatus.HEALTHY

    return StorageEvaluation(
        overall=overall,
        total_mb=total_mb,
        free_mb=free_mb,
        used_pct=used_pct,
        hdd_count=hdd_count,
        hdd_healthy_count=len(healthy_hdds),
        hdd_error_count=hdd_error_count,
        raid_status=storage.raid_status,
        has_disk_error=has_disk_error,
        is_full_critical=is_full_critical,
        reasons=reasons,
    )
