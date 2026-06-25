"""Enum trạng thái chuẩn hóa — dùng nhất quán toàn hệ thống (xem CLAUDE.md §6)."""

from enum import Enum


class NVRStatus(str, Enum):
    ONLINE = "Online"
    OFFLINE = "Offline"
    WARNING = "Warning"
    AUTH_ERROR = "Auth Error"
    NETWORK_ERROR = "Network Error"


# NVR coi là "đã chốt chết" (confirmed down). KHÁC `Warning` — Warning là trạng thái
# chập chờn chưa kết luận (state machine chống flapping, chưa escalate sang Offline).
# Dùng THỐNG NHẤT cho mọi quyết định "NVR coi như chết": sinh alert, đếm/hiển thị
# camera là mất tín hiệu, ghi log Unknown trừ uptime, đánh dấu dữ liệu camera là cũ.
# Online và Warning KHÔNG nằm trong tập này (camera giữ trạng thái last-known).
NVR_DOWN_STATES = frozenset(
    {NVRStatus.OFFLINE, NVRStatus.NETWORK_ERROR, NVRStatus.AUTH_ERROR}
)
NVR_DOWN_STATE_VALUES = frozenset(s.value for s in NVR_DOWN_STATES)


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
    CAMERA_RECOVERED = "camera_recovered"


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
