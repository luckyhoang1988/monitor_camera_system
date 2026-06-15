"""Mã hóa/giải mã mật khẩu NVR bằng Fernet (xem CLAUDE.md §7)."""

from cryptography.fernet import Fernet

from app.config import get_settings


def _get_fernet() -> Fernet:
    key = get_settings().encryption_key
    if not key:
        raise RuntimeError(
            "ENCRYPTION_KEY chưa được cấu hình. Tạo bằng: "
            'python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"'
        )
    return Fernet(key.encode())


def encrypt_password(plaintext: str) -> str:
    """Mã hóa mật khẩu để lưu vào cột password_enc."""
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt_password(token: str) -> str:
    """Giải mã mật khẩu khi cần gọi ISAPI."""
    return _get_fernet().decrypt(token.encode()).decode()
