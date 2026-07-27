"""
Pragyan Calendar - configuration.

Two layers:
  1. .env          -> secrets + trading mode (never committed)
  2. settings.json -> strategy parameters (safe to commit, editable from dashboard)

Strategy params are re-read from settings.json on every scheduled job, so edits
made in the dashboard take effect on the next cycle without a restart.
"""

import json
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT_DIR / ".env"
SETTINGS_FILE = ROOT_DIR / "settings.json"
STATE_FILE = ROOT_DIR / "state" / "calendar_state.json"
CACHE_DIR = ROOT_DIR / "cache"
LOG_DIR = ROOT_DIR / "logs"

load_dotenv(ENV_FILE)


def _get(key, default=""):
    return os.environ.get(key, default).strip()


class Credentials:
    """AngelOne SmartAPI credentials + Telegram. Read from .env only."""

    def __init__(self):
        self.angel_api_key = _get("ANGEL_API_KEY")
        self.angel_client_code = _get("ANGEL_CLIENT_CODE")
        self.angel_pin = _get("ANGEL_PIN")
        self.angel_totp_secret = _get("ANGEL_TOTP_SECRET")

        self.telegram_bot_token = _get("TELEGRAM_BOT_TOKEN")
        self.telegram_chat_id = _get("TELEGRAM_CHAT_ID")

        self.dash_user = _get("DASH_USER", "admin")
        self.dash_pass = _get("DASH_PASS")

    @property
    def trading_mode(self):
        return "LIVE" if _get("TRADING_MODE", "PAPER").upper() == "LIVE" else "PAPER"

    @property
    def is_paper(self):
        return self.trading_mode == "PAPER"

    def validate(self):
        """Return list of missing required credential names."""
        required = {
            "ANGEL_API_KEY": self.angel_api_key,
            "ANGEL_CLIENT_CODE": self.angel_client_code,
            "ANGEL_PIN": self.angel_pin,
            "ANGEL_TOTP_SECRET": self.angel_totp_secret,
        }
        return [k for k, v in required.items() if not v]


DEFAULT_SETTINGS = {
    "index": "NIFTY",
    "lots": 1,
    "product_type": "CARRYFORWARD",
    "order_type": "MARKET",
    "entry_time": "15:20",
    "weekly_exit_time": "15:18",
    "sl_check_interval_min": 5,
    "sl_check_mode": "candle_close",
    "combined_sl_pct": 30.0,
    "sl_enabled": True,
    "market_open": "09:20",
    "market_close": "15:25",
    "max_lots_guard": 20,
}

# Settings the dashboard is allowed to change.
EDITABLE = list(DEFAULT_SETTINGS.keys())


def load_settings():
    """Read settings.json, filling any missing key from DEFAULT_SETTINGS."""
    data = dict(DEFAULT_SETTINGS)
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE) as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                data.update(loaded)
        except Exception:
            pass
    # Hard clamp: a bad dashboard edit can never fire an oversized order.
    try:
        guard = int(data.get("max_lots_guard", 20))
        data["lots"] = max(1, min(int(data["lots"]), guard))
    except Exception:
        data["lots"] = 1
    return data


def save_settings(new):
    """Merge + persist settings.json. Only known keys are accepted."""
    data = load_settings()
    for k, v in (new or {}).items():
        if k in EDITABLE:
            data[k] = v
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SETTINGS_FILE, "w") as f:
        json.dump(data, f, indent=2)
    return load_settings()


def set_trading_mode(mode):
    """Rewrite TRADING_MODE in .env. Returns the mode actually written."""
    mode = "LIVE" if str(mode).upper() == "LIVE" else "PAPER"
    lines = ENV_FILE.read_text().splitlines() if ENV_FILE.exists() else []
    for i, line in enumerate(lines):
        if line.strip().startswith("TRADING_MODE"):
            lines[i] = "TRADING_MODE=" + mode
            break
    else:
        lines.append("TRADING_MODE=" + mode)
    ENV_FILE.write_text("\n".join(lines) + "\n")
    os.environ["TRADING_MODE"] = mode
    return mode


credentials = Credentials()

for _d in (STATE_FILE.parent, CACHE_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)
