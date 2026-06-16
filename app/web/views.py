"""Routes render dashboard bằng Jinja2 + HTMX."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import (
    SESSION_COOKIE,
    authenticate,
    create_session_token,
    require_admin,
    require_login,
)
from app.config import get_settings
from app.db.base import get_session
from app.db.models import NVRDevice
from app.services.user_service import UserServiceError, change_own_password
from app.services.nvr_service import (
    check_nvr_now,
    create_nvr,
    delete_nvr,
    update_nvr,
)
from app.services.query_service import (
    get_distinct_areas_stmt,
    get_nvr_detail,
    get_overview,
    list_alerts,
    list_nvrs,
    list_offline_cameras,
)
from app.services.excel_export import build_offline_cameras_xlsx, build_report_xlsx
from app.services.report_service import build_uptime_report
from app.services.system_service import get_storage_usage

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).parent / "templates"


def _csrf_context(request: Request) -> dict:
    """Phơi token CSRF (do middleware đặt ở request.state) cho mọi template."""
    return {"csrf_token": getattr(request.state, "csrf_token", "")}


templates = Jinja2Templates(
    directory=str(_TEMPLATES_DIR), context_processors=[_csrf_context]
)

# Map trạng thái -> class màu Bootstrap (badge).
_BADGE = {
    "Online": "success",
    "Offline": "danger",
    "Warning": "warning",
    "Auth Error": "danger",
    "Network Error": "danger",
    "Disabled": "secondary",
    "No Signal": "warning",
    "Auth Failed": "danger",
    "Unknown": "secondary",
}

# Map severity alert -> class màu Bootstrap.
_SEVERITY_BADGE = {"info": "info", "warning": "warning", "critical": "danger"}


def _badge(status: str | None) -> str:
    return _BADGE.get(status or "", "secondary")


def _severity_badge(severity: str | None) -> str:
    return _SEVERITY_BADGE.get(severity or "", "secondary")


def _localtime(dt) -> str:
    if dt is None:
        return "—"
    tz = ZoneInfo(get_settings().timezone)
    return dt.astimezone(tz).strftime("%d/%m/%Y %H:%M:%S")


templates.env.filters["badge"] = _badge
templates.env.filters["severity_badge"] = _severity_badge
templates.env.filters["localtime"] = _localtime


# --- Đăng nhập / Đăng xuất ---

@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, next: str = "/", error: str | None = None):
    # Đã đăng nhập rồi thì về trang chủ.
    if getattr(request.state, "username", None):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request, "login.html", {"next": next, "error": error}
    )


@router.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
    session: AsyncSession = Depends(get_session),
):
    user = await authenticate(session, username, password)
    if user is None:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"next": next, "error": "Sai tài khoản hoặc mật khẩu."},
            status_code=401,
        )
    # Chỉ cho phép redirect nội bộ (chống open-redirect).
    target = next if next.startswith("/") else "/"
    response = RedirectResponse(target, status_code=303)
    token = create_session_token(user.username, user.role)
    settings = get_settings()
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        max_age=settings.session_ttl_hours * 3600,
    )
    return response


@router.post("/logout")
async def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


# --- Tài khoản cá nhân (mọi user đã đăng nhập tự đổi mật khẩu) ---

@router.get("/account", response_class=HTMLResponse)
async def account(request: Request, user=Depends(require_login)):
    return templates.TemplateResponse(
        request, "account.html", {"user": user, "error": None, "ok": None}
    )


@router.post("/account/passwd")
async def account_change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    user=Depends(require_login),
    session: AsyncSession = Depends(get_session),
):
    error = ok = None
    try:
        await change_own_password(
            session, user.username, current_password, new_password
        )
        ok = "Đã đổi mật khẩu thành công."
    except UserServiceError as e:
        error = str(e)
    return templates.TemplateResponse(
        request,
        "account.html",
        {"user": user, "error": error, "ok": ok},
        status_code=200 if ok else 400,
    )


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, session: AsyncSession = Depends(get_session)):
    overview = await get_overview(session)
    offline_cameras = await list_offline_cameras(session)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"overview": overview, "offline_cameras": offline_cameras},
    )


@router.get("/export/offline-cameras")
async def export_offline_cameras(session: AsyncSession = Depends(get_session)):
    """Xuất Excel danh sách camera đang mất tín hiệu (bảng trên trang Tổng quan)."""
    rows = await list_offline_cameras(session)
    content = build_offline_cameras_xlsx(rows)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    filename = f"camera_mat_tin_hieu_{stamp}.xlsx"
    return Response(
        content=content,
        media_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/nvrs", response_class=HTMLResponse)
async def nvr_list(
    request: Request,
    area: str | None = None,
    status: str | None = None,
    session: AsyncSession = Depends(get_session),
):
    rows = await list_nvrs(session, area=area, status=status)
    areas = [a for (a,) in (await session.execute(get_distinct_areas_stmt())).all()]
    ctx = {"rows": rows, "areas": areas, "sel_area": area, "sel_status": status}
    # HTMX request -> chỉ trả phần bảng (partial) để swap.
    template = "partials/nvr_table.html" if request.headers.get("HX-Request") else "nvr_list.html"
    return templates.TemplateResponse(request, template, ctx)


# --- CRUD NVR (đặt /nvrs/new TRƯỚC /nvrs/{nvr_id} để không bị nuốt route) ---

def _form_to_dict(
    name, host, username, password, port, use_https, location, area, model,
    channels, note, enabled, tls_fingerprint=""
) -> dict:
    return {
        "name": name,
        "host": host,
        "username": username,
        "password": password,
        "http_port": port,
        "use_https": use_https,
        "location": location,
        "area": area,
        "model": model,
        "channel_count": channels,
        "note": note,
        "enabled": enabled,
        "tls_fingerprint": tls_fingerprint,
    }


@router.get(
    "/nvrs/new", response_class=HTMLResponse, dependencies=[Depends(require_admin)]
)
async def nvr_new_form(request: Request):
    return templates.TemplateResponse(
        request, "nvr_form.html", {"nvr": None, "action": "/nvrs/new"}
    )


@router.post("/nvrs/new", dependencies=[Depends(require_admin)])
async def nvr_create(
    name: str = Form(...),
    host: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
    port: int = Form(80),
    use_https: bool = Form(False),
    location: str = Form(""),
    area: str = Form(""),
    model: str = Form(""),
    channels: int | None = Form(None),
    note: str = Form(""),
    enabled: bool = Form(False),
    tls_fingerprint: str = Form(""),
    session: AsyncSession = Depends(get_session),
):
    data = _form_to_dict(
        name, host, username, password, port, use_https, location, area, model,
        channels, note, enabled, tls_fingerprint
    )
    await create_nvr(session, data)
    return RedirectResponse("/nvrs", status_code=303)


@router.get(
    "/nvrs/{nvr_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_admin)],
)
async def nvr_edit_form(
    request: Request, nvr_id: int, session: AsyncSession = Depends(get_session)
):
    nvr = await session.get(NVRDevice, nvr_id)
    if nvr is None:
        return HTMLResponse("NVR không tồn tại", status_code=404)
    return templates.TemplateResponse(
        request, "nvr_form.html", {"nvr": nvr, "action": f"/nvrs/{nvr_id}/edit"}
    )


@router.post("/nvrs/{nvr_id}/edit", dependencies=[Depends(require_admin)])
async def nvr_update(
    nvr_id: int,
    name: str = Form(...),
    host: str = Form(...),
    username: str = Form(...),
    password: str = Form(""),
    port: int = Form(80),
    use_https: bool = Form(False),
    location: str = Form(""),
    area: str = Form(""),
    model: str = Form(""),
    channels: int | None = Form(None),
    note: str = Form(""),
    enabled: bool = Form(False),
    tls_fingerprint: str = Form(""),
    session: AsyncSession = Depends(get_session),
):
    data = _form_to_dict(
        name, host, username, password, port, use_https, location, area, model,
        channels, note, enabled, tls_fingerprint
    )
    await update_nvr(session, nvr_id, data)
    return RedirectResponse(f"/nvrs/{nvr_id}", status_code=303)


@router.post("/nvrs/{nvr_id}/delete", dependencies=[Depends(require_admin)])
async def nvr_delete(nvr_id: int, session: AsyncSession = Depends(get_session)):
    await delete_nvr(session, nvr_id)
    return RedirectResponse("/nvrs", status_code=303)


@router.post("/nvrs/{nvr_id}/check", dependencies=[Depends(require_admin)])
async def nvr_check(nvr_id: int, session: AsyncSession = Depends(get_session)):
    await check_nvr_now(session, nvr_id)
    return RedirectResponse(f"/nvrs/{nvr_id}", status_code=303)


@router.get("/nvrs/{nvr_id}", response_class=HTMLResponse)
async def nvr_detail(
    request: Request,
    nvr_id: int,
    session: AsyncSession = Depends(get_session),
):
    detail = await get_nvr_detail(session, nvr_id)
    if detail is None:
        return HTMLResponse("NVR không tồn tại", status_code=404)
    return templates.TemplateResponse(request, "nvr_detail.html", detail)


def _valid_days(days: int) -> int:
    # Giới hạn khoảng thời gian hợp lệ để tránh truy vấn quá nặng.
    return days if days in (1, 7, 30, 90) else 7


@router.get("/reports", response_class=HTMLResponse)
async def reports(
    request: Request,
    days: int = 7,
    area: str | None = None,
    session: AsyncSession = Depends(get_session),
):
    days = _valid_days(days)
    area = area or None
    report = await build_uptime_report(session, days, area=area)
    areas = [a for (a,) in (await session.execute(get_distinct_areas_stmt())).all()]
    return templates.TemplateResponse(
        request,
        "reports.html",
        {"report": report, "days": days, "areas": areas, "sel_area": area},
    )


@router.get("/reports/export")
async def reports_export(
    days: int = 7,
    area: str | None = None,
    session: AsyncSession = Depends(get_session),
):
    """Xuất báo cáo uptime ra Excel (.xlsx) theo bộ lọc hiện tại."""
    days = _valid_days(days)
    area = area or None
    report = await build_uptime_report(session, days, area=area, worst_limit=1000)
    content = build_report_xlsx(report, area=area)
    suffix = f"_{area}" if area else ""
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    filename = f"bao_cao_uptime_{days}ngay{suffix}_{stamp}.xlsx"
    return Response(
        content=content,
        media_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/alerts", response_class=HTMLResponse)
async def alerts(
    request: Request,
    show_all: bool = False,
    session: AsyncSession = Depends(get_session),
):
    rows = await list_alerts(session, only_open=not show_all)
    ctx = {"rows": rows, "show_all": show_all}
    # HTMX polling -> chỉ swap phần bảng.
    template = "partials/alert_table.html" if request.headers.get("HX-Request") else "alerts.html"
    return templates.TemplateResponse(request, template, ctx)


@router.get("/alerts/storage", response_class=HTMLResponse)
async def alerts_storage(
    request: Request, session: AsyncSession = Depends(get_session)
):
    """Panel dung lượng DB/disk (tự refresh độc lập với bảng cảnh báo)."""
    storage = await get_storage_usage(session)
    return templates.TemplateResponse(
        request, "partials/storage_panel.html", {"storage": storage}
    )
