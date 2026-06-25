"""Test parse ISAPI Storage + logic đánh giá sức khỏe lưu trữ (hàm thuần, không mạng)."""

import asyncio

import httpx

from app.collector.isapi_client import ISAPIClient, StorageInfo
from app.collector.storage_checker import estimate_retention_days, evaluate_storage
from app.enums import StorageStatus

NS_STREAM = "http://www.hikvision.com/ver20/XMLSchema"

# 2 camera: main stream (id 101, 201) có vbrUpperCap; sub stream (102) phải bị bỏ qua.
STREAMING_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<StreamingChannelList xmlns="{NS_STREAM}">
  <StreamingChannel><id>101</id><Video><vbrUpperCap>4096</vbrUpperCap></Video></StreamingChannel>
  <StreamingChannel><id>102</id><Video><vbrUpperCap>1024</vbrUpperCap></Video></StreamingChannel>
  <StreamingChannel><id>201</id><Video><constantBitRate>2048</constantBitRate></Video></StreamingChannel>
</StreamingChannelList>
"""

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


# NVR RAID: 1 volume ảo (RW, ghi hình) + 2 đĩa vật lý (RO) TRÙNG id với nhau/volume.
RAID_STORAGE_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<storage xmlns="{NS}">
  <hddList>
    <hdd>
      <id>1</id><hddName>hdd1</hddName><hddType>Virtual Disk</hddType>
      <status>ok</status><capacity>160000</capacity><freeSpace>64000</freeSpace><property>RW</property>
    </hdd>
    <hdd>
      <id>1</id><hddType>SATA</hddType>
      <status>ok</status><capacity>13000</capacity><freeSpace>0</freeSpace><property>RO</property>
    </hdd>
    <hdd>
      <id>2</id><hddType>SATA</hddType>
      <status>ok</status><capacity>13000</capacity><freeSpace>0</freeSpace><property>RO</property>
    </hdd>
  </hddList>
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
    """Giả lập httpx.AsyncClient.get: /Storage + /Streaming/channels, 404 cho S.M.A.R.T."""

    def __init__(self):
        self.calls = []

    async def get(self, path, auth=None):
        self.calls.append(path)
        if path == "/ISAPI/ContentMgmt/Storage":
            return _FakeResp(text=STORAGE_XML)
        if path == "/ISAPI/Streaming/channels":
            return _FakeResp(text=STREAMING_XML)
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


def _eval(hdds, raid=None, temp=55):
    return evaluate_storage(
        StorageInfo(hdds=hdds, raid_status=raid),
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


def test_evaluate_full_disk_is_healthy():
    # NVR ghi đè -> đĩa đầy 95% vẫn HEALTHY, KHÔNG còn là Critical.
    ev = _eval([_hdd(1, cap=1000, free=50)])
    assert ev.overall == StorageStatus.HEALTHY
    assert ev.used_pct == 95.0  # vẫn hiển thị %


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


def test_get_record_bitrate_sums_main_streams_only():
    # Main stream 101 (vbrUpperCap 4096) + 201 (constantBitRate 2048) = 6144;
    # sub stream 102 (kết thúc '02') bị bỏ qua.
    kbps = asyncio.run(_client().get_record_bitrate_kbps(_PathClient()))
    assert kbps == 4096 + 2048


def test_estimate_retention_days():
    # 4 TB (~3,815,447 MB) với 64 Mbps (64000 kbps) -> ~5.5 ngày ghi liên tục.
    days = estimate_retention_days(3_815_447, 64_000)
    assert 5.0 < days < 6.0
    # Thiếu dữ liệu -> None.
    assert estimate_retention_days(None, 64_000) is None
    assert estimate_retention_days(1000, None) is None
    assert estimate_retention_days(1000, 0) is None


# NVR có khay trống: 1 ổ ok + 2 khay "notexist" (capacity 0) phải bị loại.
EMPTYBAY_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<storage xmlns="{NS}">
  <hddList>
    <hdd><id>1</id><hddType>SATA</hddType><status>ok</status>
      <capacity>13000</capacity><freeSpace>5000</freeSpace><property>RW</property></hdd>
    <hdd><id>2</id><hddType>SATA</hddType><status>notexist</status>
      <capacity>0</capacity><freeSpace>0</freeSpace><property>RW</property></hdd>
    <hdd><id>3</id><hddType>SATA</hddType><status>notexist</status>
      <capacity>0</capacity><freeSpace>0</freeSpace><property>RW</property></hdd>
  </hddList>
</storage>
"""


class _EmptyBayClient:
    async def get(self, path, auth=None):
        if path == "/ISAPI/ContentMgmt/Storage":
            return _FakeResp(text=EMPTYBAY_XML)
        return _FakeResp(status_code=404, text="<err/>")


def test_empty_bays_excluded():
    storage = asyncio.run(_client().get_storage_info(_EmptyBayClient()))
    assert len(storage.hdds) == 1  # 2 khay notexist bị loại
    ev = evaluate_storage(storage, temp_warn_c=55)
    assert ev.hdd_count == 1
    assert ev.overall == StorageStatus.HEALTHY


class _RaidClient:
    """Trả XML RAID (volume ảo + đĩa vật lý trùng id), 404 cho S.M.A.R.T."""

    async def get(self, path, auth=None):
        if path == "/ISAPI/ContentMgmt/Storage":
            return _FakeResp(text=RAID_STORAGE_XML)
        return _FakeResp(status_code=404, text="<err/>")


def test_raid_parsing_keeps_duplicate_ids_no_crash():
    storage = asyncio.run(_client().get_storage_info(_RaidClient()))
    # 1 volume ảo + 2 đĩa vật lý (id 1 trùng giữa ảo và vật lý) -> giữ đủ 3, không gộp.
    assert len(storage.hdds) == 3
    virtual = [h for h in storage.hdds if h.hdd_type == "Virtual Disk"]
    physical = [h for h in storage.hdds if h.hdd_type == "SATA"]
    assert len(virtual) == 1 and len(physical) == 2
    assert virtual[0].is_recording is True
    assert all(p.is_recording is False for p in physical)


def test_raid_capacity_uses_recording_volume_only():
    storage = asyncio.run(_client().get_storage_info(_RaidClient()))
    ev = evaluate_storage(storage, temp_warn_c=55)
    # Dung lượng tính theo volume ảo RW (160000), KHÔNG cộng đĩa thành viên RO.
    assert ev.total_mb == 160000
    assert ev.used_pct == 60.0  # (160000-64000)/160000
    assert ev.hdd_count == 2  # đếm theo đĩa vật lý
    assert ev.overall == StorageStatus.HEALTHY
