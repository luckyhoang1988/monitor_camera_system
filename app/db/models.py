"""ORM models cho Chek_NVR (xem CLAUDE.md §7)."""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
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
    StorageStatus,
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

    # --- Tóm tắt sức khỏe lưu trữ (cập nhật bởi job storage; xem CLAUDE.md) ---
    # Lưu sẵn trên NVR để list/detail hiển thị nhanh (giống model/serial/firmware).
    storage_status: Mapped[StorageStatus] = mapped_column(
        String(20), default=StorageStatus.UNKNOWN.value
    )
    storage_total_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    storage_free_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    storage_used_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    hdd_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    hdd_healthy_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    raid_status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # Dự đoán số ngày lưu trữ = dung lượng / tổng bitrate ghi (ghi liên tục 24/7).
    record_bitrate_kbps: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retention_days_est: Mapped[float | None] = mapped_column(Float, nullable=True)
    storage_last_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    storage_last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    cameras: Mapped[list["CameraChannel"]] = relationship(
        back_populates="nvr", cascade="all, delete-orphan"
    )
    hdds: Mapped[list["NVRHdd"]] = relationship(
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


class NVRHdd(Base):
    """Trạng thái HIỆN TẠI của từng ổ cứng trong 1 NVR (mô phỏng camera_channels)."""

    __tablename__ = "nvr_hdd"
    # KHÔNG unique theo (nvr_id, hdd_id): NVR RAID có volume ảo và đĩa vật lý trùng id.
    # Mỗi lượt quét xóa-rồi-ghi-lại toàn bộ ổ của NVR nên không tích lũy trùng.
    __table_args__ = (Index("ix_nvr_hdd_nvr", "nvr_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    nvr_id: Mapped[int] = mapped_column(
        ForeignKey("nvr_devices.id", ondelete="CASCADE")
    )
    hdd_id: Mapped[int] = mapped_column(Integer)
    hdd_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    capacity_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    free_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    is_recording: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    smart_health: Mapped[str | None] = mapped_column(String(40), nullable=True)
    temperature_c: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    nvr: Mapped["NVRDevice"] = relationship(back_populates="hdds")


class NVRStorageLog(Base):
    """Lịch sử sức khỏe lưu trữ của 1 NVR (mô phỏng nvr_status_logs)."""

    __tablename__ = "nvr_storage_logs"
    __table_args__ = (
        Index("ix_nvr_storage_log_nvr_checked", "nvr_id", "checked_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    nvr_id: Mapped[int] = mapped_column(
        ForeignKey("nvr_devices.id", ondelete="CASCADE")
    )
    overall_status: Mapped[StorageStatus] = mapped_column(String(20))
    total_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    free_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    used_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    hdd_error_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
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
