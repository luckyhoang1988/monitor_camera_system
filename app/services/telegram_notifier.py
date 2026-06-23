"""Gửi cảnh báo qua Telegram Bot (kênh thông báo ngoài, tùy chọn).

Mô hình: `alert_service` chỉ *xếp hàng* thông báo lên `session.info` ngay khi
tạo alert mới (xem `queue_alert`). Sau khi commit thành công, call site gọi
`flush_telegram_notifications(session)` để gửi thật. Cách này tránh gửi nhầm
khi transaction bị rollback, và việc gửi là best-effort: lỗi mạng/Telegram chỉ
ghi log, không làm hỏng luồng quét.
"""

from __future__ import annotations

import logging

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings

logger = logging.getLogger("chek_nvr.telegram")

_QUEUE_KEY = "telegram_queue"
_API_URL = "https://api.telegram.org/bot{token}/sendMessage"

# Biểu tượng theo mức độ alert (AlertSeverity.value).
_EMOJI = {"info": "✅", "warning": "⚠️", "critical": "🚨"}


def queue_alert(session: AsyncSession, *, severity: str, message: str) -> None:
    """Xếp một thông báo alert vào hàng đợi của session (no-op nếu Telegram tắt)."""
    if not get_settings().telegram_enabled:
        return
    text = f"{_EMOJI.get(severity, 'ℹ️')} <b>Chek_NVR</b>\n{message}"
    session.info.setdefault(_QUEUE_KEY, []).append(text)


async def send_telegram_message(text: str) -> bool:
    """Gửi 1 tin nhắn lên Telegram. Trả về True nếu gửi thành công."""
    settings = get_settings()
    if not (
        settings.telegram_enabled
        and settings.telegram_bot_token
        and settings.telegram_chat_id
    ):
        return False
    url = _API_URL.format(token=settings.telegram_bot_token)
    payload = {
        "chat_id": settings.telegram_chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload)
        if resp.status_code != 200:
            logger.warning(
                "Telegram trả về %s: %s", resp.status_code, resp.text[:300]
            )
            return False
        return True
    except Exception:  # noqa: BLE001 - gửi Telegram lỗi không được làm hỏng quy trình
        logger.exception("Lỗi khi gửi cảnh báo Telegram")
        return False


async def flush_telegram_notifications(session: AsyncSession) -> None:
    """Gửi và xóa toàn bộ thông báo đã xếp hàng trên session (gọi sau commit)."""
    queue = session.info.pop(_QUEUE_KEY, None)
    if not queue:
        return
    for text in queue:
        await send_telegram_message(text)
