"""Test rollup uptime NVR theo ngày (rollup_nvr_day)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from sqlalchemy import select

from app.db.models import DailyNvrUptime, NVRDevice, NVRStatusLog
from app.enums import NVRStatus
from app.services.rollup_service import rollup_nvr_day
from tests.conftest import make_session


def test_rollup_nvr_day_aggregates_uptime():
    async def run():
        engine, Session = await make_session()
        async with Session() as session:
            nvr = NVRDevice(name="n", host="h", username="u", password_enc="x")
            session.add(nvr)
            await session.commit()

            day = datetime(2026, 6, 20, tzinfo=UTC)
            # 4 lần kiểm tra trong ngày: 3 Online, 1 Offline -> 75%.
            for hh, st in [(1, "Online"), (7, "Online"), (13, "Offline"), (19, "Online")]:
                session.add(
                    NVRStatusLog(
                        nvr_id=nvr.id, status=st,
                        checked_at=day.replace(hour=hh),
                    )
                )
            # 1 log NGÀY KHÁC -> không được tính vào ngày 20.
            session.add(
                NVRStatusLog(
                    nvr_id=nvr.id, status=NVRStatus.ONLINE.value,
                    checked_at=datetime(2026, 6, 21, 3, tzinfo=UTC),
                )
            )
            await session.commit()

            n = await rollup_nvr_day(session, day.date())
            await session.commit()
            assert n == 1

            row = (
                await session.execute(select(DailyNvrUptime))
            ).scalars().one()
            assert row.total_checks == 4
            assert row.online_checks == 3
            assert row.uptime_pct == 75.0

            # Chạy lại cùng ngày -> idempotent (không nhân đôi).
            await rollup_nvr_day(session, day.date())
            await session.commit()
            rows = (await session.execute(select(DailyNvrUptime))).scalars().all()
            assert len(rows) == 1
        await engine.dispose()

    asyncio.run(run())
