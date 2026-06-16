"""Pool `httpx.AsyncClient` dùng chung, cache theo base_url.

Trước đây mỗi lần kiểm tra NVR tạo mới một `AsyncClient` -> mất connection
pooling, tăng latency/CPU khi số NVR lớn. Module này giữ một client theo từng
`base_url` (mỗi NVR/host) để tái sử dụng kết nối qua nhiều lượt quét.

Vòng đời: khởi tạo lười (lazy) khi cần, đóng toàn bộ ở `lifespan` shutdown của
FastAPI (và cuối script CLI vì CLI không có lifespan).
"""

from __future__ import annotations

import asyncio

import httpx

from app.collector.isapi_client import build_timeout
from app.config import get_settings

_clients: dict[str, httpx.AsyncClient] = {}
_lock = asyncio.Lock()


def _new_client(base_url: str) -> httpx.AsyncClient:
    settings = get_settings()
    return httpx.AsyncClient(
        base_url=base_url,
        timeout=build_timeout(
            settings.connect_timeout, settings.read_timeout, settings.write_timeout
        ),
        verify=settings.nvr_verify,
    )


async def get_client(base_url: str) -> httpx.AsyncClient:
    """Trả về client dùng chung cho `base_url` (tạo lười nếu chưa có)."""
    client = _clients.get(base_url)
    if client is not None and not client.is_closed:
        return client
    async with _lock:
        client = _clients.get(base_url)
        if client is None or client.is_closed:
            client = _new_client(base_url)
            _clients[base_url] = client
        return client


async def close_all() -> None:
    """Đóng toàn bộ client trong pool (gọi khi app/CLI shutdown)."""
    async with _lock:
        for client in _clients.values():
            await client.aclose()
        _clients.clear()
