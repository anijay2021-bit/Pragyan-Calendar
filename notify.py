"""Self-contained Telegram notifier. Silently no-ops if unconfigured."""

import logging
import urllib.parse
import urllib.request

from config.settings import credentials

logger = logging.getLogger("notify")
API = "https://api.telegram.org/bot%s/sendMessage"


def send_telegram(text, parse_mode="Markdown"):
    token = credentials.telegram_bot_token
    chat_id = credentials.telegram_chat_id
    if not token or not chat_id:
        logger.debug("Telegram not configured - skipping notification.")
        return False
    payload = urllib.parse.urlencode(
        {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    ).encode()
    try:
        with urllib.request.urlopen(API % token, data=payload, timeout=15) as r:
            return r.status == 200
    except Exception as exc:
        logger.warning("Telegram send failed: %s", exc)
        return False
