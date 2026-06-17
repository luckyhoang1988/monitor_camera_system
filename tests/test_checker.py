"""Test state machine NVR và map trạng thái camera (hàm thuần)."""

from app.collector.camera_checker import evaluate_cameras, map_channel_status
from app.collector.checker import apply_state_machine
from app.collector.isapi_client import ChannelInfo
from app.enums import CameraStatus, NVRStatus

THRESHOLD = 3


def test_online_resets_fail_count():
    status, count = apply_state_machine(NVRStatus.ONLINE, prev_fail_count=2, fail_threshold=THRESHOLD)
    assert status == NVRStatus.ONLINE
    assert count == 0


def test_network_error_warns_before_threshold():
    # Lần lỗi 1 và 2 -> Warning, chưa chốt Offline.
    status, count = apply_state_machine(NVRStatus.NETWORK_ERROR, prev_fail_count=0, fail_threshold=THRESHOLD)
    assert status == NVRStatus.WARNING and count == 1
    status, count = apply_state_machine(NVRStatus.NETWORK_ERROR, prev_fail_count=1, fail_threshold=THRESHOLD)
    assert status == NVRStatus.WARNING and count == 2


def test_network_error_offline_at_threshold():
    status, count = apply_state_machine(NVRStatus.NETWORK_ERROR, prev_fail_count=2, fail_threshold=THRESHOLD)
    assert status == NVRStatus.OFFLINE and count == 3


def test_warning_warns_before_threshold():
    # Warning kéo dài (port mở, API lỗi/timeout) cũng đếm như lỗi kết nối.
    status, count = apply_state_machine(NVRStatus.WARNING, prev_fail_count=0, fail_threshold=THRESHOLD)
    assert status == NVRStatus.WARNING and count == 1
    status, count = apply_state_machine(NVRStatus.WARNING, prev_fail_count=1, fail_threshold=THRESHOLD)
    assert status == NVRStatus.WARNING and count == 2


def test_warning_offline_at_threshold():
    # Bắt case NAT half-open: Warning liên tục đạt ngưỡng -> chốt Offline.
    status, count = apply_state_machine(NVRStatus.WARNING, prev_fail_count=2, fail_threshold=THRESHOLD)
    assert status == NVRStatus.OFFLINE and count == 3


def test_auth_error_is_immediate_no_counter():
    status, count = apply_state_machine(NVRStatus.AUTH_ERROR, prev_fail_count=5, fail_threshold=THRESHOLD)
    assert status == NVRStatus.AUTH_ERROR and count == 0


def test_map_channel_status_boolean_flags():
    assert map_channel_status(ChannelInfo(channel_no=1, online=True)) == CameraStatus.ONLINE
    assert map_channel_status(ChannelInfo(channel_no=2, online=False)) == CameraStatus.OFFLINE
    assert map_channel_status(ChannelInfo(channel_no=3, online=None)) == CameraStatus.UNKNOWN


def test_map_channel_status_raw_strings():
    assert map_channel_status(ChannelInfo(channel_no=1, raw_status="online")) == CameraStatus.ONLINE
    assert map_channel_status(ChannelInfo(channel_no=2, raw_status="offline")) == CameraStatus.OFFLINE
    assert map_channel_status(ChannelInfo(channel_no=3, raw_status="no signal")) == CameraStatus.NO_SIGNAL
    assert map_channel_status(ChannelInfo(channel_no=4, raw_status="disabled")) == CameraStatus.DISABLED


def test_evaluate_cameras_list():
    channels = [
        ChannelInfo(channel_no=1, name="Cong chinh", online=True),
        ChannelInfo(channel_no=2, name="Bai xe", online=False),
    ]
    results = evaluate_cameras(channels)
    assert [r.status for r in results] == [CameraStatus.ONLINE, CameraStatus.OFFLINE]
    assert results[0].name == "Cong chinh"
