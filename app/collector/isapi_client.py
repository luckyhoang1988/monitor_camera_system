"""Client gọi Hikvision ISAPI (async, HTTP Digest auth) và parse XML.

Lưu ý (xem CLAUDE.md §4):
- ISAPI chạy trên port 80/443, dùng HTTP Digest auth (không phải Basic).
- Phản hồi là XML có namespace -> dùng helper local_name() bỏ qua namespace.
- HTTP 401 = sai tài khoản/mật khẩu (Auth Error), không phải offline.
"""

from __future__ import annotations

import asyncio
import hashlib
import random
import re
import ssl
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

import httpx

# Lỗi mạng tạm thời -> đáng retry (không gồm 401/4xx/parse).
_RETRYABLE_EXC = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
)


class ISAPIError(Exception):
    """Lỗi chung khi gọi ISAPI."""


class ISAPIAuthError(ISAPIError):
    """Sai tài khoản/mật khẩu (HTTP 401)."""


def build_timeout(
    connect: float, read: float, write: float
) -> httpx.Timeout:
    """Tạo httpx.Timeout chi tiết (pool dùng chung connect)."""
    return httpx.Timeout(connect=connect, read=read, write=write, pool=connect)


def normalize_fingerprint(fp: str) -> str:
    """Chuẩn hóa fingerprint để so sánh: bỏ ':'/khoảng trắng, thường hóa."""
    return re.sub(r"[\s:]", "", fp).lower()


