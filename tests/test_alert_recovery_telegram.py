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
from app.services.status_service import NVRHealthOutcome
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


def test_camera_recovery_notifies_only_after_offline_alert():
    """Camera online lại chỉ báo Telegram khi trước đó đang có alert offline mở."""
    settings = get_settings()
    settings.telegram_enabled = True

    async def run():
        engine, Session = await _make_session()
        async with Session() as session:
            nvr_id = 1

            # Quét bình thường (0 camera offline) khi chưa từng có alert -> không báo.
            await alert_service.process_camera_alerts(session, nvr_id, "NVR-A", 0)
            assert not session.info.get(_QUEUE_KEY)

            # Có camera offline đủ lâu -> tạo alert offline (xếp hàng cảnh báo).
            await alert_service.process_camera_alerts(session, nvr_id, "NVR-A", 2)
            session.info.pop(_QUEUE_KEY, None)  # mô phỏng flush

            # Camera online lại -> resolve + báo recovery.
            await alert_service.process_camera_alerts(session, nvr_id, "NVR-A", 0)
            queue = list(session.info.get(_QUEUE_KEY, []))
            assert any("online trở lại" in t for t in queue)

            # Quét tiếp vẫn 0 offline (đã hết alert mở) -> không báo lại, tránh spam.
            session.info.pop(_QUEUE_KEY, None)
            await alert_service.process_camera_alerts(session, nvr_id, "NVR-A", 0)
            assert not session.info.get(_QUEUE_KEY)

            # Alert recovery camera ghi RESOLVED, không treo OPEN.
            rows = (
                await session.execute(
                    select(Alert).where(
                        Alert.type == AlertType.CAMERA_RECOVERED.value
                    )
                )
            ).scalars().all()
            assert len(rows) == 1
            assert rows[0].status == AlertStatus.RESOLVED.value
        await engine.dispose()

    asyncio.run(run())
    settings.telegram_enabled = False
