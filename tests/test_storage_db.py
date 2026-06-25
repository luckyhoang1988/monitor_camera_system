"""Test tích hợp DB cho đường ghi lưu trữ (update_nvr_storage).

Đây là đường đã gây sự cố production (NVR RAID trùng khóa nvr_hdd) mà test hàm thuần
không bắt được. Test ở đây ghi thật vào SQLite để chặn tái diễn.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from sqlalchemy import select

from app.collector.isapi_client import HddInfo, StorageInfo
from app.db.models import NVRDevice, NVRHdd, NVRStorageLog
from app.enums import NVRStatus, StorageStatus
from app.services import status_service
from tests.conftest import make_session


def _raid_storage() -> StorageInfo:
    """NVR RAID: 1 volume ảo RW + 2 đĩa vật lý RO, id TRÙNG nhau."""
    return StorageInfo(
        hdds=[
            HddInfo(hdd_id=1, hdd_type="Virtual Disk", capacity_mb=160000,
                    free_mb=64000, status="ok", is_recording=True),
            HddInfo(hdd_id=1, hdd_type="SATA", capacity_mb=13000,
                    free_mb=0, status="ok", is_recording=False),
            HddInfo(hdd_id=2, hdd_type="SATA", capacity_mb=13000,
                    free_mb=0, status="ok", is_recording=False),
        ],
        total_bitrate_kbps=64000,
    )


async def _seed_nvr(session) -> NVRDevice:
    nvr = NVRDevice(
        name="raid", host="h", username="u", password_enc="x",
        current_status=NVRStatus.ONLINE.value,
    )
    session.add(nvr)
    await session.commit()
    return nvr


def test_update_nvr_storage_raid_no_duplicate_key():
    async def run():
        engine, Session = await make_session()
        async with Session() as session:
            nvr = await _seed_nvr(session)
            storage = _raid_storage()

            async def fake_fetch(**_):
                return storage, None

            with patch.object(status_service, "fetch_nvr_storage", fake_fetch), \
                 patch.object(status_service, "decrypt_password", lambda _: "p"):
                outcome = await status_service.update_nvr_storage(
                    session, nvr, timeout=10, temp_warn_c=55
                )
                await session.commit()

            assert outcome.ok
            assert outcome.new_status == StorageStatus.HEALTHY
            # 3 ổ ghi đủ, KHÔNG vỡ unique (đây là bug cũ).
            hdds = (await session.execute(select(NVRHdd))).scalars().all()
            assert len(hdds) == 3
            # Dung lượng chỉ tính volume RW (160000), không cộng đĩa thành viên.
            assert nvr.storage_total_mb == 160000
            assert nvr.storage_used_pct == 60.0
            assert nvr.retention_days_est is not None
            logs = (await session.execute(select(NVRStorageLog))).scalars().all()
            assert len(logs) == 1
        await engine.dispose()

    asyncio.run(run())


def test_update_nvr_storage_twice_replaces_rows():
    """Quét 2 lần (delete+insert) -> vẫn 3 ổ, không tích lũy trùng."""
    async def run():
        engine, Session = await make_session()
        async with Session() as session:
            nvr = await _seed_nvr(session)

            async def fake_fetch(**_):
                return _raid_storage(), None

            with patch.object(status_service, "fetch_nvr_storage", fake_fetch), \
                 patch.object(status_service, "decrypt_password", lambda _: "p"):
                for _ in range(2):
                    await status_service.update_nvr_storage(
                        session, nvr, timeout=10, temp_warn_c=55
                    )
                    await session.commit()

            hdds = (await session.execute(select(NVRHdd))).scalars().all()
            assert len(hdds) == 3  # không thành 6
            logs = (await session.execute(select(NVRStorageLog))).scalars().all()
            assert len(logs) == 2  # mỗi lượt 1 dòng lịch sử
        await engine.dispose()

    asyncio.run(run())


def test_update_nvr_storage_fetch_error_keeps_state():
    """Fetch lỗi -> ok=False, KHÔNG ghi ổ/đổi trạng thái (tránh resolve nhầm)."""
    async def run():
        engine, Session = await make_session()
        async with Session() as session:
            nvr = await _seed_nvr(session)

            async def fake_fetch(**_):
                return None, "timeout"

            with patch.object(status_service, "fetch_nvr_storage", fake_fetch), \
                 patch.object(status_service, "decrypt_password", lambda _: "p"):
                outcome = await status_service.update_nvr_storage(
                    session, nvr, timeout=10, temp_warn_c=55
                )
                await session.commit()

            assert outcome.ok is False
            hdds = (await session.execute(select(NVRHdd))).scalars().all()
            assert len(hdds) == 0
            assert nvr.storage_last_error == "timeout"
        await engine.dispose()

    asyncio.run(run())
