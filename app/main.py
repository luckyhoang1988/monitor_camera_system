"""Điểm vào FastAPI: khởi tạo app, mount routes và scheduler.

Router (api/web) sẽ được cắm vào ở bước 7-8 (xem lộ trình CLAUDE.md §13).
"""

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.auth import SESSION_COOKIE, SessionUser, verify_session_token
from app.collector.http_pool import close_all as close_http_pool
from app.collector.scheduler import shutdown_scheduler, start_scheduler
from app.config import get_settings
from app.csrf import CSRF_COOKIE, issue_token, verify_csrf
from app.enums import UserRole
from app.web.user_views import router as user_router
from app.web.views import router as web_router
from app.web.views import templates

logging.basicConfig(level=logging.INFO)
settings = get_settings()

# Đường dẫn không yêu cầu đăng nhập.
_PUBLIC_PATHS = {"/login", "/logout", "/health", "/favicon.ico"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    start_scheduler()
    yield
    shutdown_scheduler()
    await close_http_pool()


app = FastAPI(title="Chek_NVR", version="0.1.0", lifespan=lifespan)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """Gắn danh tính từ cookie vào `request.state`; chặn khi chưa đăng nhập.

    Phiên là cookie ký HMAC (stateless) nên không cần truy vấn DB ở mỗi request.
    Khi `auth_enabled=False` (chỉ dev): coi như đã đăng nhập với quyền admin để
    không bị chặn và UI vẫn hiển thị đầy đủ.
    """
    user = verify_session_token(request.cookies.get(SESSION_COOKIE))
    if not get_settings().auth_enabled and user is None:
        user = SessionUser(username="dev", role=UserRole.ADMIN.value)

    # Phơi bày cho route (request.state.user) và template (username/role).
    request.state.user = user
    request.state.username = user.username if user else None
    request.state.role = user.role if user else None

    # CSRF: dùng token cookie sẵn có, hoặc phát mới (set ở response phía dưới).
    csrf_token = request.cookies.get(CSRF_COOKIE)
    is_new_csrf = csrf_token is None
    if is_new_csrf:
        csrf_token = issue_token()
    request.state.csrf_token = csrf_token

    def _with_csrf(resp):
        if is_new_csrf:
            resp.set_cookie(
                CSRF_COOKIE,
                csrf_token,
                samesite="lax",
                secure=get_settings().cookie_secure,
                max_age=get_settings().session_ttl_hours * 3600,
            )
        return resp

    if not get_settings().auth_enabled or request.url.path in _PUBLIC_PATHS:
        return _with_csrf(await call_next(request))

    if user is None:
        # HTMX request -> báo client tự redirect; còn lại redirect thường.
        if request.headers.get("HX-Request"):
            resp = RedirectResponse("/login", status_code=401)
            resp.headers["HX-Redirect"] = "/login"
            return _with_csrf(resp)
        next_url = request.url.path
        return _with_csrf(
            RedirectResponse(f"/login?next={next_url}", status_code=303)
        )

    return _with_csrf(await call_next(request))


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    """403 -> trang "Không có quyền" thân thiện; 401 -> về /login; còn lại mặc định."""
    if exc.status_code == status.HTTP_403_FORBIDDEN:
        return templates.TemplateResponse(request, "403.html", {}, status_code=403)
    if exc.status_code == status.HTTP_401_UNAUTHORIZED:
        return RedirectResponse("/login", status_code=303)
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


@app.get("/health")
async def health() -> dict[str, str]:
    """Health check đơn giản."""
    return {"status": "ok"}


app.include_router(web_router, dependencies=[Depends(verify_csrf)])
app.include_router(user_router, dependencies=[Depends(verify_csrf)])
