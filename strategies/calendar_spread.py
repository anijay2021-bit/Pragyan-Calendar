"""
Calendar spread on an index straddle.

Structure
    SELL  near-dated (weekly)  ATM CE + PE
    BUY   far-dated  (monthly) ATM CE + PE

Lifecycle
    Entry            : sell weekly straddle, buy monthly straddle at the ATM strike.
    Weekly expiry day: buy back the sold weekly legs, then sell the next weekly
                       straddle. The bought monthly legs are left untouched - that
                       is the whole point of the calendar.
    Monthly expiry   : close everything (both the weekly short and the monthly
                       long), then re-enter a fresh cycle.

Risk
    A combined stop-loss on the short premium (CE + PE sold together). Breaching
    it squares off the short legs. Checked on candle close by default rather than
    tick-by-tick, to avoid being knocked out by a single spike.
"""

import datetime
import json
import logging

from config.settings import STATE_FILE, credentials, load_settings
from notify import send_telegram
from strategies.instruments import OptionChain

logger = logging.getLogger("calendar")


class Phase:
    NOT_STARTED = "NOT_STARTED"
    ACTIVE = "ACTIVE"
    CLOSED = "CLOSED"


class LegState:
    IDLE = "IDLE"
    SOLD = "SOLD"
    BOUGHT = "BOUGHT"
    EXITED = "EXITED"


def _today():
    return datetime.date.today()


