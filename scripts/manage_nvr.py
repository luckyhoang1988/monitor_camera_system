"""CLI quản lý NVR cho Chek_NVR.

Mật khẩu luôn được mã hóa (Fernet) trước khi lưu DB. Ví dụ:

  python -m scripts.manage_nvr add --name "NVR Khu A" --host 192.168.1.10 \
      --username admin --password "matkhau" --port 80 --location "Tang 1" --area "Khu A"

  python -m scripts.manage_nvr list
  python -m scripts.manage_nvr test --id 1        # gọi ISAPI thử 1 NVR
  python -m scripts.manage_nvr scan               # quét toàn bộ 1 lần
  python -m scripts.manage_nvr delete --id 1
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

# Windows console mặc định cp1252 -> ép UTF-8 để in được tiếng Việt.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

# Tắt echo SQL cho CLI (engine bật echo khi DEBUG=true).
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

from sqlalchemy import delete, select

from app.collector.checker import check_nvr
from app.config import get_settings
from app.db.base import AsyncSessionLocal
from app.db.models import NVRDevice
from app.security import decrypt_password, encrypt_password


async def cmd_add(args: argparse.Namespace) -> None:
    async with AsyncSessionLocal() as session:
        nvr = NVRDevice(
            name=args.name,
            host=args.host,
            http_port=args.port,
            use_https=args.https,
            username=args.username,
            password_enc=encrypt_password(args.password),
            location=args.location,
            area=args.area,
            model=args.model,
            channel_count=args.channels,
            note=args.note,
        )
        session.add(nvr)
        await session.commit()
        print(f"Đã thêm NVR #{nvr.id}: {nvr.name} ({nvr.host}:{nvr.http_port})")


async def cmd_list(_: argparse.Namespace) -> None:
    async with AsyncSessionLocal() as session:
        nvrs = (await session.scalars(select(NVRDevice).order_by(NVRDevice.id))).all()
    if not nvrs:
        print("Chưa có NVR nào.")
        return
    print(f"{'ID':<4}{'Tên':<22}{'Host:Port':<24}{'Khu vực':<14}{'Trạng thái':<14}{'Bật'}")
    print("-" * 92)
    for n in nvrs:
        print(
            f"{n.id:<4}{(n.name or '')[:21]:<22}"
            f"{(n.host + ':' + str(n.http_port))[:23]:<24}"
            f"{(n.area or '—')[:13]:<14}{n.current_status:<14}{'✓' if n.enabled else '✗'}"
        )


async def cmd_delete(args: argparse.Namespace) -> None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            delete(NVRDevice).where(NVRDevice.id == args.id)
        )
        await session.commit()
        if result.rowcount:
            print(f"Đã xóa NVR #{args.id}.")
        else:
            print(f"Không tìm thấy NVR #{args.id}.")


async def cmd_test(args: argparse.Namespace) -> None:
    """Gọi ISAPI thử một NVR (không ghi DB) để kiểm tra kết nối/credential."""
    settings = get_settings()
    async with AsyncSessionLocal() as session:
        nvr = await session.get(NVRDevice, args.id)
    if nvr is None:
        print(f"Không tìm thấy NVR #{args.id}.")
        return

    print(f"Đang kiểm tra '{nvr.name}' ({nvr.host}:{nvr.http_port}) ...")
    result = await check_nvr(
        host=nvr.host,
        username=nvr.username,
        password=decrypt_password(nvr.password_enc),
        port=nvr.http_port,
        use_https=nvr.use_https,
        timeout=settings.request_timeout,
        tls_fingerprint=nvr.tls_fingerprint,
        retries=settings.request_retries,
        retry_backoff_base=settings.retry_backoff_base,
    )
    print(f"  Trạng thái thô : {result.raw_status.value}")
    print(f"  Ping / Port    : {result.ping_ok} / {result.port_ok}")
    print(f"  Phản hồi (ms)  : {result.response_time_ms}")
    if result.device:
        print(
            f"  Thiết bị       : {result.device.model} | SN {result.device.serial} "
            f"| FW {result.device.firmware}"
        )
    print(f"  Số camera      : {len(result.channels)}")
    for ch in result.channels[:10]:
        print(f"    - Kênh {ch.channel_no}: {ch.name} | online={ch.online}")
    if result.error:
        print(f"  Lỗi            : {result.error}")


async def cmd_scan(_: argparse.Namespace) -> None:
    """Chạy một lượt quét toàn bộ NVR ngay lập tức (health + camera)."""
    from app.collector.scheduler import scan_cameras, scan_nvr_health

    await scan_nvr_health()
    await scan_cameras()
    print("Đã quét xong.")


async def cmd_purge(args: argparse.Namespace) -> None:
    """Dọn log cũ ngay (retention thủ công)."""
    from app.services.retention_service import purge_old_logs

    days = args.days if args.days is not None else get_settings().log_retention_days
    async with AsyncSessionLocal() as session:
        result = await purge_old_logs(session, retention_days=days)
        await session.commit()
    print(
        f"Đã xóa {result.nvr_logs} log NVR, {result.camera_logs} log camera, "
        f"{result.resolved_alerts} alert đã đóng (giữ {days} ngày)."
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Quản lý NVR cho Chek_NVR")
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="Thêm NVR mới")
    p_add.add_argument("--name", required=True)
    p_add.add_argument("--host", required=True, help="IP hoặc domain")
    p_add.add_argument("--username", required=True)
    p_add.add_argument("--password", required=True)
    p_add.add_argument("--port", type=int, default=80)
    p_add.add_argument("--https", action="store_true")
    p_add.add_argument("--location", default=None)
    p_add.add_argument("--area", default=None)
    p_add.add_argument("--model", default=None)
    p_add.add_argument("--channels", type=int, default=None)
    p_add.add_argument("--note", default=None)
    p_add.set_defaults(func=cmd_add)

    sub.add_parser("list", help="Liệt kê NVR").set_defaults(func=cmd_list)

    p_del = sub.add_parser("delete", help="Xóa NVR theo id")
    p_del.add_argument("--id", type=int, required=True)
    p_del.set_defaults(func=cmd_delete)

    p_test = sub.add_parser("test", help="Gọi ISAPI thử 1 NVR (không ghi DB)")
    p_test.add_argument("--id", type=int, required=True)
    p_test.set_defaults(func=cmd_test)

    sub.add_parser("scan", help="Quét toàn bộ NVR 1 lần").set_defaults(func=cmd_scan)

    p_purge = sub.add_parser("purge", help="Dọn log cũ (retention) ngay")
    p_purge.add_argument(
        "--days", type=int, default=None, help="Số ngày giữ lại (mặc định lấy từ config)"
    )
    p_purge.set_defaults(func=cmd_purge)

    return parser


async def _run(args: argparse.Namespace) -> None:
    """Chạy lệnh rồi đóng pool HTTP dùng chung (CLI không có lifespan)."""
    from app.collector.http_pool import close_all

    try:
        await args.func(args)
    finally:
        await close_all()


def main() -> None:
    args = build_parser().parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
