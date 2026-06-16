"""Giám sát dung lượng lưu trữ: kích thước database so với disk của host.

Dùng cho panel trên trang Cảnh báo để kiểm soát, tránh để disk đầy (Postgres sẽ
dừng ghi khi hết chỗ). DB chạy trong container `db` với volume `pgdata` nằm trên
cùng phân vùng disk của host (Docker data-root), nên `shutil.disk_usage("/")` gọi
từ container `app` phản ánh đúng disk thật mà DB đang dùng.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings


def _human(n: int | float) -> str:
    """Định dạng số byte sang chuỗi dễ đọc (B/KB/MB/GB/TB)."""
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


@dataclass
class StorageUsage:
    db_size: int
    disk_total: int
    disk_used: int
    disk_free: int
    db_pct_of_disk: float
    disk_used_pct: float
    level: str  # ok | warning | danger (theo ngưỡng disk_used_pct)

    @property
    def db_size_h(self) -> str:
        return _human(self.db_size)

    @property
    def disk_total_h(self) -> str:
        return _human(self.disk_total)

    @property
    def disk_used_h(self) -> str:
        return _human(self.disk_used)

    @property
    def disk_free_h(self) -> str:
        return _human(self.disk_free)


async def get_storage_usage(session: AsyncSession) -> StorageUsage:
    """Lấy dung lượng DB + disk và tính % để hiển thị/giám sát."""
    settings = get_settings()
    db_size = int(
        await session.scalar(text("SELECT pg_database_size(current_database())")) or 0
    )
    total, used, free = shutil.disk_usage("/")

    db_pct = round(db_size / total * 100, 1) if total else 0.0
    used_pct = round(used / total * 100, 1) if total else 0.0

    if used_pct >= settings.disk_crit_pct:
        level = "danger"
    elif used_pct >= settings.disk_warn_pct:
        level = "warning"
    else:
        level = "ok"

    return StorageUsage(
        db_size=db_size,
        disk_total=total,
        disk_used=used,
        disk_free=free,
        db_pct_of_disk=db_pct,
        disk_used_pct=used_pct,
        level=level,
    )
