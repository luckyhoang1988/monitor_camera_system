"""Event bus trong tiến trình (asyncio pub/sub) cho cập nhật realtime qua SSE.

Mô hình tương đương "Grafana Live": collector phát một sự kiện *nhẹ* (chỉ tên loại +
id) mỗi khi trạng thái thật sự thay đổi; endpoint SSE (`/events`) đẩy xuống browser để
HTMX re-fetch đúng partial cũ. Bus KHÔNG mang dữ liệu hiển thị — chỉ là tín hiệu
"có gì đó đổi, hãy tải lại".

RÀNG BUỘC: bus này nằm trong MỘT tiến trình -> chỉ broadcast giữa các kết nối SSE của
cùng một worker uvicorn. Với quy mô ~45 NVR/720 camera, chạy 1 worker là đủ. Nếu sau này
scale nhiều worker/replica, thay bằng broker chia sẻ (Redis pub/sub).
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("chek_nvr.events")

# Các loại sự kiện phát ra (khớp tên dùng ở template: hx-trigger="sse:<type>").
EVENT_NVR_CHANGE = "nvr-change"
EVENT_CAMERA_CHANGE = "camera-change"
EVENT_STORAGE_CHANGE = "storage-change"
EVENT_ALERT = "alert"


class EventBus:
    """Quản lý tập subscriber (mỗi kết nối SSE = 1 asyncio.Queue) và broadcast event."""

    def __init__(self, max_queue: int = 100) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self._max_queue = max_queue

    def subscribe(self) -> asyncio.Queue:
        """Đăng ký một queue mới cho 1 client SSE. Nhớ gọi `unsubscribe` khi client rời."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=self._max_queue)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        """Gỡ queue khi client SSE ngắt kết nối (an toàn nếu đã gỡ)."""
        self._subscribers.discard(queue)

    def publish(self, event_type: str, data: dict | None = None) -> None:
        """Đẩy event (non-blocking) tới mọi subscriber.

        Queue đầy (client chậm/treo) -> bỏ qua event đó cho client ấy thay vì chặn
        collector. Client vẫn tự đồng bộ lại nhờ polling fallback.
        """
        event = {"type": event_type, "data": data or {}}
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning(
                    "SSE queue đầy — bỏ qua event %s cho 1 client chậm", event_type
                )

    @property
    def subscriber_count(self) -> int:
        """Số kết nối SSE đang mở (phục vụ debug/giám sát)."""
        return len(self._subscribers)


# Singleton dùng chung toàn app (collector publish, route /events subscribe).
event_bus = EventBus()
