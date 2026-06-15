"""Test các helper parse XML của ISAPI client (hàm thuần, không cần mạng)."""

from xml.etree import ElementTree as ET

from app.collector.isapi_client import _find_text, local_name

NS = "http://www.hikvision.com/ver20/XMLSchema"

DEVICE_INFO_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<DeviceInfo xmlns="{NS}">
  <deviceName>NVR-Khu-A</deviceName>
  <model>DS-7616NI-K2</model>
  <serialNumber>DS7616NI1620201231</serialNumber>
  <firmwareVersion>V4.30.000</firmwareVersion>
</DeviceInfo>
"""

CHANNELS_STATUS_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<InputProxyChannelStatusList xmlns="{NS}">
  <InputProxyChannelStatus>
    <id>1</id>
    <online>true</online>
  </InputProxyChannelStatus>
  <InputProxyChannelStatus>
    <id>2</id>
    <online>false</online>
  </InputProxyChannelStatus>
</InputProxyChannelStatusList>
"""


def test_local_name_strips_namespace():
    assert local_name(f"{{{NS}}}model") == "model"
    assert local_name("model") == "model"


def test_find_text_with_namespace():
    root = ET.fromstring(DEVICE_INFO_XML)
    assert _find_text(root, "model") == "DS-7616NI-K2"
    assert _find_text(root, "serialNumber") == "DS7616NI1620201231"
    assert _find_text(root, "firmwareVersion") == "V4.30.000"
    assert _find_text(root, "khong-ton-tai") is None


def test_parse_channel_status_online_flag():
    root = ET.fromstring(CHANNELS_STATUS_XML)
    results = {}
    for ch in root.iter():
        if local_name(ch.tag) != "InputProxyChannelStatus":
            continue
        cid = _find_text(ch, "id")
        raw = _find_text(ch, "online")
        results[int(cid)] = raw.lower() == "true"
    assert results == {1: True, 2: False}
