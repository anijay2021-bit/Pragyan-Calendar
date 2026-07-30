"""
Contract selection driven by AngelOne's scrip master.

Why we parse the master instead of building symbols by hand: NSE has changed
both the weekly expiry weekday and the NIFTY lot size more than once. Anything
that hardcodes "Thursday" or "lot size 75" silently breaks the day it changes.
Here expiries, strikes and lot sizes all come from the broker's own contract
file, so the strategy follows whatever the exchange is actually doing.
"""

import datetime
import json
import logging
import os
import urllib.request

from config.settings import CACHE_DIR

logger = logging.getLogger("instruments")

SCRIP_MASTER_URL = (
    "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"
)
MASTER_FILE = CACHE_DIR / "scrip_master.json"
MASTER_MAX_AGE_H = 12

# Spot index tokens on the NSE segment.
INDEX_SPOT = {
    "NIFTY": {"symbol": "NIFTY", "token": "26000", "exchange": "NSE"},
    "BANKNIFTY": {"symbol": "BANKNIFTY", "token": "26009", "exchange": "NSE"},
}

_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def _parse_expiry(text):
    """'11AUG2026' -> date(2026, 8, 11). Returns None if unparseable."""
    text = (text or "").strip().upper()
    if len(text) < 9:
        return None
    try:
        return datetime.date(int(text[5:9]), _MONTHS[text[2:5]], int(text[0:2]))
    except (KeyError, ValueError):
        return None


def download_master(force=False):
    """Download the scrip master, cached on disk."""
    fresh = False
    if MASTER_FILE.exists() and not force:
        age_h = (
            datetime.datetime.now()
            - datetime.datetime.fromtimestamp(os.path.getmtime(MASTER_FILE))
        ).total_seconds() / 3600.0
        fresh = age_h < MASTER_MAX_AGE_H
    if not fresh:
        logger.info("Downloading AngelOne scrip master ...")
        MASTER_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(MASTER_FILE) + ".tmp"
        with urllib.request.urlopen(SCRIP_MASTER_URL, timeout=180) as r:
            with open(tmp, "wb") as f:
                f.write(r.read())
        os.replace(tmp, MASTER_FILE)
        logger.info("Scrip master saved (%.1f MB)", os.path.getsize(MASTER_FILE) / 1e6)
    with open(MASTER_FILE) as f:
        return json.load(f)


class OptionChain:
    """Index option contracts for one underlying, indexed for fast lookup."""

    def __init__(self, index="NIFTY", force_refresh=False):
        self.index = index.upper()
        if self.index not in INDEX_SPOT:
            raise ValueError("Unsupported index: %s" % index)
        rows = download_master(force=force_refresh)

        self.contracts = {}   # (expiry_date, strike_int, 'CE'|'PE') -> contract
        self.expiries = set()
        self.lot_size = None
        strikes = set()

        for row in rows:
            if row.get("exch_seg") != "NFO" or row.get("name") != self.index:
                continue
            sym = (row.get("symbol") or "").upper()
            if not sym.endswith(("CE", "PE")):
                continue
            expiry = _parse_expiry(row.get("expiry"))
            if not expiry:
                continue
            try:
                # Strike is quoted in paise in the master file.
                strike = int(round(float(row["strike"]) / 100.0))
                lot = int(row["lotsize"])
            except (KeyError, ValueError, TypeError):
                continue
            if strike <= 0:
                continue
            opt = sym[-2:]
            self.contracts[(expiry, strike, opt)] = {
                "symbol": row["symbol"],
                "token": str(row["token"]),
                "strike": strike,
                "expiry": expiry,
                "lot_size": lot,
                "option_type": opt,
            }
            self.expiries.add(expiry)
            strikes.add(strike)
            self.lot_size = lot

        if not self.contracts:
            raise RuntimeError("No %s option contracts found in scrip master" % self.index)

        self.expiries = sorted(self.expiries)
        self.strike_step = self._infer_step(sorted(strikes))
        logger.info(
            "%s chain: %d contracts, lot=%d, step=%d, next expiries=%s",
            self.index, len(self.contracts), self.lot_size, self.strike_step,
            [e.isoformat() for e in self.expiries[:4]],
        )

    @staticmethod
    def _infer_step(strikes):
        """Smallest positive gap between consecutive strikes."""
        gaps = {b - a for a, b in zip(strikes, strikes[1:]) if b > a}
        return min(gaps) if gaps else 50

    # ------------------------------------------------------------- expiries

    def weekly_expiry(self, on_or_after=None):
        """Nearest expiry on/after the given date (defaults to today)."""
        ref = on_or_after or datetime.date.today()
        for e in self.expiries:
            if e >= ref:
                return e
        raise RuntimeError("No expiry on/after %s" % ref)

    def monthly_expiry(self, after=None):
        """
        Next monthly expiry strictly after `after`.

        A monthly expiry is the last expiry within its calendar month - derived
        from the data, not assumed to be the last Thursday.
        """
        ref = after or datetime.date.today()
        last_of_month = {}
        for e in self.expiries:
            key = (e.year, e.month)
            if key not in last_of_month or e > last_of_month[key]:
                last_of_month[key] = e
        for e in sorted(last_of_month.values()):
            if e > ref:
                return e
        raise RuntimeError("No monthly expiry after %s" % ref)

    def is_expiry_day(self, day=None):
        return (day or datetime.date.today()) in set(self.expiries)

    def is_monthly_expiry_day(self, day=None):
        day = day or datetime.date.today()
        if not self.is_expiry_day(day):
            return False
        same_month = [e for e in self.expiries if (e.year, e.month) == (day.year, day.month)]
        return bool(same_month) and day == max(same_month)

    # --------------------------------------------------------------- strikes

    def atm_strike(self, spot):
        """Nearest listed strike to spot."""
        step = self.strike_step
        approx = int(round(float(spot) / step) * step)
        available = {k[1] for k in self.contracts}
        if approx in available:
            return approx
        return min(available, key=lambda s: (abs(s - approx), s))

    def get(self, expiry, strike, option_type):
        c = self.contracts.get((expiry, int(strike), option_type.upper()))
        if not c:
            raise RuntimeError(
                "Contract not listed: %s %s %s %s" % (self.index, expiry, strike, option_type)
            )
        return c

    def straddle(self, expiry, strike):
        """Both legs at one strike/expiry."""
        return {"CE": self.get(expiry, strike, "CE"), "PE": self.get(expiry, strike, "PE")}

    def spot_ref(self):
        return INDEX_SPOT[self.index]

    def select_calendar_legs(self, spot):
        """
        The calendar spread:
          SELL the near (weekly) straddle, BUY the far (monthly) straddle.
        """
        # A new cycle must never sell a contract expiring today. On expiry day
        # the position is squared off first, then the near leg rolls forward to
        # the next weekly - so the floor is tomorrow, not today.
        tomorrow = datetime.date.today() + datetime.timedelta(days=1)
        weekly = self.weekly_expiry(on_or_after=tomorrow)
        monthly = self.monthly_expiry(after=weekly)
        atm = self.atm_strike(spot)
        return {
            "atm_strike": atm,
            "spot_at_entry": float(spot),
            "weekly_expiry": weekly,
            "monthly_expiry": monthly,
            "sell_legs": self.straddle(weekly, atm),   # near-dated -> sold
            "buy_legs": self.straddle(monthly, atm),   # far-dated  -> bought
            "lot_size": self.lot_size,
        }