async def get_cert_fingerprint(host: str, port: int, timeout: float = 5.0) -> str:
    """Mở TLS tới host:port, trả SHA-256 fingerprint (hex) của cert máy chủ.

    Không xác thực CA (NVR thường dùng cert tự ký) — chỉ lấy cert để pin.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port, ssl=ctx), timeout=timeout
    )
    try:
        ssl_obj = writer.get_extra_info("ssl_object")
        der = ssl_obj.getpeercert(binary_form=True)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001 - đóng socket lỗi không quan trọng
            pass
    return hashlib.sha256(der).hexdigest()


def local_name(tag: str) -> str:
    """Trả về tên tag, bỏ phần namespace '{...}'."""
    return tag.rsplit("}", 1)[-1]


def _find_text(elem: ET.Element, name: str) -> str | None:
    """Tìm phần tử con đầu tiên có local-name == name (đệ quy), trả text."""
    for child in elem.iter():
        if local_name(child.tag) == name and child.text is not None:
            return child.text.strip()
    return None


def _child_text(elem: ET.Element, name: str) -> str | None:
    """Như _find_text nhưng CHỈ xét con trực tiếp (không đệ quy).

    Cần cho <hdd>: id/capacity... phải lấy đúng của ổ đó, không lẫn id của phần tử
    lồng bên trong (vd đĩa thành viên RAID) khiến trùng khóa.
    """
    for child in list(elem):
        if local_name(child.tag) == name and child.text is not None:
            return child.text.strip()
    return None


def _to_int(value: str | None) -> int | None:
    """Ép chuỗi số (có thể kèm đơn vị/khoảng trắng) về int, lỗi -> None."""
    if value is None:
        return None
    m = re.search(r"-?\d+", value)
    return int(m.group()) if m else None


@dataclass
class DeviceInfo:
    model: str | None = None
    serial: str | None = None
    firmware: str | None = None
    device_name: str | None = None


@dataclass
class ChannelInfo:
    channel_no: int
    name: str | None = None
    ip: str | None = None
    online: bool | None = None  # None = chưa rõ
    raw_status: str | None = None


@dataclass
class HddInfo:
    """Trạng thái 1 ổ cứng trong NVR (từ /ISAPI/ContentMgmt/Storage).

    Với NVR RAID, <hddList> gồm CẢ volume ghi ("Virtual Disk", property RW) LẪN các
    đĩa vật lý thành viên ("SATA", property RO) — id có thể trùng nhau giữa hai loại.
    `hdd_type` để phân biệt; dung lượng ghi chỉ tính trên volume RW (is_recording).
    """

    hdd_id: int
    name: str | None = None
    hdd_type: str | None = None  # "SATA" (vật lý) / "Virtual Disk" (volume RAID) / ...
    capacity_mb: int | None = None
    free_mb: int | None = None
    status: str | None = None  # ok / unformatted / error / sleeping / ...
    is_recording: bool | None = None  # property RW -> volume đang ghi; None = không rõ
    smart_health: str | None = None  # "good"/"bad"... (chỉ một số firmware)
    temperature_c: int | None = None  # nhiệt độ °C (chỉ một số firmware)


@dataclass
class StorageInfo:
    """Tổng hợp lưu trữ của 1 NVR: danh sách ổ + RAID + tổng bitrate ghi (nếu có)."""

    hdds: list[HddInfo] = field(default_factory=list)
    raid_status: str | None = None  # None = không có RAID / không hỗ trợ
    # Tổng bitrate main-stream của mọi camera (kbps) — để dự đoán số ngày lưu trữ.
    total_bitrate_kbps: int | None = None
    # Có ổ nào trả S.M.A.R.T không: True=có, False=đã dò nhưng firmware không hỗ trợ,
    # None=không dò lần này. Dùng để THÔI dò lần sau (đỡ ~16 request/NVR/vòng).
    smart_supported: bool | None = None


@dataclass
class ISAPIResult:
    """Kết quả gộp của một lượt gọi ISAPI cho 1 NVR."""

    device: DeviceInfo | None = None
    channels: list[ChannelInfo] = field(default_factory=list)
    response_time_ms: int | None = None


class ISAPIClient:
    """Bao đóng các lệnh ISAPI cho một NVR."""

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        port: int = 80,
        use_https: bool = False,
        timeout: int = 10,
        *,
        retries: int = 0,
        retry_backoff_base: float = 0.5,
    ) -> None:
        scheme = "https" if use_https else "http"
        self.base_url = f"{scheme}://{host}:{port}"
        self.timeout = timeout
        self.retries = retries
        self.retry_backoff_base = retry_backoff_base
        self._auth = httpx.DigestAuth(username, password)

    async def _get_xml(self, client: httpx.AsyncClient, path: str) -> ET.Element:
        """GET một endpoint ISAPI và parse XML thành Element gốc.

        Retry với exponential backoff + jitter cho lỗi mạng tạm thời
        (connect/read timeout). KHÔNG retry 401 (Auth Error) để giữ phân loại đúng.
        """
        for attempt in range(self.retries + 1):
            try:
                resp = await client.get(path, auth=self._auth)
                break
            except _RETRYABLE_EXC:
                if attempt >= self.retries:
                    raise
                delay = self.retry_backoff_base * (2**attempt) + random.uniform(
                    0, self.retry_backoff_base
                )
                await asyncio.sleep(delay)
        if resp.status_code == 401:
            raise ISAPIAuthError(f"401 Unauthorized tại {path}")
        resp.raise_for_status()
        try:
            return ET.fromstring(resp.text)
        except ET.ParseError as exc:  # noqa: PERF203
            raise ISAPIError(f"Không parse được XML từ {path}: {exc}") from exc

    async def get_device_info(self, client: httpx.AsyncClient) -> DeviceInfo:
        """GET /ISAPI/System/deviceInfo."""
        root = await self._get_xml(client, "/ISAPI/System/deviceInfo")
        return DeviceInfo(
            model=_find_text(root, "model"),
            serial=_find_text(root, "serialNumber"),
            firmware=_find_text(root, "firmwareVersion"),
            device_name=_find_text(root, "deviceName"),
        )

    async def get_channels(self, client: httpx.AsyncClient) -> list[ChannelInfo]:
        """Gộp danh sách kênh + trạng thái online/offline từng camera.

        - /ISAPI/ContentMgmt/InputProxy/channels        -> id, tên, ip
        - /ISAPI/ContentMgmt/InputProxy/channels/status -> online/offline
        """
        channels: dict[int, ChannelInfo] = {}

        list_root = await self._get_xml(
            client, "/ISAPI/ContentMgmt/InputProxy/channels"
        )
        for ch in list_root.iter():
            if local_name(ch.tag) != "InputProxyChannel":
                continue
            cid = _find_text(ch, "id")
            if cid is None:
                continue
            no = int(re.sub(r"\D", "", cid) or 0)
            channels[no] = ChannelInfo(
                channel_no=no,
                name=_find_text(ch, "name"),
                ip=_find_text(ch, "ipAddress") or _find_text(ch, "addressingFormatType"),
            )

        try:
            status_root = await self._get_xml(
                client, "/ISAPI/ContentMgmt/InputProxy/channels/status"
            )
        except ISAPIError:
            # Một số firmware không hỗ trợ endpoint status -> giữ danh sách, online=None
            return list(channels.values())

        for ch in status_root.iter():
            if local_name(ch.tag) != "InputProxyChannelStatus":
                continue
            cid = _find_text(ch, "id")
            if cid is None:
                continue
            no = int(re.sub(r"\D", "", cid) or 0)
            raw = _find_text(ch, "online")
            online = raw.lower() == "true" if raw is not None else None
            info = channels.setdefault(no, ChannelInfo(channel_no=no))
            info.online = online
            info.raw_status = raw

        return list(channels.values())

    async def get_storage_info(
        self, client: httpx.AsyncClient, *, probe_smart: bool = True
    ) -> StorageInfo:
        """Lấy trạng thái lưu trữ (HDD + RAID) của NVR.

        - /ISAPI/ContentMgmt/Storage -> <hddList><hdd>... và <raidList><raid>...
          Mỗi <hdd> có: id, hddName, capacity, freeSpace, status, property.
          `property` chứa "rw"/"R/W" -> ổ đang dùng để ghi hình.
        - S.M.A.R.T (health/nhiệt độ) ở endpoint phụ tùy firmware -> bọc try/except,
          không hỗ trợ thì để None. `probe_smart=False` để BỎ HẲN việc dò (caller biết
          NVR này không hỗ trợ) -> tiết kiệm ~1 request/ổ mỗi vòng quét.
        """
        root = await self._get_xml(client, "/ISAPI/ContentMgmt/Storage")

        hdds: list[HddInfo] = []
        # CHỈ lấy <hdd> là con trực tiếp của <hddList>, đọc field bằng _child_text để
        # không lẫn id/giá trị của phần tử lồng bên trong -> tránh trùng khóa.
        for lst in root.iter():
            if local_name(lst.tag) != "hddList":
                continue
            for hdd in list(lst):
                if local_name(hdd.tag) != "hdd":
                    continue
                hid = _child_text(hdd, "id")
                if hid is None:
                    continue
                status = _child_text(hdd, "status")
                # "notexist" = khay trống (chưa gắn ổ), capacity=0 -> bỏ qua, không
                # tính là ổ (tránh thổi phồng "số ổ" và bảng đầy dòng 0 GB).
                if (status or "").lower() == "notexist":
                    continue
                prop = _child_text(hdd, "property")
                is_recording = (
                    None
                    if not prop
                    else ("rw" in prop.lower() or "r/w" in prop.lower())
                )
                hdds.append(
                    HddInfo(
                        hdd_id=int(re.sub(r"\D", "", hid) or 0),
                        name=_child_text(hdd, "hddName"),
                        hdd_type=_child_text(hdd, "hddType"),
                        capacity_mb=_to_int(_child_text(hdd, "capacity")),
                        free_mb=_to_int(_child_text(hdd, "freeSpace")),
                        status=status,
                        is_recording=is_recording,
                    )
                )

        raid_status: str | None = None
        for raid in root.iter():
            if local_name(raid.tag) != "raid":
                continue
            # Lấy trạng thái RAID đầu tiên gặp được (thường chỉ 1 array).
            raid_status = _child_text(raid, "status") or _child_text(raid, "raidStatus")
            if raid_status:
                break

        # S.M.A.R.T best-effort: dò nếu chưa biết là không hỗ trợ.
        smart_supported: bool | None = None
        if probe_smart:
            smart_supported = await self._enrich_smart(client, hdds) > 0

        return StorageInfo(
            hdds=hdds, raid_status=raid_status, smart_supported=smart_supported
        )

    async def _enrich_smart(
        self, client: httpx.AsyncClient, hdds: list[HddInfo]
    ) -> int:
        """Bổ sung sức khỏe S.M.A.R.T + nhiệt độ cho từng ổ (best-effort).

        Endpoint S.M.A.R.T khác nhau theo firmware và nhiều máy không có -> mọi lỗi
        (404/parse/timeout) đều nuốt, để các trường smart_health/temperature_c = None.
        Trả số ổ đọc được S.M.A.R.T (0 = firmware không hỗ trợ).
        """
        n_ok = 0
        for hdd in hdds:
            # Volume ảo (RAID array) không có S.M.A.R.T -> bỏ qua.
            if (hdd.hdd_type or "").lower().startswith("virtual"):
                continue
            try:
                smart = await self._get_xml(
                    client, f"/ISAPI/ContentMgmt/Storage/hdd/{hdd.hdd_id}/Smart"
                )
            except (TimeoutError, ISAPIError, httpx.HTTPError, OSError):
                continue
            hdd.smart_health = _find_text(smart, "evaluation") or _find_text(
                smart, "selfEvaluation"
            )
            hdd.temperature_c = _to_int(_find_text(smart, "temperature"))
            n_ok += 1
        return n_ok

    async def get_record_bitrate_kbps(
        self, client: httpx.AsyncClient
    ) -> int | None:
        """Tổng bitrate ghi (kbps) = cộng bitrate main-stream của mọi camera.

        /ISAPI/Streaming/channels -> nhiều <StreamingChannel>; main stream có id kết
        thúc '01' (vd 101=kênh 1 main, 102=sub). Bitrate trong <Video>: ưu tiên
        constantBitRate (CBR), nếu không có/0 thì vbrUpperCap (VBR max — ước lượng
        thận trọng). Trả None nếu không đọc được kênh nào (firmware/endpoint khác).
        """
        root = await self._get_xml(client, "/ISAPI/Streaming/channels")
        total = 0
        found = False
        for ch in root.iter():
            if local_name(ch.tag) != "StreamingChannel":
                continue
            cid = _find_text(ch, "id") or _find_text(ch, "channelID")
            if cid is None or not cid.endswith("01"):
                continue  # chỉ tính main stream
            br = _to_int(_find_text(ch, "constantBitRate")) or _to_int(
                _find_text(ch, "vbrUpperCap")
            )
            if br:
                total += br
                found = True
        return total if found else None


async def probe_nvr(
    host: str,
    username: str,
    password: str,
    port: int = 80,
    use_https: bool = False,
    timeout: int = 10,
    *,
    verify: bool | str = False,
) -> ISAPIResult:
    """Tiện ích: mở 1 client, lấy device info + channels cho một NVR."""
    import time

    client_obj = ISAPIClient(host, username, password, port, use_https, timeout)
    async with httpx.AsyncClient(
        base_url=client_obj.base_url, timeout=timeout, verify=verify
    ) as client:
        start = time.perf_counter()
        device = await client_obj.get_device_info(client)
        channels = await client_obj.get_channels(client)
        elapsed_ms = int((time.perf_counter() - start) * 1000)
    return ISAPIResult(device=device, channels=channels, response_time_ms=elapsed_ms)
