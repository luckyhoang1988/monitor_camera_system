"""Pydantic schemas dùng cho API/dashboard."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.enums import (
    AlertSeverity,
    AlertStatus,
    AlertType,
    CameraStatus,
    NVRStatus,
)


class NVRBase(BaseModel):
    name: str
    host: str
    http_port: int = 80
    use_https: bool = False
    username: str
    location: str | None = None
    area: str | None = None
    model: str | None = None
    channel_count: int | None = None
    note: str | None = None


class NVRCreate(NVRBase):
    password: str  # plaintext khi tạo; được mã hóa trước khi lưu


class NVRRead(NVRBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    current_status: NVRStatus
    last_checked_at: datetime | None = None
    last_error: str | None = None
    enabled: bool = True


class CameraRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    nvr_id: int
    channel_no: int
    name: str | None = None
    camera_ip: str | None = None
    camera_type: str | None = None
    location: str | None = None
    current_status: CameraStatus
    last_checked_at: datetime | None = None
    last_error: str | None = None


class AlertRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    type: AlertType
    severity: AlertSeverity
    nvr_id: int | None = None
    camera_id: int | None = None
    message: str
    status: AlertStatus
    created_at: datetime
    resolved_at: datetime | None = None


class SystemOverview(BaseModel):
    """Số liệu khối tổng quan trên dashboard."""

    nvr_total: int = 0
    nvr_online: int = 0
    nvr_offline: int = 0
    nvr_warning: int = 0
    camera_total: int = 0
    camera_online: int = 0
    camera_offline: int = 0
    uptime_ratio: float = 0.0  # % camera online trên tổng
