"""Test parse ISAPI Storage + logic đánh giá sức khỏe lưu trữ (hàm thuần, không mạng)."""

import asyncio

import httpx
import pytest

from app.collector.isapi_client import ISAPIClient, StorageInfo
from app.collector.storage_checker import evaluate_storage
from app.enums import StorageStatus

NS = "http://www.hikvision.com/ver20/XMLSchema"

# 2 ổ: 1 ok đang ghi (R/W), 1 báo error. Có RAID degraded.
STORAGE_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<storage xmlns="{NS}">
  <hddList>
    <hdd>
      <id>1</id>
      <hddName>HDD1</hddName>
      <capacity>3815447</capacity>
      <freeSpace>1024000</freeSpace>
      <status>ok</status>
      <property>RW</property>
    </hdd>
    <hdd>
      <id>2</id>
      <hddName>HDD2</hddName>
      <capacity>3815447</capacity>
      <freeSpace>0</freeSpace>
      <status>error</status>
      <property>R</property>
    </hdd>
  </hddList>
  <raidList>
    <raid>
      <id>1</id>
      <status>degraded</status>
    </raid>
  </raidList>
</storage>
"""


class _FakeResp:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=None)


class _PathClient:
    """Giả lập httpx.AsyncClient.get: trả XML cho /Storage, 404 cho endpoint S.M.A.R.T."""

    def __init__(self):
        self.calls = []

    async def get(self, path, auth=None):
        self.calls.append(path)
        if path == "/ISAPI/ContentMgmt/Storage":
            return _FakeResp(text=STORAGE_XML)
        # S.M.A.R.T không hỗ trợ -> 404 (get_storage_info phải nuốt lỗi này).
        return _FakeResp(status_code=404, text="<err/>")


def _client():
    return ISAPIClient("h", "u", "p")


def test_get_storage_info_parses_hdds_and_raid():
    storage = asyncio.run(_client().get_storage_info(_PathClient()))
    assert len(storage.hdds) == 2
    h1, h2 = sorted(storage.hdds, key=lambda h: h.hdd_id)
    assert h1.name == "HDD1"
    assert h1.capacity_mb == 3815447
    assert h1.free_mb == 1024000
    assert h1.status == "ok"
    assert h1.is_recording is True  # property RW
    assert h2.status == "error"
    assert h2.is_recording is False  # property R -> chỉ đọc
    assert storage.raid_status == "degraded"
    # S.M.A.R.T 404 -> bỏ qua, không raise; các trường để None.
    assert h1.smart_health is None
    assert h1.temperature_c is None


def _hdd(hdd_id, status="ok", cap=1000, free=500, rec=True, temp=None):
    from app.collector.isapi_client import HddInfo

    return HddInfo(
        hdd_id=hdd_id,
        name=f"HDD{hdd_id}",
        capacity_mb=cap,
        free_mb=free,
        status=status,
        is_recording=rec,
        temperature_c=temp,
    )


def _eval(hdds, raid=None, warn=80, crit=90, temp=55):
    return evaluate_storage(
        StorageInfo(hdds=hdds, raid_status=raid),
        warn_pct=warn,
        crit_pct=crit,
        temp_warn_c=temp,
    )


def test_evaluate_healthy():
    ev = _eval([_hdd(1, free=500), _hdd(2, free=500)])  # 50% dùng
    assert ev.overall == StorageStatus.HEALTHY
    assert ev.hdd_count == 2
    assert ev.hdd_healthy_count == 2
    assert ev.used_pct == 50.0


def test_evaluate_critical_on_disk_error():
    ev = _eval([_hdd(1), _hdd(2, status="error")])
    assert ev.overall == StorageStatus.CRITICAL
    assert ev.has_disk_error is True
    assert ev.hdd_error_count == 1


def test_evaluate_critical_on_full():
    ev = _eval([_hdd(1, cap=1000, free=50)])  # 95% dùng
    assert ev.overall == StorageStatus.CRITICAL
    assert ev.is_full_critical is True


def test_evaluate_warning_on_used_pct():
    ev = _eval([_hdd(1, cap=1000, free=150)])  # 85% dùng
    assert ev.overall == StorageStatus.WARNING
    assert ev.is_full_critical is False


def test_evaluate_warning_on_raid_degraded():
    ev = _eval([_hdd(1, free=900)], raid="degraded")  # 10% dùng nhưng RAID hỏng
    assert ev.overall == StorageStatus.WARNING


def test_evaluate_warning_on_high_temp():
    ev = _eval([_hdd(1, free=900, temp=60)])  # nóng >= 55°C
    assert ev.overall == StorageStatus.WARNING


def test_evaluate_critical_when_none_recording():
    # Có ổ ok nhưng KHÔNG ổ nào đang ghi -> mất ghi hình -> Critical.
    ev = _eval([_hdd(1, rec=False), _hdd(2, rec=False)])
    assert ev.overall == StorageStatus.CRITICAL
    assert ev.has_disk_error is True


def test_evaluate_unknown_no_hdds():
    ev = _eval([])
    assert ev.overall == StorageStatus.UNKNOWN
    assert ev.used_pct is None


def test_evaluate_ignores_recording_when_property_unknown():
    # Firmware không báo property (is_recording=None) -> KHÔNG kết luận mất ghi hình.
    ev = _eval([_hdd(1, rec=None, free=900)])
    assert ev.overall == StorageStatus.HEALTHY
