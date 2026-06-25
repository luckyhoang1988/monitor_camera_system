"""NVR offline phải bị trừ vào uptime camera trong báo cáo.

Khi NVR rớt, job camera ghi log Unknown cho từng kênh (log_cameras_unreachable) thay
vì bỏ trống. Nhờ đó:
- system_camera_uptime bị trừ đúng thời gian NVR chết (Unknown != Online).
- Unknown->Online KHÔNG bị tính là "hồi phục camera" (tránh ngập danh sách recovery).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import CameraChannel, CameraStatusLog, NVRDevice
from app.enums import CameraStatus, NVRStatus
from app.services.report_service import build_uptime_report
from app.services.status_service import log_cameras_unreachable


async def _make_session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def test_log_cameras_unreachable_writes_unknown_for_each_camera():
    async def run():
        engine, Session = await _make_session()
        async with Session() as session:
            nvr = NVRDevice(
                name="NVR-A",
                host="10.0.0.10",
                username="admin",
                password_enc="enc",
                current_status=NVRStatus.OFFLINE.value,
            )
            session.add(nvr)
            await session.flush()
            session.add_all(
                [
                    CameraChannel(nvr_id=nvr.id, channel_no=1),
                    CameraChannel(nvr_id=nvr.id, channel_no=2),
                ]
            )
            await session.flush()

            await log_cameras_unreachable(session, nvr)
            await session.commit()

            logs = (
                await session.execute(select(CameraStatusLog))
            ).scalars().all()
            assert len(logs) == 2
            assert all(l.status == CameraStatus.UNKNOWN.value for l in logs)
        await engine.dispose()

    asyncio.run(run())


def test_nvr_downtime_lowers_camera_uptime():
    async def run():
        engine, Session = await _make_session()
        async with Session() as session:
            now = datetime.now(timezone.utc)
            start = now - timedelta(hours=1)

            nvr = NVRDevice(
                name="NVR-A",
                host="10.0.0.10",
                username="admin",
                password_enc="enc",
                current_status=NVRStatus.OFFLINE.value,
            )
            session.add(nvr)
            await session.flush()
            cam = CameraChannel(nvr_id=nvr.id, channel_no=1)
            session.add(cam)
            await session.flush()

            # 1 lần đo Online + 1 lần Unknown (NVR rớt) -> uptime 50%, không phải 100%.
            session.add_all(
                [
                    CameraStatusLog(
                        camera_id=cam.id,
                        status=CameraStatus.ONLINE.value,
                        checked_at=start + timedelta(minutes=5),
                    ),
                    CameraStatusLog(
                        camera_id=cam.id,
                        status=CameraStatus.UNKNOWN.value,
                        checked_at=start + timedelta(minutes=15),
                    ),
                ]
            )
            await session.commit()

            report = await build_uptime_report(session, days=1, start=start, end=now)
            assert report.system_camera_uptime == 50.0

        await engine.dispose()

    asyncio.run(run())


def test_unknown_to_online_is_not_a_camera_recovery():
    async def run():
        engine, Session = await _make_session()
        async with Session() as session:
            now = datetime.now(timezone.utc)
            start = now - timedelta(hours=1)

            nvr = NVRDevice(
                name="NVR-A",
                host="10.0.0.10",
                username="admin",
                password_enc="enc",
                current_status=NVRStatus.ONLINE.value,
            )
            session.add(nvr)
            await session.flush()
            cam = CameraChannel(nvr_id=nvr.id, channel_no=1)
            session.add(cam)
            await session.flush()

            # Unknown (NVR vừa rớt) -> Online (NVR sống lại): KHÔNG phải hồi phục camera.
            session.add_all(
                [
                    CameraStatusLog(
                        camera_id=cam.id,
                        status=CameraStatus.UNKNOWN.value,
                        checked_at=start + timedelta(minutes=5),
                    ),
                    CameraStatusLog(
                        camera_id=cam.id,
                        status=CameraStatus.ONLINE.value,
                        checked_at=start + timedelta(minutes=10),
                    ),
                ]
            )
            await session.commit()

            report = await build_uptime_report(session, days=1, start=start, end=now)
            assert report.camera_recoveries == []
        await engine.dispose()

    asyncio.run(run())
