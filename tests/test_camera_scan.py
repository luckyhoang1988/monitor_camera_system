"""Integration test luồng quét camera + sinh/resolve alert trên DB in-memory.

Tập trung vào kịch bản LỖI thực tế (xem CLAUDE.md §5): khi fetch dữ liệu camera
thất bại/timeout thì KHÔNG được resolve nhầm alert và KHÔNG reset offline_since.
Dùng SQLite in-memory (StaticPool = chia sẻ 1 connection) nên không cần Postgres.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.collector.isapi_client import ChannelInfo
from app.db.base import Base
from app.db.models import Alert, CameraChannel, NVRDevice
from app.enums import AlertStatus, AlertType, CameraStatus, NVRStatus
from app.services import status_service
from app.services.alert_service import process_camera_alerts
from app.services.status_service import _update_cameras, update_nvr_cameras


async def _make_session():
    """Tạo engine SQLite in-memory + session factory, đã create_all bảng."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _add_nvr(session) -> NVRDevice:
    nvr = NVRDevice(
        name="NVR-Test",
        host="10.0.0.1",
        username="admin",
        password_enc="enc",  # giải mã được mock ở các test gọi update_nvr_cameras
        current_status=NVRStatus.ONLINE.value,
    )
    session.add(nvr)
    await session.flush()
    return nvr


async def _open_alerts(session):
    return (
        await session.scalars(
            select(Alert).where(Alert.status == AlertStatus.OPEN.value)
        )
    ).all()


async def _resolved_alerts(session):
    return (
        await session.scalars(
            select(Alert).where(Alert.status == AlertStatus.RESOLVED.value)
        )
    ).all()


def _patch_fetch(monkeypatch, channels, error):
    """Mock fetch_nvr_channels + decrypt_password trong status_service."""
    monkeypatch.setattr(status_service, "decrypt_password", lambda _enc: "pw")

    async def _fake_fetch(**_kwargs):
        return channels, error

    monkeypatch.setattr(status_service, "fetch_nvr_channels", _fake_fetch)


# --- Kịch bản 1: fetch timeout KHÔNG resolve nhầm alert (regression cho bug chính) ---


def test_fetch_timeout_keeps_alert_and_offline_since(monkeypatch):
    async def run():
        engine, Session = await _make_session()
        async with Session() as session:
            nvr = await _add_nvr(session)
            past = datetime.now(timezone.utc) - timedelta(minutes=30)
            cam = CameraChannel(
                nvr_id=nvr.id,
                channel_no=1,
                current_status=CameraStatus.OFFLINE.value,
                offline_since=past,
            )
            session.add(cam)
            session.add(
                Alert(
                    type=AlertType.CAMERA_OFFLINE.value,
                    nvr_id=nvr.id,
                    message="2 camera offline",
                    status=AlertStatus.OPEN.value,
                )
            )
            await session.commit()

            _patch_fetch(monkeypatch, channels=[], error="ReadTimeout")
            outcome = await update_nvr_cameras(session, nvr, timeout=5)

            assert outcome.ok is False
            assert outcome.alertable_offline == 0
            # Scheduler chỉ gọi process_camera_alerts khi ok=True -> ở đây KHÔNG gọi.
            await session.commit()

            assert len(await _open_alerts(session)) == 1
            assert await _resolved_alerts(session) == []
            cam_row = await session.get(CameraChannel, cam.id)
            assert cam_row.offline_since == past
        await engine.dispose()

    asyncio.run(run())


# --- Kịch bản 2: status endpoint trả rỗng (không có error) cũng coi là không tin cậy ---


def test_empty_channels_is_not_ok(monkeypatch):
    async def run():
        engine, Session = await _make_session()
        async with Session() as session:
            nvr = await _add_nvr(session)
            await session.commit()
            _patch_fetch(monkeypatch, channels=[], error=None)
            outcome = await update_nvr_cameras(session, nvr, timeout=5)
            assert outcome.ok is False
            assert outcome.alertable_offline == 0
        await engine.dispose()

    asyncio.run(run())


# --- Kịch bản 3: recovery -> alert được resolve, offline_since clear ---


