"""
AngelOne SmartAPI broker adapter.

Everything the strategy needs from a broker lives behind this one class:
login, LTP, candles and order placement. Swapping brokers means writing
another class with the same four methods - the strategy itself is broker
agnostic.

Session handling: SmartAPI sessions are valid for the trading day, and
AngelOne rate-limits logins. We cache the session to disk so agent restarts
(or the dashboard restarting the service) reuse it instead of re-authenticating.
"""

import datetime
import json
import logging
import os
import time

import pyotp
from SmartApi import SmartConnect

from config.settings import CACHE_DIR, credentials

logger = logging.getLogger("broker")

SESSION_FILE = CACHE_DIR / "angel_session.json"

# AngelOne publishes the full contract master here (~35 MB).
SCRIP_MASTER_URL = (
    "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"
)

EXCHANGE_NFO = "NFO"
EXCHANGE_NSE = "NSE"


class BrokerError(RuntimeError):
    pass


class AngelOneBroker:
    def __init__(self, api_key=None, client_code=None, pin=None, totp_secret=None):
        self.api_key = api_key or credentials.angel_api_key
        self.client_code = client_code or credentials.angel_client_code
        self.pin = pin or credentials.angel_pin
        self.totp_secret = totp_secret or credentials.angel_totp_secret
        self.smart = None
        self.feed_token = None
        self.profile_name = None

    # ---------------------------------------------------------------- session

    def _load_cached_session(self):
        if not SESSION_FILE.exists():
            return None
        try:
            with open(SESSION_FILE) as f:
                data = json.load(f)
        except Exception:
            return None
        # Sessions do not survive the trading day.
        if data.get("date") != datetime.date.today().isoformat():
            return None
        if data.get("client_code") != self.client_code:
            return None
        return data

    def _save_session(self, data):
        data = dict(data)
        data["date"] = datetime.date.today().isoformat()
        data["client_code"] = self.client_code
        SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(SESSION_FILE, "w") as f:
            json.dump(data, f)
        try:
            os.chmod(SESSION_FILE, 0o600)
        except OSError:
            pass

    def connect(self, force=False):
        """Authenticate (or restore a cached session). Returns profile name."""
        missing = credentials.validate()
        if missing:
            raise BrokerError("Missing credentials in .env: " + ", ".join(missing))

        self.smart = SmartConnect(self.api_key)

        cached = None if force else self._load_cached_session()
        if cached:
            try:
                self.smart.setAccessToken(cached["jwt"])
                self.smart.setRefreshToken(cached["refresh"])
                self.smart.setUserId(self.client_code)
                self.feed_token = cached.get("feed")
                self.profile_name = cached.get("name")
                # Cheap call to prove the restored session is actually alive.
                self.ltp(EXCHANGE_NSE, "NIFTY", "26000")
                logger.info("Reused cached AngelOne session (%s)", self.profile_name)
                return self.profile_name
            except Exception as exc:
                logger.warning("Cached session rejected (%s) - logging in fresh", exc)

        try:
            otp = pyotp.TOTP(self.totp_secret).now()
        except Exception as exc:
            raise BrokerError("ANGEL_TOTP_SECRET is not a valid base32 TOTP secret") from exc

        data = self.smart.generateSession(self.client_code, self.pin, otp)
        if not data or not data.get("status"):
            raise BrokerError("AngelOne login failed: %s" % (data or {}).get("message"))

        jwt = data["data"]["jwtToken"]
        refresh = data["data"]["refreshToken"]
        self.feed_token = self.smart.getfeedToken()

        try:
            prof = self.smart.getProfile(refresh)
            self.profile_name = prof["data"].get("name")
        except Exception:
            self.profile_name = self.client_code

        self._save_session(
            {"jwt": jwt, "refresh": refresh, "feed": self.feed_token, "name": self.profile_name}
        )
        logger.info("AngelOne connected: %s", self.profile_name)
        return self.profile_name

    # ------------------------------------------------------------------ data

    def _retry(self, fn, what, attempts=3, pause=0.4):
        last = None
        for i in range(attempts):
            try:
                return fn()
            except Exception as exc:
                last = exc
                # AngelOne throttles aggressively; back off rather than hammer.
                time.sleep(pause * (i + 1))
        raise BrokerError("%s failed after %d attempts: %s" % (what, attempts, last))

    def ltp(self, exchange, tradingsymbol, symboltoken):
        """Last traded price as float."""

        def _call():
            res = self.smart.ltpData(exchange, tradingsymbol, str(symboltoken))
            if not res or not res.get("status"):
                raise BrokerError((res or {}).get("message", "no ltp"))
            return float(res["data"]["ltp"])

        return self._retry(_call, "ltp(%s)" % tradingsymbol)

    def candles(self, exchange, symboltoken, interval, from_dt, to_dt):
        """OHLC candles. from_dt/to_dt are datetimes. Returns list of rows."""

        def _call():
            params = {
                "exchange": exchange,
                "symboltoken": str(symboltoken),
                "interval": interval,
                "fromdate": from_dt.strftime("%Y-%m-%d %H:%M"),
                "todate": to_dt.strftime("%Y-%m-%d %H:%M"),
            }
            res = self.smart.getCandleData(params)
            if not res or not res.get("status"):
                raise BrokerError((res or {}).get("message", "no candles"))
            return res.get("data") or []

        return self._retry(_call, "candles(%s)" % symboltoken)

    # ---------------------------------------------------------------- orders

    def place_order(self, tradingsymbol, symboltoken, side, quantity,
                    product_type="CARRYFORWARD", order_type="MARKET",
                    price=0.0, exchange=EXCHANGE_NFO):
        """
        Place a real order. side is BUY or SELL.
        Returns the broker order id. Raises BrokerError on rejection.
        """
        params = {
            "variety": "NORMAL",
            "tradingsymbol": tradingsymbol,
            "symboltoken": str(symboltoken),
            "transactiontype": side.upper(),
            "exchange": exchange,
            "ordertype": order_type.upper(),
            "producttype": product_type.upper(),
            "duration": "DAY",
            "price": str(price if order_type.upper() == "LIMIT" else 0),
            "squareoff": "0",
            "stoploss": "0",
            "quantity": str(int(quantity)),
        }

        def _call():
            res = self.smart.placeOrderFullResponse(params)
            if not res or not res.get("status"):
                raise BrokerError((res or {}).get("message", "order rejected"))
            data = res.get("data") or {}
            return data.get("orderid") or data.get("orderId")

        order_id = self._retry(_call, "place_order(%s %s)" % (side, tradingsymbol), attempts=2)
        logger.info("ORDER %s %s x%s -> %s", side, tradingsymbol, quantity, order_id)
        return order_id

    def positions(self):
        try:
            res = self.smart.position()
            return (res or {}).get("data") or []
        except Exception as exc:
            logger.warning("positions() failed: %s", exc)
            return []
