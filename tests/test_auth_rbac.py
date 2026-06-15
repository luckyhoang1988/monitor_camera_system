"""Test phân quyền (RBAC): token mang role, dependency guard, middleware.

Tránh chạm DB qua TestClient (asyncpg + ProactorEventLoop của Windows lỗi khi teardown):
- Phần guard chỉ kiểm route KHÔNG chạm DB hoặc dừng ở tầng 403/redirect trước DB.
- Phần service/DB được test riêng bằng asyncio.run ở môi trường thật, không nằm ở đây.
"""

from __future__ import annotations

import os

import pytest

# Đặt env TRƯỚC mọi import app.* (get_settings dùng lru_cache — chỉ đọc env lần đầu).
os.environ["SECRET_KEY"] = "test-secret-rbac"
os.environ["AUTH_ENABLED"] = "true"

from app.auth import (  # noqa: E402
    SESSION_COOKIE,
    create_session_token,
    verify_session_token,
)
from app.enums import UserRole  # noqa: E402


# --------------------------------------------------------------------------- #
# Token mang role (round-trip)
# --------------------------------------------------------------------------- #
def test_session_token_roundtrip_keeps_role():
    for role in (UserRole.ADMIN.value, UserRole.VIEWER.value):
        token = create_session_token("alice", role, ttl_seconds=60)
        su = verify_session_token(token)
        assert su is not None
        assert su.username == "alice"
        assert su.role == role
    assert verify_session_token(create_session_token("a", "admin", ttl_seconds=60)).is_admin


def test_session_token_username_with_separator():
    # Username chứa '|' không được làm hỏng việc tách trường.
    token = create_session_token("a|b|c", "viewer", ttl_seconds=60)
    su = verify_session_token(token)
    assert su is not None and su.username == "a|b|c" and su.role == "viewer"


def test_session_token_tampered_or_expired():
    token = create_session_token("bob", "admin", ttl_seconds=60)
    assert verify_session_token(token + "x") is None  # chữ ký sai
    assert verify_session_token(None) is None
    assert verify_session_token("rác") is None
    assert verify_session_token(create_session_token("bob", "admin", ttl_seconds=-1)) is None


# --------------------------------------------------------------------------- #
# Guard qua TestClient (không chạm DB)
# --------------------------------------------------------------------------- #
@pytest.fixture
def client():
    os.environ["AUTH_ENABLED"] = "true"
    os.environ["SECRET_KEY"] = "test-secret-rbac"
    from starlette.testclient import TestClient

    import app.main as m

    return TestClient(m.app, follow_redirects=False)


def _headers(role: str) -> dict:
    token = create_session_token("tester", role, ttl_seconds=300)
    return {"Cookie": f"{SESSION_COOKIE}={token}"}


def test_unauthenticated_redirects_to_login(client):
    r = client.get("/users")
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_viewer_forbidden_on_admin_routes(client):
    headers = _headers(UserRole.VIEWER.value)
    # require_admin chạy trước thân route nên 403 xảy ra TRƯỚC khi chạm DB.
    for path in ("/users", "/users/new", "/nvrs/new"):
        r = client.get(path, headers=headers)
        assert r.status_code == 403, (path, r.status_code)
        assert "Không có quyền" in r.text


def test_admin_allowed_on_admin_get_routes(client):
    headers = _headers(UserRole.ADMIN.value)
    # /users/new và /nvrs/new không chạm DB ở GET -> phải 200.
    for path in ("/users/new", "/nvrs/new"):
        r = client.get(path, headers=headers)
        assert r.status_code == 200, (path, r.status_code)


def test_login_page_public(client):
    r = client.get("/login")
    assert r.status_code == 200
