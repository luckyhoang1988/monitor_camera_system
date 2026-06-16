"""ORM models cho Chek_NVR (xem CLAUDE.md §7)."""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.enums import (
    AlertSeverity,
    AlertStatus,
    AlertType,
    CameraStatus,
    NVRStatus,
    UserRole,
)


class NVRDevice(Base):
    __tablename__ = "nvr_devices"
    __table_args__ = (
        Index("ix_nvr_status", "current_status"),
        Index("ix_nvr_area_status", "area", "current_status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    host: Mapped[str] = mapped_column(String(255))  # IP hoặc domain
    http_port: Mapped[int] = mapped_column(Integer, default=80)
    use_https: Mapped[bool] = mapped_column(Boolean, default=False)
    username: Mapped[str] = mapped_column(String(120))
    password_enc: Mapped[str] = mapped_column(Text)  # mã hóa Fernet, KHÔNG plaintext
    # SHA-256 fingerprint cert TLS để pin (chống MITM với cert tự ký). NULL = không pin.
    tls_fingerprint: Mapped[str | None] = mapped_column(String(128), nullable=True)
    location: Mapped[str | None] = mapped_column(String(255), nullable=True)
    area: Mapped[str | None] = mapped_column(String(120), nullable=True)
    model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    serial: Mapped[str | None] = mapped_column(String(120), nullable=True)
    firmware: Mapped[str | None] = mapped_column(String(120), nullable=True)
    channel_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Trạng thái hiện tại + bộ đếm xác nhận (state machine chống flapping)
    current_status: Mapped[NVRStatus] = mapped_column(
        String(20), default=NVRStatus.OFFLINE.value
    )
    fail_count: Mapped[int] = mapped_column(Integer, default=0)
    last_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    cameras: Mapped[list["CameraChannel"]] = relationship(
        back_populates="nvr", cascade="all, delete-orphan"
    )


class CameraChannel(Base):
    __tablename__ = "camera_channels"
    __table_args__ = (
        Index("ix_camera_nvr_channel", "nvr_id", "channel_no", unique=True),
        Index("ix_cam_status", "current_status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    nvr_id: Mapped[int] = mapped_column(
        ForeignKey("nvr_devices.id", ondelete="CASCADE")
    )
    channel_no: Mapped[int] = mapped_column(Integer)
    name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    camera_ip: Mapped[str | None] = mapped_column(String(255), nullable=True)
    camera_type: Mapped[str | None] = mapped_column(String(120), nullable=True)
    location: Mapped[str | None] = mapped_column(String(255), nullable=True)

    current_status: Mapped[CameraStatus] = mapped_column(
        String(20), default=CameraStatus.UNKNOWN.value
    )
    last_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Mốc bắt đầu offline liên tục (để tính ngưỡng phút sinh alert). NULL = đang online.
    offline_since: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    nvr: Mapped["NVRDevice"] = relationship(back_populates="cameras")


class NVRStatusLog(Base):
    __tablename__ = "nvr_status_logs"
    __table_args__ = (Index("ix_nvr_log_nvr_checked", "nvr_id", "checked_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    nvr_id: Mapped[int] = mapped_column(
        ForeignKey("nvr_devices.id", ondelete="CASCADE")
    )
    status: Mapped[NVRStatus] = mapped_column(String(20))
    response_time_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_msg: Mapped[str | None] = mapped_column(Text, nullable=True)
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class CameraStatusLog(Base):
    __tablename__ = "camera_status_logs"
    __table_args__ = (Index("ix_cam_log_cam_checked", "camera_id", "checked_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    camera_id: Mapped[int] = mapped_column(
        ForeignKey("camera_channels.id", ondelete="CASCADE")
    )
    status: Mapped[CameraStatus] = mapped_column(String(20))
    error_msg: Mapped[str | None] = mapped_column(Text, nullable=True)
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Alert(Base):
    __tablename__ = "alerts"
    __table_args__ = (Index("ix_alert_status_created", "status", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[AlertType] = mapped_column(String(30))
    severity: Mapped[AlertSeverity] = mapped_column(
        String(20), default=AlertSeverity.WARNING.value
    )
    nvr_id: Mapped[int | None] = mapped_column(
        ForeignKey("nvr_devices.id", ondelete="CASCADE"), nullable=True
    )
    camera_id: Mapped[int | None] = mapped_column(
        ForeignKey("camera_channels.id", ondelete="CASCADE"), nullable=True
    )
    message: Mapped[str] = mapped_column(Text)
    status: Mapped[AlertStatus] = mapped_column(
        String(20), default=AlertStatus.OPEN.value
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class User(Base):
    """Skeleton — bổ sung auth ở giai đoạn sau."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(120), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(30), default=UserRole.VIEWER.value)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