def test_recovery_resolves_alert(monkeypatch):
    async def run():
        engine, Session = await _make_session()
        async with Session() as session:
            nvr = await _add_nvr(session)
            await session.commit()

            ch_off = [ChannelInfo(channel_no=1, raw_status="offline")]
            await _update_cameras(session, nvr.id, ch_off)
            cam = (await session.scalars(select(CameraChannel))).one()
            # Backdate để vượt ngưỡng phút -> lần quét sau tính là alertable.
            cam.offline_since = datetime.now(timezone.utc) - timedelta(hours=1)
            await session.flush()

            alertable = await _update_cameras(session, nvr.id, ch_off)
            assert alertable == 1
            await process_camera_alerts(session, nvr.id, nvr.name, alertable)
            await session.commit()
            assert len(await _open_alerts(session)) == 1

            # Camera lên lại -> alertable=0 -> resolve.
            ch_on = [ChannelInfo(channel_no=1, raw_status="online")]
            alertable2 = await _update_cameras(session, nvr.id, ch_on)
            assert alertable2 == 0
            await process_camera_alerts(session, nvr.id, nvr.name, alertable2)
            await session.commit()

            cam_row = await session.get(CameraChannel, cam.id)
            assert cam_row.offline_since is None
            assert await _open_alerts(session) == []
            # 2 alert resolved: alert offline được resolve + sự kiện recovery (báo Telegram).
            resolved = await _resolved_alerts(session)
            assert len(resolved) == 2
            assert {a.type for a in resolved} == {
                AlertType.CAMERA_OFFLINE.value,
                AlertType.CAMERA_RECOVERED.value,
            }
        await engine.dispose()

    asyncio.run(run())


# --- Kịch bản 4: trạng thái không tin cậy (UNKNOWN/NO_SIGNAL) giữ nguyên offline_since ---


def test_unreliable_status_keeps_offline_since():
    async def run():
        engine, Session = await _make_session()
        async with Session() as session:
            nvr = await _add_nvr(session)
            await session.commit()

            await _update_cameras(
                session, nvr.id, [ChannelInfo(channel_no=1, raw_status="offline")]
            )
            cam = (await session.scalars(select(CameraChannel))).one()
            before = cam.offline_since
            assert before is not None

            # raw_status không nhận diện -> UNKNOWN, không được xóa offline_since.
            await _update_cameras(
                session, nvr.id, [ChannelInfo(channel_no=1, raw_status="???")]
            )
            cam2 = await session.get(CameraChannel, cam.id)
            assert cam2.current_status == CameraStatus.UNKNOWN.value
            assert cam2.offline_since == before

            # NO_SIGNAL cũng là mơ hồ -> giữ nguyên.
            await _update_cameras(
                session, nvr.id, [ChannelInfo(channel_no=1, raw_status="no signal")]
            )
            cam3 = await session.get(CameraChannel, cam.id)
            assert cam3.current_status == CameraStatus.NO_SIGNAL.value
            assert cam3.offline_since == before
        await engine.dispose()

    asyncio.run(run())


# --- Kịch bản 5: NAT half-open dài chu kỳ -> alert không nhấp nháy mở/đóng ---


def test_nat_half_open_no_alert_flapping(monkeypatch):
    async def run():
        engine, Session = await _make_session()
        async with Session() as session:
            nvr = await _add_nvr(session)
            past = datetime.now(timezone.utc) - timedelta(minutes=30)
            session.add(
                CameraChannel(
                    nvr_id=nvr.id,
                    channel_no=1,
                    current_status=CameraStatus.OFFLINE.value,
                    offline_since=past,
                )
            )
            session.add(
                Alert(
                    type=AlertType.CAMERA_OFFLINE.value,
                    nvr_id=nvr.id,
                    message="1 camera offline",
                    status=AlertStatus.OPEN.value,
                )
            )
            await session.commit()

            _patch_fetch(monkeypatch, channels=[], error="ReadTimeout")
            for _ in range(5):  # nhiều chu kỳ fetch fail liên tiếp
                outcome = await update_nvr_cameras(session, nvr, timeout=5)
                assert outcome.ok is False
                if outcome.ok:  # mô phỏng đúng guard ở scheduler
                    await process_camera_alerts(
                        session, nvr.id, nvr.name, outcome.alertable_offline
                    )
                await session.commit()

            assert len(await _open_alerts(session)) == 1
            assert await _resolved_alerts(session) == []
        await engine.dispose()

    asyncio.run(run())