class CalendarSpreadStrategy:
    def __init__(self, broker, chain=None):
        self.broker = broker
        self.settings = load_settings()
        self.chain = chain or OptionChain(self.settings["index"])
        self.state = self._empty_state()
        self._load_state()

    # ----------------------------------------------------------------- state

    def _empty_state(self):
        leg = lambda: {"symbol": None, "token": None, "entry_price": None,
                       "order_id": None, "state": LegState.IDLE}
        return {
            "phase": Phase.NOT_STARTED,
            "index": self.settings["index"],
            "atm_strike": None,
            "weekly_expiry": None,
            "monthly_expiry": None,
            "lots": None,
            "lot_size": None,
            "sell_legs": {"CE": leg(), "PE": leg()},
            "buy_legs": {"CE": leg(), "PE": leg()},
            "combined_entry_price": None,
            "combined_sl": None,
            "mode": credentials.trading_mode,
            "last_updated": None,
        }

    def _save_state(self):
        self.state["last_updated"] = datetime.datetime.now().isoformat()
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump(self.state, f, indent=2, default=str)

    def _load_state(self):
        if not STATE_FILE.exists():
            logger.info("No state file - starting fresh.")
            return
        try:
            with open(STATE_FILE) as f:
                saved = json.load(f)
        except Exception as exc:
            logger.warning("State unreadable (%s) - starting fresh.", exc)
            return
        monthly = saved.get("monthly_expiry")
        # Only resume if the far leg has not yet expired.
        if monthly and datetime.date.fromisoformat(str(monthly)) >= _today():
            self.state = saved
            logger.info("Resumed state - phase=%s monthly=%s", saved.get("phase"), monthly)
        else:
            logger.info("Saved state has expired - starting fresh.")

    # ---------------------------------------------------------------- helpers

    def refresh_settings(self):
        """Re-read settings.json so dashboard edits apply on the next cycle."""
        self.settings = load_settings()
        return self.settings

    @property
    def qty(self):
        lot_size = self.state.get("lot_size") or self.chain.lot_size
        return int(lot_size) * int(self.settings["lots"])

    def _spot(self):
        ref = self.chain.spot_ref()
        return self.broker.ltp(ref["exchange"], ref["symbol"], ref["token"])

    def _ltp(self, contract):
        return self.broker.ltp("NFO", contract["symbol"], contract["token"])

    def _order(self, contract, side, price=0.0):
        """Route an order. In PAPER mode nothing reaches the broker."""
        qty = self.qty
        if credentials.is_paper:
            oid = "PAPER-%s-%s-%s" % (
                side, contract["symbol"][-10:], datetime.datetime.now().strftime("%H%M%S")
            )
            logger.info("[PAPER] %s %s x%d @ %.2f", side, contract["symbol"], qty, price)
            return oid
        return self.broker.place_order(
            tradingsymbol=contract["symbol"],
            symboltoken=contract["token"],
            side=side,
            quantity=qty,
            product_type=self.settings["product_type"],
            order_type=self.settings["order_type"],
            price=price,
        )

    def _notify(self, text):
        try:
            send_telegram(text)
        except Exception as exc:
            logger.warning("Telegram failed: %s", exc)

    # ------------------------------------------------------------------ entry

    def enter(self):
        """Open a fresh calendar: sell weekly straddle, buy monthly straddle."""
        self.refresh_settings()
        if self.state["phase"] == Phase.ACTIVE:
            logger.info("Already ACTIVE - entry skipped.")
            return False

        spot = self._spot()
        legs = self.chain.select_calendar_legs(spot)
        qty = int(legs["lot_size"]) * int(self.settings["lots"])

        logger.info(
            "=== ENTRY === spot=%.2f atm=%d weekly=%s monthly=%s qty=%d mode=%s",
            spot, legs["atm_strike"], legs["weekly_expiry"], legs["monthly_expiry"],
            qty, credentials.trading_mode,
        )

        self.state = self._empty_state()
        self.state.update({
            "atm_strike": legs["atm_strike"],
            "weekly_expiry": legs["weekly_expiry"].isoformat(),
            "monthly_expiry": legs["monthly_expiry"].isoformat(),
            "lots": int(self.settings["lots"]),
            "lot_size": int(legs["lot_size"]),
            "mode": credentials.trading_mode,
        })

        # 1. BUY the far-dated (monthly) straddle first - long leg on before short.
        for opt in ("CE", "PE"):
            c = legs["buy_legs"][opt]
            price = self._ltp(c)
            oid = self._order(c, "BUY", price)
            self.state["buy_legs"][opt] = {
                "symbol": c["symbol"], "token": c["token"], "entry_price": price,
                "order_id": oid, "state": LegState.BOUGHT,
            }
            logger.info("BUY  monthly %s %s @ %.2f", opt, c["symbol"], price)

        # 2. SELL the near-dated (weekly) straddle.
        sold_total = 0.0
        for opt in ("CE", "PE"):
            c = legs["sell_legs"][opt]
            price = self._ltp(c)
            oid = self._order(c, "SELL", price)
            self.state["sell_legs"][opt] = {
                "symbol": c["symbol"], "token": c["token"], "entry_price": price,
                "order_id": oid, "state": LegState.SOLD,
            }
            sold_total += price
            logger.info("SELL weekly  %s %s @ %.2f", opt, c["symbol"], price)

        combined = round(sold_total, 2)
        sl = round(combined * (1 + float(self.settings["combined_sl_pct"]) / 100.0), 2)
        self.state["combined_entry_price"] = combined
        self.state["combined_sl"] = sl
        self.state["phase"] = Phase.ACTIVE
        self._save_state()

        self._notify(
            "*Calendar Spread ENTERED*\n"
            "Index: %s   Strike: %d\n"
            "SELL weekly  %s: %s + %s\n"
            "BUY  monthly %s: %s + %s\n"
            "Short premium: %.2f   SL: %.2f\n"
            "Qty: %d (%d lots)   Mode: %s"
            % (self.state["index"], legs["atm_strike"],
               legs["weekly_expiry"], legs["sell_legs"]["CE"]["symbol"],
               legs["sell_legs"]["PE"]["symbol"],
               legs["monthly_expiry"], legs["buy_legs"]["CE"]["symbol"],
               legs["buy_legs"]["PE"]["symbol"],
               combined, sl, qty, int(self.settings["lots"]), credentials.trading_mode)
        )
        return True

    # ------------------------------------------------------- weekly roll/exit

    def exit_weekly_legs(self):
        """Buy back the sold weekly legs. Monthly longs stay open."""
        if self.state["phase"] != Phase.ACTIVE:
            return False
        closed = []
        for opt in ("CE", "PE"):
            leg = self.state["sell_legs"][opt]
            if leg["state"] != LegState.SOLD:
                continue
            c = {"symbol": leg["symbol"], "token": leg["token"]}
            price = self._ltp(c)
            self._order(c, "BUY", price)
            leg["state"] = LegState.EXITED
            leg["exit_price"] = price
            closed.append("%s @ %.2f" % (opt, price))
            logger.info("Covered weekly %s %s @ %.2f", opt, leg["symbol"], price)
        self._save_state()
        if closed:
            self._notify("*Weekly legs squared off*\n" + "\n".join(closed))
        return bool(closed)

    def roll_weekly(self):
        """Sell the next weekly straddle at the current ATM."""
        self.refresh_settings()
        if self.state["phase"] != Phase.ACTIVE:
            return False

        spot = self._spot()
        # Next expiry strictly after the one that just died.
        prev = datetime.date.fromisoformat(str(self.state["weekly_expiry"]))
        new_expiry = self.chain.weekly_expiry(on_or_after=prev + datetime.timedelta(days=1))
        atm = self.chain.atm_strike(spot)
        legs = self.chain.straddle(new_expiry, atm)

        sold_total = 0.0
        for opt in ("CE", "PE"):
            c = legs[opt]
            price = self._ltp(c)
            oid = self._order(c, "SELL", price)
            self.state["sell_legs"][opt] = {
                "symbol": c["symbol"], "token": c["token"], "entry_price": price,
                "order_id": oid, "state": LegState.SOLD,
            }
            sold_total += price
            logger.info("SELL weekly %s %s @ %.2f", opt, c["symbol"], price)

        combined = round(sold_total, 2)
        self.state["weekly_expiry"] = new_expiry.isoformat()
        self.state["atm_strike"] = atm
        self.state["combined_entry_price"] = combined
        self.state["combined_sl"] = round(
            combined * (1 + float(self.settings["combined_sl_pct"]) / 100.0), 2
        )
        self._save_state()

        self._notify(
            "*Weekly Roll*\nNew expiry: %s   Strike: %d\n"
            "SELL %s + %s\nShort premium: %.2f   SL: %.2f"
            % (new_expiry, atm, legs["CE"]["symbol"], legs["PE"]["symbol"],
               combined, self.state["combined_sl"])
        )
        return True

    def exit_all(self):
        """Close every open leg - run on monthly expiry."""
        if self.state["phase"] != Phase.ACTIVE:
            return False
        done = []
        for book, closing_side in (("sell_legs", "BUY"), ("buy_legs", "SELL")):
            for opt in ("CE", "PE"):
                leg = self.state[book][opt]
                if leg["state"] not in (LegState.SOLD, LegState.BOUGHT):
                    continue
                c = {"symbol": leg["symbol"], "token": leg["token"]}
                price = self._ltp(c)
                self._order(c, closing_side, price)
                leg["state"] = LegState.EXITED
                leg["exit_price"] = price
                done.append("%s %s @ %.2f" % (closing_side, leg["symbol"], price))
        self.state["phase"] = Phase.CLOSED
        self._save_state()
        self._notify("*Calendar CLOSED (monthly expiry)*\n" + "\n".join(done))
        return True

    # -------------------------------------------------------------- stop loss

    def check_sl(self):
        """Combined stop-loss on the short premium."""
        self.refresh_settings()
        if self.state["phase"] != Phase.ACTIVE or not self.settings.get("sl_enabled", True):
            return False
        sl = self.state.get("combined_sl")
        if not sl:
            return False

        total = 0.0
        for opt in ("CE", "PE"):
            leg = self.state["sell_legs"][opt]
            if leg["state"] != LegState.SOLD:
                return False
            total += self._get_sl_price(leg)

        total = round(total, 2)
        logger.debug("SL check: short premium %.2f vs SL %.2f", total, sl)
        if total < sl:
            return False

        logger.warning("SL HIT: short premium %.2f >= %.2f", total, sl)
        self._notify("*STOP LOSS HIT*\nShort premium %.2f >= SL %.2f\nSquaring off short legs."
                     % (total, sl))
        self.exit_weekly_legs()
        return True

    def _get_sl_price(self, leg):
        """Candle-close price by default; last trade price if configured."""
        c = {"symbol": leg["symbol"], "token": leg["token"]}
        if self.settings.get("sl_check_mode") != "candle_close":
            return self._ltp(c)
        tf = int(self.settings["sl_check_interval_min"])
        interval = {1: "ONE_MINUTE", 3: "THREE_MINUTE", 5: "FIVE_MINUTE",
                    10: "TEN_MINUTE", 15: "FIFTEEN_MINUTE"}.get(tf, "FIVE_MINUTE")
        now = datetime.datetime.now()
        try:
            rows = self.broker.candles(
                "NFO", leg["token"], interval, now - datetime.timedelta(minutes=tf * 4), now
            )
            if rows:
                return float(rows[-1][4])   # close of the last completed candle
        except Exception as exc:
            logger.warning("Candle fetch failed (%s) - falling back to LTP", exc)
        return self._ltp(c)

    # ---------------------------------------------------------------- status

    def status(self):
        s = dict(self.state)
        s["settings"] = self.settings
        s["mode"] = credentials.trading_mode
        return s
