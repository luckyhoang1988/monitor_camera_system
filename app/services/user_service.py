"""Thao tác ghi cho người dùng đăng nhập: tạo / đổi role / reset & đổi mật khẩu / xóa.

Tách logic khỏi route (giống `nvr_service.py`), tái dùng băm/kiểm mật khẩu ở
`app/auth.py`. Có guard chống tự khóa: không xóa chính mình, không xóa/hạ-cấp admin
cuối cùng (nếu không sẽ không còn ai quản trị được hệ thống).
"""

from __future__ import annotations

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import hash_password, verify_password
from app.db.models import User
from app.enums import UserRole


class UserServiceError(ValueError):
    """Lỗi nghiệp vụ có thông điệp thân thiện để hiển thị cho người dùng."""


def _validate_role(role: str) -> str:
    valid = {r.value for r in UserRole}
    if role not in valid:
        raise UserServiceError(f"Vai trò không hợp lệ: {role!r} (chỉ {sorted(valid)}).")
    return role


async def _count_admins(session: AsyncSession) -> int:
    return int(
        await session.scalar(
            select(func.count()).select_from(User).where(User.role == UserRole.ADMIN.value)
        )
        or 0
    )


async def list_users(session: AsyncSession) -> list[User]:
    return list((await session.scalars(select(User).order_by(User.id))).all())


async def create_user(
    session: AsyncSession, *, username: str, password: str, role: str
) -> User:
    username = username.strip()
    if not username:
        raise UserServiceError("Tên đăng nhập không được để trống.")
    if not password:
        raise UserServiceError("Mật khẩu không được để trống.")
    _validate_role(role)
    exists = (
        await session.scalars(select(User).where(User.username == username))
    ).first()
    if exists is not None:
        raise UserServiceError(f"Tài khoản '{username}' đã tồn tại.")
    user = User(username=username, password_hash=hash_password(password), role=role)
    session.add(user)
    await session.commit()
    return user


async def set_role(session: AsyncSession, user_id: int, role: str) -> User:
    _validate_role(role)
    user = await session.get(User, user_id)
    if user is None:
        raise UserServiceError("Không tìm thấy người dùng.")
    # Không cho hạ cấp admin cuối cùng.
    if (
        user.role == UserRole.ADMIN.value
        and role != UserRole.ADMIN.value
        and await _count_admins(session) <= 1
    ):
        raise UserServiceError("Không thể hạ quyền admin cuối cùng.")
    user.role = role
    await session.commit()
    return user


async def reset_password(session: AsyncSession, user_id: int, new_password: str) -> User:
    if not new_password:
        raise UserServiceError("Mật khẩu mới không được để trống.")
    user = await session.get(User, user_id)
    if user is None:
        raise UserServiceError("Không tìm thấy người dùng.")
    user.password_hash = hash_password(new_password)
    await session.commit()
    return user


async def delete_user(
    session: AsyncSession, user_id: int, *, acting_username: str
) -> None:
    user = await session.get(User, user_id)
    if user is None:
        raise UserServiceError("Không tìm thấy người dùng.")
    if user.username == acting_username:
        raise UserServiceError("Không thể tự xóa tài khoản đang đăng nhập.")
    if user.role == UserRole.ADMIN.value and await _count_admins(session) <= 1:
        raise UserServiceError("Không thể xóa admin cuối cùng.")
    await session.execute(sa_delete(User).where(User.id == user_id))
    await session.commit()


async def change_own_password(
    session: AsyncSession, username: str, current_password: str, new_password: str
) -> None:
    """Tự đổi mật khẩu: phải đúng mật khẩu hiện tại."""
    if not new_password:
        raise UserServiceError("Mật khẩu mới không được để trống.")
    user = (
        await session.scalars(select(User).where(User.username == username))
    ).first()
    if user is None:
        raise UserServiceError("Không tìm thấy người dùng.")
    if not verify_password(current_password, user.password_hash):
        raise UserServiceError("Mật khẩu hiện tại không đúng.")
    user.password_hash = hash_password(new_password)
    await session.commit()
