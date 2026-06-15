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
    request_timeout: int = 10
    max_concurrency: int = 20
    fail_threshold: int = 3

    # --- Ngưỡng cảnh báo ---
    slow_response_ms: int = 5000
    camera_offline_alert_min: int = 10

    # --- Retention ---
    log_retention_days: int = 90

    # --- App ---
    timezone: str = "Asia/Ho_Chi_Minh"
    debug: bool = True
    echo_sql: bool = False  # bật để in câu lệnh SQL (chỉ khi debug DB)


@lru_cache
def get_settings() -> Settings:
    """Trả về Settings (cache 1 instance cho toàn app)."""
    return Settings()
