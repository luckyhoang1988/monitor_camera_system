"""CLI quản lý người dùng đăng nhập dashboard.

Mật khẩu luôn được băm (PBKDF2-HMAC-SHA256) trước khi lưu DB. Ví dụ:

  python -m scripts.manage_user add --username admin --password "matkhau" --role admin
  python -m scripts.manage_user list
  python -m scripts.manage_user passwd --username admin --password "matkhaumoi"
  python -m scripts.manage_user delete --username admin
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

from sqlalchemy import delete, select

from app.auth import hash_password
from app.db.base import AsyncSessionLocal
from app.db.models import User
from app.enums import UserRole

_ROLES = [r.value for r in UserRole]


async def cmd_add(args: argparse.Namespace) -> None:
    async with AsyncSessionLocal() as session:
        exists = (
            await session.scalars(select(User).where(User.username == args.username))
        ).first()
        if exists is not None:
            print(f"Tài khoản '{args.username}' đã tồn tại.")
            return
        user = User(
            username=args.username,
            password_hash=hash_password(args.password),
            role=args.role,
        )
        session.add(user)
        await session.commit()
        print(f"Đã tạo người dùng #{user.id}: {user.username} (vai trò: {user.role}).")


async def cmd_list(_: argparse.Namespace) -> None:
    async with AsyncSessionLocal() as session:
        users = (await session.scalars(select(User).order_by(User.id))).all()
    if not users:
        print("Chưa có người dùng nào. Tạo bằng: python -m scripts.manage_user add ...")
        return
    print(f"{'ID':<4}{'Tài khoản':<24}{'Vai trò':<12}")
    print("-" * 40)
    for u in users:
        print(f"{u.id:<4}{u.username:<24}{u.role:<12}")


async def cmd_passwd(args: argparse.Namespace) -> None:
    async with AsyncSessionLocal() as session:
        user = (
            await session.scalars(select(User).where(User.username == args.username))
        ).first()
        if user is None:
            print(f"Không tìm thấy tài khoản '{args.username}'.")
            return
        user.password_hash = hash_password(args.password)
        await session.commit()
        print(f"Đã đổi mật khẩu cho '{args.username}'.")


async def cmd_delete(args: argparse.Namespace) -> None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            delete(User).where(User.username == args.username)
        )
        await session.commit()
        if result.rowcount:
            print(f"Đã xóa tài khoản '{args.username}'.")
        else:
            print(f"Không tìm thấy tài khoản '{args.username}'.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Quản lý người dùng Chek_NVR")
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="Tạo người dùng mới")
    p_add.add_argument("--username", required=True)
    p_add.add_argument("--password", required=True)
    p_add.add_argument(
        "--role", default=UserRole.VIEWER.value, choices=_ROLES, help="viewer | admin"
    )
    p_add.set_defaults(func=cmd_add)

    sub.add_parser("list", help="Liệt kê người dùng").set_defaults(func=cmd_list)

    p_pw = sub.add_parser("passwd", help="Đổi mật khẩu")
    p_pw.add_argument("--username", required=True)
    p_pw.add_argument("--password", required=True)
    p_pw.set_defaults(func=cmd_passwd)

    p_del = sub.add_parser("delete", help="Xóa người dùng")
    p_del.add_argument("--username", required=True)
    p_del.set_defaults(func=cmd_delete)

    return parser


def main() -> None:
    args = build_parser().parse_args()
    asyncio.run(args.func(args))


if __name__ == "__main__":
    main()
