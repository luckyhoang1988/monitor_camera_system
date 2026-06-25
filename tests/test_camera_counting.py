"""Đếm camera ở dashboard theo trạng thái NVR (phương án B).

Quy tắc thống nhất: camera coi là "mất" khi tự nó Offline/No Signal HOẶC NVR cha ở
trạng thái CHỐT chết (Offline/Network/Auth). NVR `Warning` (chập chờn) thì camera giữ
last-known — KHÔNG bị tính offline (chống flapping, đồng bộ tầng cảnh báo).
"""

from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import CameraChannel, NVRDevice
from app.enums import CameraStatus, NVRStatus
from app.services.query_service import get_overview, list_offline_cameras


async def _make_session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _nvr_with_cameras(session, name, host, status):
    nvr = NVRDevice(
        name=name,
        host=host,
        username="admin",
        password_enc="enc",
        current_status=status.value,
    )
    session.add(nvr)
    await session.flush()
    # Hai camera đều đang lưu trạng thái Online (last-known).
    session.add_all(
        [
            CameraChannel(
                nvr_id=nvr.id, channel_no=1, current_status=CameraStatus.ONLINE.value
            ),
            CameraChannel(
                nvr_id=nvr.id, channel_no=2, current_status=CameraStatus.ONLINE.value
            ),
        ]
    )
    await session.flush()
    return nvr


def test_warning_lenient_offline_penalized_in_overview():
    async def run():
        engine, Session = await _make_session()
        async with Session() as session:
            await _nvr_with_cameras(session, "ON", "10.0.0.1", NVRStatus.ONLINE)
            await _nvr_with_cameras(session, "WARN", "10.0.0.2", NVRStatus.WARNING)
            await _nvr_with_cameras(session, "OFF", "10.0.0.3", NVRStatus.OFFLINE)
            await session.commit()

            ov = await get_overview(session)
            assert ov.camera_total == 6
            # Online (2) + Warning giữ last-known Online (2) = 4 online; chỉ Offline bị trừ.
            assert ov.camera_online == 4
            assert ov.camera_offline == 2
        await engine.dispose()

    asyncio.run(run())


def test_offline_list_excludes_warning_includes_down():
    async def run():
        engine, Session = await _make_session()
        async with Session() as session:
            await _nvr_with_cameras(session, "WARN", "10.0.0.2", NVRStatus.WARNING)
            await _nvr_with_cameras(session, "AUTH", "10.0.0.4", NVRStatus.AUTH_ERROR)
            await session.commit()

            rows = await list_offline_cameras(session)
            # Chỉ 2 camera của NVR Auth Error (down); Warning không xuất hiện.
            assert len(rows) == 2
            assert all(r["nvr_status"] == NVRStatus.AUTH_ERROR.value for r in rows)
            # Vào danh sách do NVR down (camera tự nó vẫn Online) -> stale.
            assert all(r["stale"] for r in rows)
        await engine.dispose()

    asyncio.run(run())
