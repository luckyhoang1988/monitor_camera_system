"""Bảo vệ CSRF cho các form POST (double-submit cookie).

Cơ chế: phát một token ngẫu nhiên lưu ở cookie `csrf_token` (đọc được bởi JS/template,
KHÔNG httponly). Mỗi form POST nhúng token đó vào hidden field; khi nhận POST, so khớp
field với cookie bằng so sánh hằng-thời-gian. Trình duyệt bên thứ ba không đọc được
cookie cùng-site nên không giả mạo được giá trị field -> chặn CSRF.

Token ổn định theo vòng đời cookie (không đổi mỗi request) để form đã render vẫn khớp.
"""

from __future__ import annotations

import hmac
import secrets

from fastapi import HTTPException, Request, status

CSRF_COOKIE = "csrf_token"
CSRF_FIELD = "csrf_token"
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}


def issue_token() -> str:
    """Sinh token CSRF ngẫu nhiên (url-safe)."""
    return secrets.token_urlsafe(32)


async def verify_csrf(request: Request) -> None:
    """Dependency: kiểm tra token CSRF cho method không an toàn (POST/PUT/...).

    Bỏ qua GET/HEAD/OPTIONS. Sai/thiếu token -> HTTP 403.
    """
    if request.method in _SAFE_METHODS:
        return
    cookie_token = request.cookies.get(CSRF_COOKIE)
    form = await request.form()
    form_token = form.get(CSRF_FIELD)
    if (
        not cookie_token
        or not form_token
        or not hmac.compare_digest(str(cookie_token), str(form_token))
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="CSRF token không hợp lệ."
        )
