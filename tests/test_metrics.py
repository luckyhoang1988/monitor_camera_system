"""Test endpoint /metrics: render đúng định dạng Prometheus từ DB."""

from __future__ import annotations

import asyncio

from app.db.models import CameraChannel, NVRDevice
from app.enums import CameraStatus, NVRStatus, StorageStatus
from app.services.metrics_service import render_metrics
from tests.conftest import make_session


def test_render_metrics_outputs_prometheus_text():
    async def run():
        engine, Session = await make_session()
        async with Session() as session:
            nvr = NVRDevice(
                name="n", host="h", username="u", password_enc="x",
                current_status=NVRStatus.ONLINE.value,
                storage_status=StorageStatus.HEALTHY.value,
            )
            session.add(nvr)
            await session.commit()
            session.add(
                CameraChannel(
                    nvr_id=nvr.id, channel_no=1,
                    current_status=CameraStatus.ONLINE.value,
                )
            )
            await session.commit()

            body = (await render_metrics(session)).decode()
        await engine.dispose()

        assert "chek_nvr_total 1.0" in body
        assert 'chek_nvr_status{status="Online"} 1.0' in body
        assert "chek_nvr_camera_total 1.0" in body
        assert 'chek_nvr_storage_status{status="Healthy"} 1.0' in body
        assert "chek_nvr_alerts_open 0.0" in body

    asyncio.run(run())
