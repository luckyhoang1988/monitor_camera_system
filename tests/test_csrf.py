"""Test bảo vệ CSRF cho POST form (dùng /logout — không chạm DB)."""

from starlette.testclient import TestClient

from app.csrf import CSRF_COOKIE
from app.main import app

client = TestClient(app)


def test_post_without_csrf_token_rejected():
    client.cookies.clear()
    r = client.post("/logout", follow_redirects=False)
    assert r.status_code == 403


def test_post_with_mismatched_csrf_rejected():
    client.cookies.clear()
    client.cookies.set(CSRF_COOKIE, "aaa")
    r = client.post(
        "/logout", data={"csrf_token": "bbb"}, follow_redirects=False
    )
    assert r.status_code == 403


def test_post_with_matching_csrf_ok():
    client.cookies.clear()
    client.cookies.set(CSRF_COOKIE, "tok123")
    r = client.post(
        "/logout", data={"csrf_token": "tok123"}, follow_redirects=False
    )
    assert r.status_code == 303


def test_safe_get_not_blocked():
    client.cookies.clear()
    r = client.get("/login", follow_redirects=False)
    assert r.status_code == 200
