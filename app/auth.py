"""Xác thực & phiên đăng nhập cho dashboard.

Cố tình chỉ dùng thư viện chuẩn (không thêm dependency):
- Băm mật khẩu bằng PBKDF2-HMAC-SHA256 (có salt ngẫu nhiên cho mỗi mật khẩu).
- Phiên đăng nhập = cookie ký HMAC-SHA256 *stateless* (không cần tra DB mỗi request),
  chứa `username` + thời điểm hết hạn. Bí mật ký lấy từ `secret_key` (fallback
  `encryption_key`) trong config.

So sánh chữ ký / hash luôn dùng `hmac.compare_digest` để tránh timing attack.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import User
from app.enums import UserRole

SESSION_COOKIE = "chek_session"

_PBKDF2_ITERATIONS = 240_000
_PBKDF2_ALGO = "pbkdf2_sha256"


# --------------------------------------------------------------------------- #
# Băm mật khẩu
# --------------------------------------------------------------------------- #
def hash_password(plaintext: str) -> str:
    """Trả chuỗi `pbkdf2_sha256$iterations$salt_hex$hash_hex` để lưu DB."""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac(
        "sha256", plaintext.encode(), salt, _PBKDF2_ITERATIONS
    )
    return f"{_PBKDF2_ALGO}${_PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(plaintext: str, stored: str) -> bool:
    """Kiểm tra mật khẩu so với chuỗi hash đã lưu (an toàn timing)."""
    try:
        algo, iterations_s, salt_hex, hash_hex = stored.split("$")
        if algo != _PBKDF2_ALGO:
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", plaintext.encode(), bytes.fromhex(salt_hex), int(iterations_s)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk.hex(), hash_hex)


# --------------------------------------------------------------------------- #
# Cookie phiên (ký HMAC, stateless)
# --------------------------------------------------------------------------- #
def _secret() -> bytes:
    settings = get_settings()
    key = settings.secret_key or settings.encryption_key
    if not key:
        raise RuntimeError(
            "SECRET_KEY (hoặc ENCRYPTION_KEY) chưa cấu hình — không thể ký phiên đăng nhập."
        )
    return key.encode()


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64d(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


@dataclass(frozen=True)
class SessionUser:
    """Danh tính lấy từ cookie phiên (không tra DB)."""

    username: str
    role: str

    @property
    def is_admin(self) -> bool:
        return self.role == UserRole.ADMIN.value


def create_session_token(
    username: str, role: str, *, ttl_seconds: int | None = None
) -> str:
    """Sinh token phiên cho `username`+`role`, hết hạn sau `ttl_seconds`.

    Định dạng payload: `role|expires|username` — đặt username CUỐI để username chứa
    ký tự `|` cũng không làm hỏng việc tách trường.
    """
    if ttl_seconds is None:
        ttl_seconds = get_settings().session_ttl_hours * 3600
    expires = int(time.time()) + ttl_seconds
    payload = f"{role}|{expires}|{username}"
    payload_b64 = _b64e(payload.encode())
    sig = hmac.new(_secret(), payload_b64.encode(), hashlib.sha256).digest()
    return f"{payload_b64}.{_b64e(sig)}"


def verify_session_token(token: str | None) -> SessionUser | None:
    """Trả về SessionUser nếu token hợp lệ & chưa hết hạn, ngược lại None."""
    if not token or "." not in token:
        return None
    payload_b64, sig_b64 = token.rsplit(".", 1)
    expected = hmac.new(_secret(), payload_b64.encode(), hashlib.sha256).digest()
    try:
        if not hmac.compare_digest(expected, _b64d(sig_b64)):
            return None
        role, expires_s, username = _b64d(payload_b64).decode().split("|", 2)
    except (ValueError, TypeError):
        return None
    if int(expires_s) < int(time.time()):
        return None
    return SessionUser(username=username, role=role)


# --------------------------------------------------------------------------- #
# Dependencies phân quyền (đọc request.state do middleware gắn sẵn)
# --------------------------------------------------------------------------- #
def require_login(request: Request) -> SessionUser:
    """Yêu cầu đã đăng nhập. Trả về SessionUser; 401 nếu chưa."""
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    return user


def require_admin(request: Request) -> SessionUser:
    """Yêu cầu vai trò admin. 401 nếu chưa đăng nhập, 403 nếu không phải admin."""
    user = require_login(request)
    if user.role != UserRole.ADMIN.value:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    return user


# --------------------------------------------------------------------------- #
# Xác thực với DB
# --------------------------------------------------------------------------- #
async def authenticate(
    session: AsyncSession, username: str, password: str
) -> User | None:
    """Trả về User nếu đúng tài khoản/mật khẩu, ngược lại None."""
    user = (
        await session.scalars(select(User).where(User.username == username))
    ).first()
    if user is None:
        # Vẫn băm một lần để giảm chênh lệch thời gian (chống user-enumeration).
        verify_password(password, hash_password("dummy"))
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user
