"""Chuẩn hóa sức khỏe lưu trữ NVR từ dữ liệu ISAPI Storage (xem CLAUDE.md).

Tách phần logic thuần (không I/O) ra đây để test bằng mock dễ dàng: cho 1 `StorageInfo`
+ ngưỡng nhiệt độ -> ra `StorageEvaluation` (trạng thái tổng hợp + số liệu để ghi log/alert).

LƯU Ý: NVR ghi đè (circular recording) nên ĐĨA ĐẦY LÀ BÌNH THƯỜNG, không phải sự cố.
Vì vậy %dung lượng KHÔNG quyết định trạng thái Warning/Critical (chỉ để hiển thị). Trạng
thái chỉ phản ánh hỏng hóc thật: ổ error/chưa format, mất ghi hình, RAID suy giảm, nhiệt độ.
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
    used_pct: float | None  # chỉ để hiển thị, KHÔNG quyết định trạng thái
    hdd_count: int
    hdd_healthy_count: int
    hdd_error_count: int
    raid_status: str | None
    # Cờ chi tiết để alert giải thích nguyên nhân (đầy KHÔNG còn là lỗi).
    has_disk_error: bool = False  # ổ error/unformatted hoặc có ổ không ghi được
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
    temp_warn_c: int,
) -> StorageEvaluation:
    """Map StorageInfo -> StorageEvaluation. %dung lượng KHÔNG ảnh hưởng trạng thái.

    Quy tắc (ưu tiên Critical > Warning > Healthy):
    - Critical: có ổ error/unformatted, hoặc KHÔNG ổ nào đang ghi (mất ghi hình).
    - Warning : RAID degraded, hoặc nhiệt độ ổ >= temp_warn_c.
    - Healthy : còn lại (kể cả khi đĩa đã đầy — NVR ghi đè là bình thường).
    - Unknown : không có ổ nào (NVR không gắn ổ / chưa đọc được).
    """
    hdds: list[HddInfo] = storage.hdds

    # Volume ghi (property RW): với NVR RAID là "Virtual Disk", với NVR thường là chính
    # các đĩa vật lý RW. Dung lượng/%/số ngày lưu CHỈ tính trên đây để không cộng trùng
    # đĩa thành viên (RO, freeSpace=0) làm %đầy sai.
    recording = [h for h in hdds if h.is_recording]
    cap_src = recording if recording else hdds
    total_mb = sum(h.capacity_mb or 0 for h in cap_src)
    free_mb = sum(h.free_mb or 0 for h in cap_src)
    used_pct = (
        round((total_mb - free_mb) / total_mb * 100, 1) if total_mb > 0 else None
    )

    # Đếm "số ổ" theo đĩa VẬT LÝ (loại trừ volume ảo) cho dễ hiểu; nếu không có đĩa
    # vật lý nào (không phải RAID) thì đếm toàn bộ.
    physical = [
        h for h in hdds if not (h.hdd_type or "").lower().startswith("virtual")
    ]
    count_src = physical if physical else hdds
    hdd_count = len(count_src)
    healthy_hdds = [h for h in count_src if _is_ok(h.status)]
    # Lỗi xét trên MỌI ổ (gồm cả đĩa thành viên RAID).
    error_hdds = [h for h in hdds if _is_bad(h.status)]
    hdd_error_count = len(error_hdds)

    raid_bad = (storage.raid_status or "").strip().lower() in _RAID_BAD
    hot_hdds = [
        h for h in hdds if h.temperature_c is not None and h.temperature_c >= temp_warn_c
    ]
    # "Mất ghi hình": có ổ nhưng không volume nào đang ghi (R/W). Bỏ qua khi firmware
    # không báo property (tất cả None) để tránh báo nhầm.
    recording_known = any(h.is_recording is not None for h in hdds)
    none_recording = recording_known and not any(h.is_recording for h in hdds)

    reasons: list[str] = []
    has_disk_error = bool(error_hdds) or none_recording

    if not hdds:
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

    # --- Critical (hỏng thật) ---
    if error_hdds:
        reasons.append(
            f"{len(error_hdds)} ổ lỗi: "
            + ", ".join(f"{h.name or h.hdd_id}={h.status}" for h in error_hdds)
        )
    if none_recording:
        reasons.append("không ổ nào đang ghi hình (R/W)")

    if has_disk_error:
        overall = StorageStatus.CRITICAL
    else:
        # --- Warning (RAID/nhiệt độ) — KHÔNG tính %đầy ---
        warn_reasons: list[str] = []
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
        reasons=reasons,
    )


def estimate_retention_days(
    total_capacity_mb: int | None,
    total_bitrate_kbps: int | None,
) -> float | None:
    """Dự đoán số ngày lưu trữ từ dung lượng ổ + tổng bitrate ghi (ghi liên tục 24/7).

    Công thức: ngày ≈ dung lượng(MB) × 8000 / (bitrate(kbps) × 86400).
    - 8000 = ×8 bit/byte × (10^6 byte/MB ÷ 10^3 bit/kbit).
    - Giả định ghi LIÊN TỤC (worst-case). Ghi theo chuyển động sẽ lưu được lâu hơn.
    Trả None nếu thiếu dữ liệu (không đọc được bitrate hoặc dung lượng = 0).
    """
    if not total_capacity_mb or not total_bitrate_kbps:
        return None
    days = total_capacity_mb * 8000 / (total_bitrate_kbps * 86400)
    return round(days, 1)
