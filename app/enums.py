"""Enum trạng thái chuẩn hóa — dùng nhất quán toàn hệ thống (xem CLAUDE.md §6)."""

from enum import Enum


class NVRStatus(str, Enum):
    ONLINE = "Online"
    OFFLINE = "Offline"
    WARNING = "Warning"
    AUTH_ERROR = "Auth Error"
    NETWORK_ERROR = "Network Error"


class CameraStatus(str, Enum):
    ONLINE = "Online"
    OFFLINE = "Offline"
    DISABLED = "Disabled"
    NO_SIGNAL = "No Signal"
    AUTH_FAILED = "Auth Failed"
    UNKNOWN = "Unknown"


class AlertType(str, Enum):
    NVR_OFFLINE = "nvr_offline"
    CAMERA_OFFLINE = "camera_offline"
    AUTH_ERROR = "auth_error"
    SLOW_RESPONSE = "slow_response"
    NVR_RECOVERED = "nvr_recovered"


class AlertSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class AlertStatus(str, Enum):
    OPEN = "open"
    RESOLVED = "resolved"


class UserRole(str, Enum):
    ADMIN = "admin"  # toàn quyền: CRUD NVR, quản lý user
    VIEWER = "viewer"  # chỉ xem (read-only)
