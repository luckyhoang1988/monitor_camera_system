"""Client gọi Hikvision ISAPI (async, HTTP Digest auth) và parse XML.

Lưu ý (xem CLAUDE.md §4):
- ISAPI chạy trên port 80/443, dùng HTTP Digest auth (không phải Basic).
- Phản hồi là XML có namespace -> dùng helper local_name() bỏ qua namespace.
- HTTP 401 = sai tài khoản/mật khẩu (Auth Error), không phải offline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

import httpx


class ISAPIError(Exception):
    """Lỗi chung khi gọi ISAPI."""


class ISAPIAuthError(ISAPIError):
    """Sai tài khoản/mật khẩu (HTTP 401)."""


def local_name(tag: str) -> str:
    """Trả về tên tag, bỏ phần namespace '{...}'."""
    return tag.rsplit("}", 1)[-1]


def _find_text(elem: ET.Element, name: str) -> str | None:
    """Tìm phần tử con đầu tiên có local-name == name (đệ quy), trả text."""
    for child in elem.iter():
        if local_name(child.tag) == name and child.text is not None:
            return child.text.strip()
    return None


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
    ) -> None:
        scheme = "https" if use_https else "http"
        self.base_url = f"{scheme}://{host}:{port}"
        self.timeout = timeout
        self._auth = httpx.DigestAuth(username, password)

    async def _get_xml(self, client: httpx.AsyncClient, path: str) -> ET.Element:
        """GET một endpoint ISAPI và parse XML thành Element gốc."""
        resp = await client.get(path, auth=self._auth)
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


async def probe_nvr(
    host: str,
    username: str,
    password: str,
    port: int = 80,
    use_https: bool = False,
    timeout: int = 10,
) -> ISAPIResult:
    """Tiện ích: mở 1 client, lấy device info + channels cho một NVR."""
    import time

    client_obj = ISAPIClient(host, username, password, port, use_https, timeout)
    async with httpx.AsyncClient(
        base_url=client_obj.base_url, timeout=timeout, verify=False
    ) as client:
        start = time.perf_counter()
        device = await client_obj.get_device_info(client)
        channels = await client_obj.get_channels(client)
        elapsed_ms = int((time.perf_counter() - start) * 1000)
    return ISAPIResult(device=device, channels=channels, response_time_ms=elapsed_ms)
