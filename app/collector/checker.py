"""Kiểm tra NVR nhiều lớp (ping -> port -> ISAPI) + state machine chống flapping.

Quy ước (xem CLAUDE.md §5):
- API hợp lệ            -> Online
- HTTP 401             -> Auth Error
- Ping/port OK, API lỗi/chậm -> Warning
- Không tới được       -> Network Error (sau N lần liên tiếp mới chốt Offline)
"""

from __future__ import annotations

import asyncio
import platform
import time
from dataclasses import dataclass, field

import httpx

from app.collector.http_pool import get_client
from app.collector.isapi_client import (
    ChannelInfo,
    DeviceInfo,
    ISAPIAuthError,
    ISAPIClient,
    ISAPIError,
    StorageInfo,
    get_cert_fingerprint,
    normalize_fingerprint,
)
from app.enums import NVRStatus


class TLSFingerprintMismatch(ISAPIError):
    """Fingerprint cert TLS không khớp giá trị đã pin (nghi ngờ MITM)."""


@dataclass
class NVRCheckResult:
    """Kết quả thô của một lần kiểm tra NVR (chưa áp state machine)."""

    raw_status: NVRStatus
    ping_ok: bool = False
    port_ok: bool = False
    response_time_ms: int | None = None
    error: str | None = None
    device: DeviceInfo | None = None
    channels: list[ChannelInfo] = field(default_factory=list)


