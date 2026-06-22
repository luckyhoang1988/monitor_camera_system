"""Routes render dashboard bằng Jinja2 + HTMX."""

from __future__ import annotations

from datetime import datetime, time, timezone
from math import ceil
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
from app.services.retention_service import purge_logs_in_range

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
    ctx = {"overview": overview, "offline_cameras": offline_cameras}
    # HTMX polling -> chỉ swap phần thân (cards + bảng camera mất tín hiệu).
    template = (
        "partials/dashboard_body.html"
        if request.headers.get("HX-Request")
        else "dashboard.html"
    )
    return templates.TemplateResponse(request, template, ctx)


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


def _parse_report_range(
    from_date: str | None, to_date: str | None
) -> tuple[datetime | None, datetime | None]:
    """Đổi từ ngày/tới ngày (giờ local) sang khoảng (start, end) UTC.

    Bao trùm trọn ngày: đầu ngày `from_date` → cuối ngày `to_date`. Chỉ trả về
    khoảng khi cả hai ngày hợp lệ và start <= end; ngược lại trả (None, None) để
    rơi về cửa sổ tương đối `days`.
    """
    if not from_date or not to_date:
        return None, None
    tz = ZoneInfo(get_settings().timezone)
    try:
        start_d = datetime.strptime(from_date, "%Y-%m-%d").date()
        end_d = datetime.strptime(to_date, "%Y-%m-%d").date()
    except ValueError:
        return None, None
    if start_d > end_d:
        return None, None
    start = datetime.combine(start_d, time.min, tzinfo=tz).astimezone(timezone.utc)
    end = datetime.combine(end_d, time.max, tzinfo=tz).astimezone(timezone.utc)
    return start, end


@router.get("/reports", response_class=HTMLResponse)
async def reports(
    request: Request,
    days: int = 7,
    area: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    cam_from_date: str | None = None,
    cam_to_date: str | None = None,
    cam_page: int = 1,
    session: AsyncSession = Depends(get_session),
):
    days = _valid_days(days)
    area = area or None
    start, end = _parse_report_range(from_date, to_date)
    cam_start, cam_end = _parse_report_range(cam_from_date, cam_to_date)
    report = await build_uptime_report(
        session,
        days,
        area=area,
        start=start,
        end=end,
        camera_recovery_start=cam_start,
        camera_recovery_end=cam_end,
    )
    # Phân trang riêng cho bảng "Lịch sử camera online trở lại".
    cam_page_size = 20
    cam_total_rows = len(report.camera_recoveries)
    cam_total_pages = max(1, ceil(cam_total_rows / cam_page_size))
    cam_page = min(max(cam_page, 1), cam_total_pages)
    cam_offset = (cam_page - 1) * cam_page_size
    report.camera_recoveries = report.camera_recoveries[
        cam_offset: cam_offset + cam_page_size
    ]

    areas = [a for (a,) in (await session.execute(get_distinct_areas_stmt())).all()]
    return templates.TemplateResponse(
        request,
        "reports.html",
        {
            "report": report,
            "days": days,
            "areas": areas,
            "sel_area": area,
            "from_date": from_date if start else "",
            "to_date": to_date if end else "",
            "cam_from_date": cam_from_date if cam_start else "",
            "cam_to_date": cam_to_date if cam_end else "",
            "cam_page": cam_page,
            "cam_total_pages": cam_total_pages,
            "cam_total_rows": cam_total_rows,
            "cam_page_size": cam_page_size,
        },
    )


@router.get("/reports/export")
async def reports_export(
    days: int = 7,
    area: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    cam_from_date: str | None = None,
    cam_to_date: str | None = None,
    session: AsyncSession = Depends(get_session),
):
    """Xuất báo cáo uptime ra Excel (.xlsx) theo bộ lọc hiện tại."""
    days = _valid_days(days)
    area = area or None
    start, end = _parse_report_range(from_date, to_date)
    cam_start, cam_end = _parse_report_range(cam_from_date, cam_to_date)
    report = await build_uptime_report(
        session,
        days,
        area=area,
        worst_limit=1000,
        start=start,
        end=end,
        camera_recovery_start=cam_start,
        camera_recovery_end=cam_end,
    )
    content = build_report_xlsx(report, area=area)
    suffix = f"_{area}" if area else ""
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    if start and end:
        period = f"{from_date}_den_{to_date}"
    else:
        period = f"{days}ngay"
    filename = f"bao_cao_uptime_{period}{suffix}_{stamp}.xlsx"
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


@router.post("/alerts/purge", dependencies=[Depends(require_admin)])
async def alerts_purge(
    request: Request,
    from_date: str = Form(...),
    to_date: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    """Xóa thủ công log trạng thái trong khoảng ngày (giờ local) — giải phóng disk.

    Người dùng chọn từ ngày tới ngày trên giao diện; diễn giải theo timezone local
    (đầu ngày from_date → cuối ngày to_date) rồi đổi sang UTC để khớp `checked_at`.
    """
    tz = ZoneInfo(get_settings().timezone)
    ctx: dict = {}
    try:
        start_d = datetime.strptime(from_date, "%Y-%m-%d").date()
        end_d = datetime.strptime(to_date, "%Y-%m-%d").date()
        if start_d > end_d:
            raise ValueError("Ngày bắt đầu phải trước hoặc bằng ngày kết thúc.")
        start = datetime.combine(start_d, time.min, tzinfo=tz).astimezone(timezone.utc)
        end = datetime.combine(end_d, time.max, tzinfo=tz).astimezone(timezone.utc)
        result = await purge_logs_in_range(session, start=start, end=end)
        await session.commit()
        ctx["result"] = result
        ctx["from_date"] = from_date
        ctx["to_date"] = to_date
    except ValueError as exc:
        ctx["error"] = str(exc) or "Ngày không hợp lệ."

    resp = templates.TemplateResponse(
        request, "partials/purge_result.html", ctx
    )
    # Báo cho panel dung lượng làm mới ngay sau khi xóa.
    resp.headers["HX-Trigger"] = "refreshStorage"
    return resp
