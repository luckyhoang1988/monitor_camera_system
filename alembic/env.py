"""Môi trường migration Alembic (async) cho Chek_NVR."""

import asyncio
from logging.config import fileConfig

from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import pool

from alembic import context

from app.config import get_settings
from app.db.base import Base

# Nạp toàn bộ models để Base.metadata biết mọi bảng.
from app.db import models  # noqa: F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Lấy DB URL trực tiếp từ app config (.env). KHÔNG dùng set_main_option vì
# configparser sẽ hiểu '%' trong password (URL-encode) là cú pháp interpolation.
DB_URL = get_settings().database_url

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = DB_URL
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = create_async_engine(DB_URL, poolclass=pool.NullPool)
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
