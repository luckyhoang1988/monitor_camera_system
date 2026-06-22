"""Test report recovery events cho NVR/camera trong khoảng lọc."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import CameraChannel, CameraStatusLog, NVRDevice, NVRStatusLog
from app.enums import CameraStatus, NVRStatus
from app.services.report_service import build_uptime_report


async def _make_session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def test_recovery_events_detected_with_previous_status_before_range():
    async def run():
        engine, Session = await _make_session()
        async with Session() as session:
            now = datetime.now(timezone.utc)
            start = now - timedelta(hours=1)
            end = now

            nvr = NVRDevice(
                name="NVR-A",
                host="10.0.0.10",
                username="admin",
                password_enc="enc",
                area="Khu A",
                current_status=NVRStatus.ONLINE.value,
            )
            session.add(nvr)
            await session.flush()

            cam = CameraChannel(
                nvr_id=nvr.id,
                channel_no=1,
                name="Cam 1",
                current_status=CameraStatus.ONLINE.value,
            )
            session.add(cam)
            await session.flush()

            # Trạng thái trước khoảng lọc.
            session.add(
                NVRStatusLog(
                    nvr_id=nvr.id,
                    status=NVRStatus.OFFLINE.value,
                    checked_at=start - timedelta(minutes=10),
                )
            )
            session.add(
                CameraStatusLog(
                    camera_id=cam.id,
                    status=CameraStatus.OFFLINE.value,
                    checked_at=start - timedelta(minutes=8),
                )
            )
            # Trong khoảng lọc: hồi phục.
            session.add(
                NVRStatusLog(
                    nvr_id=nvr.id,
                    status=NVRStatus.ONLINE.value,
                    checked_at=start + timedelta(minutes=5),
                )
            )
            session.add(
                CameraStatusLog(
                    camera_id=cam.id,
                    status=CameraStatus.ONLINE.value,
                    checked_at=start + timedelta(minutes=6),
                )
            )
            await session.commit()

            report = await build_uptime_report(session, days=1, start=start, end=end)

            assert len(report.nvr_recoveries) == 1
            assert report.nvr_recoveries[0].name == "NVR-A"
            assert report.nvr_recoveries[0].from_status == NVRStatus.OFFLINE.value

            assert len(report.camera_recoveries) == 1
            assert report.camera_recoveries[0].name == "Cam 1"
            assert report.camera_recoveries[0].from_status == CameraStatus.OFFLINE.value
        await engine.dispose()

    asyncio.run(run())


def test_camera_recovery_collects_all_transitions_in_range():
    async def run():
        engine, Session = await _make_session()
        async with Session() as session:
            now = datetime.now(timezone.utc)
            start = now - timedelta(hours=1)
            end = now

            nvr = NVRDevice(
                name="NVR-B",
                host="10.0.0.11",
                username="admin",
                password_enc="enc",
                area="Khu B",
                current_status=NVRStatus.ONLINE.value,
            )
            session.add(nvr)
            await session.flush()

            cam = CameraChannel(
                nvr_id=nvr.id,
                channel_no=2,
                name="Cam 2",
                current_status=CameraStatus.ONLINE.value,
            )
            session.add(cam)
            await session.flush()

            session.add_all(
                [
                    CameraStatusLog(
                        camera_id=cam.id,
                        status=CameraStatus.OFFLINE.value,
                        checked_at=start + timedelta(minutes=1),
                    ),
                    CameraStatusLog(
                        camera_id=cam.id,
                        status=CameraStatus.ONLINE.value,
                        checked_at=start + timedelta(minutes=5),
                    ),
                    CameraStatusLog(
                        camera_id=cam.id,
                        status=CameraStatus.OFFLINE.value,
                        checked_at=start + timedelta(minutes=10),
                    ),
                    CameraStatusLog(
                        camera_id=cam.id,
                        status=CameraStatus.ONLINE.value,
                        checked_at=start + timedelta(minutes=15),
                    ),
                ]
            )
            await session.commit()

            report = await build_uptime_report(session, days=1, start=start, end=end)
            assert len(report.camera_recoveries) == 2
            # Sắp xếp giảm dần theo thời gian: lần hồi phục mới nhất đứng trước.
            assert report.camera_recoveries[0].recovered_at > report.camera_recoveries[1].recovered_at
        await engine.dispose()

    asyncio.run(run())


def test_recovery_events_respect_area_filter():
    async def run():
        engine, Session = await _make_session()
        async with Session() as session:
            now = datetime.now(timezone.utc)
            start = now - timedelta(hours=1)
            end = now

            nvr_a = NVRDevice(
                name="NVR-Area-A",
                host="10.0.0.20",
                username="admin",
                password_enc="enc",
                area="A",
                current_status=NVRStatus.ONLINE.value,
            )
            nvr_b = NVRDevice(
                name="NVR-Area-B",
                host="10.0.0.21",
                username="admin",
                password_enc="enc",
                area="B",
                current_status=NVRStatus.ONLINE.value,
            )
            session.add_all([nvr_a, nvr_b])
            await session.flush()

            session.add_all(
                [
                    NVRStatusLog(
                        nvr_id=nvr_a.id,
                        status=NVRStatus.OFFLINE.value,
                        checked_at=start + timedelta(minutes=1),
                    ),
                    NVRStatusLog(
                        nvr_id=nvr_a.id,
                        status=NVRStatus.ONLINE.value,
                        checked_at=start + timedelta(minutes=2),
                    ),
                    NVRStatusLog(
                        nvr_id=nvr_b.id,
                        status=NVRStatus.OFFLINE.value,
                        checked_at=start + timedelta(minutes=1),
                    ),
                    NVRStatusLog(
                        nvr_id=nvr_b.id,
                        status=NVRStatus.ONLINE.value,
                        checked_at=start + timedelta(minutes=2),
                    ),
                ]
            )
            await session.commit()

            report = await build_uptime_report(
                session, days=1, area="A", start=start, end=end
            )
            assert len(report.nvr_recoveries) == 1
            assert report.nvr_recoveries[0].area == "A"
            assert report.nvr_recoveries[0].name == "NVR-Area-A"
        await engine.dispose()

    asyncio.run(run())
