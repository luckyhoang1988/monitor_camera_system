"""Cấu hình ứng dụng, đọc từ biến môi trường / file .env."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Toàn bộ cấu hình runtime của Chek_NVR."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Database ---
    database_url: str = "postgresql+asyncpg://chek_nvr:password@localhost:5432/chek_nvr"

    # --- Mã hóa mật khẩu NVR (Fernet) ---
    encryption_key: str = ""

    # --- Đăng nhập dashboard ---
    # Bí mật ký cookie phiên. Nếu rỗng, dùng tạm encryption_key. Nên đặt riêng.
    secret_key: str = ""
    session_ttl_hours: int = 12  # thời hạn phiên đăng nhập (giờ)
    auth_enabled: bool = True  # đặt False để tắt yêu cầu đăng nhập (dev)
    # Gắn cờ Secure cho cookie phiên: BẬT (True) khi chạy sau HTTPS, TẮT (False)
    # khi truy cập qua HTTP thuần (vd LAN) nếu không trình duyệt sẽ không gửi cookie.
    cookie_secure: bool = False

    # --- Tần suất quét (giây) ---
    nvr_check_interval: int = 180
    camera_check_interval: int = 600

    # --- Tham số kiểm tra ---
    request_timeout: int = 10  # timeout tổng (giây) — dùng cho ping/port và fallback
    max_concurrency: int = 20
    fail_threshold: int = 3

    # Timeout chi tiết cho HTTP ISAPI (giây). Tránh treo lâu ở một pha.
    connect_timeout: float = 5.0
    read_timeout: float = 10.0
    write_timeout: float = 5.0

    # Retry cho lỗi mạng tạm thời (connect/read timeout) — không retry 401.
    request_retries: int = 2
    retry_backoff_base: float = 0.5  # giây; backoff mũ + jitter

    # --- Bảo mật kết nối NVR (TLS) ---
    # NVR Hikvision thường dùng cert tự ký -> mặc định không verify CA.
    # Production nên pin fingerprint per-NVR (cột tls_fingerprint) để chặn MITM.
    nvr_tls_verify: bool = False  # True để bật xác thực chứng chỉ TLS theo CA
    nvr_ca_cert_path: str | None = None  # CA bundle nội bộ (nếu có)

    # --- Ngưỡng cảnh báo ---
    slow_response_ms: int = 5000
    camera_offline_alert_min: int = 10
    # Ngưỡng màu cho % disk đã dùng (panel giám sát dung lượng ở trang Cảnh báo).
    disk_warn_pct: int = 80  # vàng khi >= ngưỡng này
    disk_crit_pct: int = 90  # đỏ khi >= ngưỡng này

    # --- Cảnh báo qua Telegram (kênh ngoài, tùy chọn) ---
    # Bật để đẩy mỗi alert mới (offline/auth/recovery/slow/camera) lên Telegram.
    telegram_enabled: bool = False
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""  # chat/group/channel id (group thường là số âm)

    # --- Retention ---
    log_retention_days: int = 90

    # --- App ---
    timezone: str = "Asia/Ho_Chi_Minh"
    debug: bool = True
    echo_sql: bool = False  # bật để in câu lệnh SQL (chỉ khi debug DB)

    @property
    def nvr_verify(self) -> bool | str:
        """Giá trị `verify` cho httpx: ưu tiên CA bundle nội bộ, sau đó cờ verify."""
        return self.nvr_ca_cert_path or self.nvr_tls_verify


@lru_cache
def get_settings() -> Settings:
    """Trả về Settings (cache 1 instance cho toàn app)."""
    return Settings()
