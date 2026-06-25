"""Tiện ích dùng chung cho test tích hợp DB (SQLite in-memory, không cần Postgres).

`make_session()` tạo engine aiosqlite + tạo toàn bộ bảng từ metadata, trả về
(engine, sessionmaker). Mẫu này vốn lặp ở nhiều file test — gom về đây để tái dùng.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.base import Base


async def make_session():
    """Engine SQLite in-memory + schema đầy đủ. Nhớ `await engine.dispose()` cuối test."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)
