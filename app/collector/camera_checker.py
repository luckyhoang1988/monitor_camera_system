"""Chuẩn hóa trạng thái camera từ dữ liệu ISAPI channels (xem CLAUDE.md §6)."""

from __future__ import annotations

from dataclasses import dataclass

from app.collector.isapi_client import ChannelInfo
from app.enums import CameraStatus


@dataclass
class CameraCheckResult:
    channel_no: int
    name: str | None
    ip: str | None
    status: CameraStatus
    error: str | None = None


def map_channel_status(channel: ChannelInfo) -> CameraStatus:
    """Map cờ online/raw_status của một kênh sang CameraStatus chuẩn hóa."""
    raw = (channel.raw_status or "").strip().lower()

    # Một số firmware trả chuỗi mô tả thay vì true/false.
    if raw in {"online", "true"}:
        return CameraStatus.ONLINE
    if raw in {"offline", "false"}:
        return CameraStatus.OFFLINE
    if raw in {"disabled", "disable"}:
        return CameraStatus.DISABLED
    if "no" in raw and "signal" in raw:  # "no signal", "nosignal"
        return CameraStatus.NO_SIGNAL
    if "auth" in raw or "password" in raw or "login" in raw:
        return CameraStatus.AUTH_FAILED

    # Fallback theo cờ boolean online nếu không có raw_status rõ ràng.
    if channel.online is True:
        return CameraStatus.ONLINE
    if channel.online is False:
        return CameraStatus.OFFLINE
    return CameraStatus.UNKNOWN


def evaluate_cameras(channels: list[ChannelInfo]) -> list[CameraCheckResult]:
    """Chuyển danh sách kênh thô thành kết quả trạng thái camera chuẩn hóa."""
    return [
        CameraCheckResult(
            channel_no=ch.channel_no,
            name=ch.name,
            ip=ch.ip,
            status=map_channel_status(ch),
        )
        for ch in channels
    ]
