"""Routes quản lý người dùng — CHỈ admin (gắn require_admin ở cấp router)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_admin
from app.db.base import get_session
from app.enums import UserRole
from app.services.user_service import (
    UserServiceError,
    create_user,
    delete_user,
    list_users,
    reset_password,
    set_role,
)
from app.web.views import templates

router = APIRouter(dependencies=[Depends(require_admin)])

_ROLES = [r.value for r in UserRole]


async def _render_list(
    request: Request, session: AsyncSession, *, error: str | None = None, status_code: int = 200
):
    users = await list_users(session)
    return templates.TemplateResponse(
        request,
        "users.html",
        {"users": users, "roles": _ROLES, "error": error},
        status_code=status_code,
    )


@router.get("/users", response_class=HTMLResponse)
async def users_list(request: Request, session: AsyncSession = Depends(get_session)):
    return await _render_list(request, session)


@router.get("/users/new", response_class=HTMLResponse)
async def user_new_form(request: Request):
    return templates.TemplateResponse(
        request, "user_form.html", {"roles": _ROLES, "error": None}
    )


@router.post("/users/new")
async def user_create(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    role: str = Form(UserRole.VIEWER.value),
    session: AsyncSession = Depends(get_session),
):
    try:
        await create_user(session, username=username, password=password, role=role)
    except UserServiceError as e:
        return templates.TemplateResponse(
            request,
            "user_form.html",
            {"roles": _ROLES, "error": str(e)},
            status_code=400,
        )
    return RedirectResponse("/users", status_code=303)


@router.post("/users/{user_id}/role")
async def user_set_role(
    request: Request,
    user_id: int,
    role: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    try:
        await set_role(session, user_id, role)
    except UserServiceError as e:
        return await _render_list(request, session, error=str(e), status_code=400)
    return RedirectResponse("/users", status_code=303)


@router.post("/users/{user_id}/passwd")
async def user_reset_password(
    request: Request,
    user_id: int,
    password: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    try:
        await reset_password(session, user_id, password)
    except UserServiceError as e:
        return await _render_list(request, session, error=str(e), status_code=400)
    return RedirectResponse("/users", status_code=303)


@router.post("/users/{user_id}/delete")
async def user_delete(
    request: Request,
    user_id: int,
    session: AsyncSession = Depends(get_session),
):
    try:
        await delete_user(
            session, user_id, acting_username=request.state.username
        )
    except UserServiceError as e:
        return await _render_list(request, session, error=str(e), status_code=400)
    return RedirectResponse("/users", status_code=303)
