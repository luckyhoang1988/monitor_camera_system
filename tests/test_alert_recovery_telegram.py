"""Đảm bảo alert recovery (NVR online lại) luôn được đẩy lên Telegram.

Regression: trước đây alert NVR_RECOVERED được tạo với status OPEN và không bao
giờ resolve, nên từ lần hồi phục thứ 2 trở đi bị dedupe chặn -> không gọi
queue_alert -> không gửi Telegram. is_event=True phải bỏ qua dedupe.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.config import get_settings
from app.db.base import Base
from app.db.models import Alert
from app.enums import AlertStatus, AlertType, NVRStatus
from app.services import alert_service
from app.services.status_service import (
    CameraEvent,
    CameraScanOutcome,
    NVRHealthOutcome,
)
from app.services.telegram_notifier import _QUEUE_KEY


async def _make_session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _recovery_outcome(nvr_id: int) -> NVRHealthOutcome:
    return NVRHealthOutcome(
        nvr_id=nvr_id,
        prev_status=NVRStatus.OFFLINE,
        new_status=NVRStatus.ONLINE,
        response_time_ms=50,
    )


def test_recovery_queues_telegram_every_time():
    # Bật Telegram để queue_alert hoạt động (token/chat không cần cho việc xếp hàng).
    # get_settings() là lru_cache -> mutate trực tiếp instance singleton.
    settings = get_settings()
    settings.telegram_enabled = True

    async def run():
        engine, Session = await _make_session()
        async with Session() as session:
            # Hồi phục lần 1.
            await alert_service.process_nvr_alerts(
                session, _recovery_outcome(1), "NVR-A"
            )
            queue1 = list(session.info.get(_QUEUE_KEY, []))
            session.info.pop(_QUEUE_KEY, None)  # mô phỏng flush sau commit

            # Hồi phục lần 2 (sau khi lại offline rồi online).
            await alert_service.process_nvr_alerts(
                session, _recovery_outcome(1), "NVR-A"
            )
            queue2 = list(session.info.get(_QUEUE_KEY, []))

            # Cả hai lần đều phải xếp hàng Telegram.
            assert any("online trở lại" in t for t in queue1)
            assert any("online trở lại" in t for t in queue2)

            # Alert recovery được ghi RESOLVED (sự kiện), không treo OPEN.
            rows = (
                await session.execute(
                    select(Alert).where(
                        Alert.type == AlertType.NVR_RECOVERED.value
                    )
                )
            ).scalars().all()
            assert len(rows) == 2
            assert all(r.status == AlertStatus.RESOLVED.value for r in rows)
        await engine.dispose()

    asyncio.run(run())
    settings.telegram_enabled = False


def _scan(offline=(), recovered=()):
    return CameraScanOutcome(
        ok=True, offline_alertable=list(offline), recovered=list(recovered)
    )


def test_camera_recovery_notifies_per_camera_only_after_offline_alert():
    """Recovery camera báo chi tiết (kênh/tên) và chỉ khi trước đó có alert offline mở."""
    settings = get_settings()
    settings.telegram_enabled = True

    async def run():
        engine, Session = await _make_session()
        async with Session() as session:
            nvr_id = 1
            cam = CameraEvent(camera_id=10, channel_no=3, name="Cổng chính")

            # Quét bình thường (không có sự kiện) -> không báo.
            await alert_service.process_camera_alerts(session, nvr_id, "NVR-A", _scan())
            assert not session.info.get(_QUEUE_KEY)

            # Camera offline đủ lâu -> alert offline chi tiết (có kênh + tên).
            await alert_service.process_camera_alerts(
                session, nvr_id, "NVR-A", _scan(offline=[cam])
            )
            offline_q = list(session.info.get(_QUEUE_KEY, []))
            assert any("kênh 3" in t and "Cổng chính" in t for t in offline_q)
            session.info.pop(_QUEUE_KEY, None)  # mô phỏng flush

            # Camera online lại -> resolve + báo recovery chi tiết.
            await alert_service.process_camera_alerts(
                session, nvr_id, "NVR-A", _scan(recovered=[cam])
            )
            queue = list(session.info.get(_QUEUE_KEY, []))
            assert any("kênh 3" in t and "online trở lại" in t for t in queue)

            # Recovery lần nữa khi đã hết alert mở -> không báo lại, tránh spam.
            session.info.pop(_QUEUE_KEY, None)
            await alert_service.process_camera_alerts(
                session, nvr_id, "NVR-A", _scan(recovered=[cam])
            )
            assert not session.info.get(_QUEUE_KEY)

            # Alert recovery camera ghi RESOLVED, gắn đúng camera_id, không treo OPEN.
            rows = (
                await session.execute(
                    select(Alert).where(
                        Alert.type == AlertType.CAMERA_RECOVERED.value
                    )
                )
            ).scalars().all()
            assert len(rows) == 1
            assert rows[0].status == AlertStatus.RESOLVED.value
            assert rows[0].camera_id == 10
        await engine.dispose()

    asyncio.run(run())
    settings.telegram_enabled = False


def test_camera_alerts_grouped_into_single_message_per_cycle():
    """Nhiều camera cùng NVR rớt/lên trong 1 chu kỳ -> gộp thành 1 tin liệt kê kênh."""
    settings = get_settings()
    settings.telegram_enabled = True

    async def run():
        engine, Session = await _make_session()
        async with Session() as session:
            nvr_id = 1
            cams = [
                CameraEvent(camera_id=10, channel_no=3, name="Cổng chính"),
                CameraEvent(camera_id=11, channel_no=5, name="Bãi xe"),
                CameraEvent(camera_id=12, channel_no=7, name=None),
            ]

            # 3 camera offline cùng lúc -> đúng 1 tin nhắn, liệt kê cả 3 kênh.
            await alert_service.process_camera_alerts(
                session, nvr_id, "NVR-A", _scan(offline=cams)
            )
            offline_q = list(session.info.get(_QUEUE_KEY, []))
            assert len(offline_q) == 1
            msg = offline_q[0]
            assert "3 camera" in msg
            assert "kênh 3 (Cổng chính)" in msg
            assert "kênh 5 (Bãi xe)" in msg
            assert "kênh 7" in msg
            session.info.pop(_QUEUE_KEY, None)

            # 3 camera online lại cùng lúc -> đúng 1 tin recovery gộp.
            await alert_service.process_camera_alerts(
                session, nvr_id, "NVR-A", _scan(recovered=cams)
            )
            rec_q = list(session.info.get(_QUEUE_KEY, []))
            assert len(rec_q) == 1
            assert "3 camera đã online trở lại" in rec_q[0]
            assert "kênh 5 (Bãi xe)" in rec_q[0]
        await engine.dispose()

    asyncio.run(run())
    settings.telegram_enabled = False