async def ping_host(host: str, timeout: int = 3) -> bool:
    """Ping ICMP một lần, cross-platform (Windows dùng -n, Unix dùng -c)."""
    is_windows = platform.system().lower() == "windows"
    count_flag = "-n" if is_windows else "-c"
    timeout_flag = "-w" if is_windows else "-W"
    # Windows -w nhận mili-giây, Unix -W nhận giây.
    timeout_val = str(timeout * 1000) if is_windows else str(timeout)
    try:
        proc = await asyncio.create_subprocess_exec(
            "ping",
            count_flag,
            "1",
            timeout_flag,
            timeout_val,
            host,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        rc = await asyncio.wait_for(proc.wait(), timeout=timeout + 2)
        return rc == 0
    except (asyncio.TimeoutError, OSError):
        return False


async def check_tcp_port(host: str, port: int, timeout: int = 3) -> bool:
    """Thử mở TCP connection tới host:port."""
    try:
        fut = asyncio.open_connection(host, port)
        reader, writer = await asyncio.wait_for(fut, timeout=timeout)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001 - đóng socket không quan trọng nếu lỗi
            pass
        return True
    except (asyncio.TimeoutError, OSError):
        return False


async def _assert_fingerprint(
    host: str, port: int, tls_fingerprint: str, timeout: int
) -> None:
    """Pin SHA-256 cert; sai -> raise TLSFingerprintMismatch (nghi ngờ MITM)."""
    actual = await get_cert_fingerprint(host, port, timeout=min(timeout, 5))
    if normalize_fingerprint(actual) != normalize_fingerprint(tls_fingerprint):
        raise TLSFingerprintMismatch(
            f"TLS fingerprint không khớp (nghi ngờ MITM): thực tế {actual}"
        )


async def check_nvr(
    host: str,
    username: str,
    password: str,
    port: int = 80,
    use_https: bool = False,
    timeout: int = 10,
    *,
    tls_fingerprint: str | None = None,
    retries: int = 0,
    retry_backoff_base: float = 0.5,
    fetch_channels: bool = True,
) -> NVRCheckResult:
    """Kiểm tra một NVR theo 3 lớp, trả về trạng thái thô (raw_status).

    - Dùng `AsyncClient` dùng chung từ pool (verify/timeout cấu hình ở pool).
    - `tls_fingerprint`: nếu đặt (và use_https), pin SHA-256 cert -> chặn MITM
      dù verify=False; sai fingerprint -> Warning kèm last_error rõ ràng.
    - `fetch_channels=False`: chỉ kiểm tra sức khỏe NVR (device), bỏ qua camera
      (dùng cho job health tần suất cao; camera quét ở job riêng).
    """
    ping_ok = await ping_host(host, timeout=min(timeout, 3))
    port_ok = await check_tcp_port(host, port, timeout=min(timeout, 3))

    client_obj = ISAPIClient(
        host,
        username,
        password,
        port,
        use_https,
        timeout,
        retries=retries,
        retry_backoff_base=retry_backoff_base,
    )
    start = time.perf_counter()
    try:
        # Pin fingerprint trước khi gọi API (chỉ khi HTTPS + đã cấu hình).
        if use_https and tls_fingerprint:
            await _assert_fingerprint(host, port, tls_fingerprint, timeout)

        client = await get_client(client_obj.base_url)
        device = await client_obj.get_device_info(client)
        channels = (
            await client_obj.get_channels(client) if fetch_channels else []
        )
        elapsed = int((time.perf_counter() - start) * 1000)
        return NVRCheckResult(
            raw_status=NVRStatus.ONLINE,
            ping_ok=ping_ok,
            port_ok=port_ok,
            response_time_ms=elapsed,
            device=device,
            channels=channels,
        )
    except ISAPIAuthError as exc:
        return NVRCheckResult(
            raw_status=NVRStatus.AUTH_ERROR,
            ping_ok=ping_ok,
            port_ok=port_ok,
            error=str(exc),
        )
    except (ISAPIError, httpx.HTTPError, OSError, asyncio.TimeoutError) as exc:
        # Tới được thiết bị nhưng API lỗi/timeout -> Warning; nếu không -> Network Error
        raw = NVRStatus.WARNING if (ping_ok or port_ok) else NVRStatus.NETWORK_ERROR
        return NVRCheckResult(
            raw_status=raw,
            ping_ok=ping_ok,
            port_ok=port_ok,
            error=str(exc),
        )


async def fetch_nvr_channels(
    host: str,
    username: str,
    password: str,
    port: int = 80,
    use_https: bool = False,
    timeout: int = 10,
    *,
    tls_fingerprint: str | None = None,
    retries: int = 0,
    retry_backoff_base: float = 0.5,
) -> tuple[list[ChannelInfo], str | None]:
    """Chỉ lấy danh sách kênh + trạng thái camera của NVR (job camera).

    Dùng cho NVR đã Online: bỏ ping/port/deviceInfo để giảm tải. Trả về
    `(channels, error)`; lỗi chỉ trả error để caller log, không đổi trạng thái NVR.
    """
    client_obj = ISAPIClient(
        host,
        username,
        password,
        port,
        use_https,
        timeout,
        retries=retries,
        retry_backoff_base=retry_backoff_base,
    )
    try:
        if use_https and tls_fingerprint:
            await _assert_fingerprint(host, port, tls_fingerprint, timeout)
        client = await get_client(client_obj.base_url)
        channels = await client_obj.get_channels(client)
        return channels, None
    except (ISAPIError, httpx.HTTPError, OSError, asyncio.TimeoutError) as exc:
        return [], str(exc)


async def fetch_nvr_storage(
    host: str,
    username: str,
    password: str,
    port: int = 80,
    use_https: bool = False,
    timeout: int = 10,
    *,
    tls_fingerprint: str | None = None,
    retries: int = 0,
    retry_backoff_base: float = 0.5,
) -> tuple[StorageInfo | None, str | None]:
    """Chỉ lấy trạng thái lưu trữ (HDD/RAID/S.M.A.R.T) của NVR (job storage).

    Song song với `fetch_nvr_channels`: dùng cho NVR đã Online, bỏ ping/port/deviceInfo
    để giảm tải. Trả về `(storage, error)`; lỗi chỉ trả error để caller log, KHÔNG đổi
    trạng thái NVR.
    """
    client_obj = ISAPIClient(
        host,
        username,
        password,
        port,
        use_https,
        timeout,
        retries=retries,
        retry_backoff_base=retry_backoff_base,
    )
    try:
        if use_https and tls_fingerprint:
            await _assert_fingerprint(host, port, tls_fingerprint, timeout)
        client = await get_client(client_obj.base_url)
        storage = await client_obj.get_storage_info(client)
        return storage, None
    except (ISAPIError, httpx.HTTPError, OSError, asyncio.TimeoutError) as exc:
        return None, str(exc)


# --- State machine chống flapping (hàm thuần, dễ test) ---

# Các trạng thái coi là "thất bại" cần qua bộ đếm trước khi chốt Offline.
# WARNING gộp vào đây để bắt case NAT half-open (port mở nhưng API lỗi/timeout
# liên tục): NVR public sau port-forward có thể vẫn mở cổng TCP dù đã chết.
_FAILURE_STATES = {NVRStatus.NETWORK_ERROR, NVRStatus.WARNING}


def apply_state_machine(
    raw_status: NVRStatus,
    prev_fail_count: int,
    fail_threshold: int,
) -> tuple[NVRStatus, int]:
    """Áp bộ đếm xác nhận.

    Trả về (trạng thái hiệu lực, fail_count mới).
    - Lỗi kết nối: tăng fail_count; chỉ chốt Offline khi đạt ngưỡng,
      trước đó giữ Warning để tránh báo động giả (flapping).
    - Các trạng thái khác: reset fail_count, dùng nguyên trạng thái thô.
    """
    if raw_status in _FAILURE_STATES:
        new_count = prev_fail_count + 1
        if new_count >= fail_threshold:
            return NVRStatus.OFFLINE, new_count
        return NVRStatus.WARNING, new_count
    return raw_status, 0
