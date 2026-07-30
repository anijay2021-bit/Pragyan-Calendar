"""
Pragyan Calendar - agent entry point.

Self-scheduling: run it once and it stays up, firing the calendar jobs at the
configured IST times on the right days. Schedule times come from settings.json,
so changing them in the dashboard reschedules on the next restart.

    python main.py            # run the scheduler
    python main.py --once     # fire entry immediately (manual/testing)
    python main.py --status   # print current state and exit
    python main.py --check    # connectivity + contract self-test, places no orders
"""

import argparse
import datetime
import json
import logging
import sys

import pytz
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from config.settings import LOG_DIR, credentials, load_settings
from brokers.angelone import AngelOneBroker
from strategies.calendar_spread import CalendarSpreadStrategy
from strategies.instruments import OptionChain

IST = pytz.timezone("Asia/Kolkata")

# Render log timestamps in IST even though the server clock is UTC.
logging.Formatter.converter = lambda *a: datetime.datetime.now(IST).timetuple()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s IST [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(str(LOG_DIR / "calendar.log")),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("main")


def _hhmm(text, fallback=(15, 20)):
    try:
        h, m = str(text).split(":")
        return int(h), int(m)
    except Exception:
        return fallback


def build():
    broker = AngelOneBroker()
    name = broker.connect()
    logger.info("Broker connected: %s", name)
    settings = load_settings()
    chain = OptionChain(settings["index"])
    return broker, CalendarSpreadStrategy(broker, chain)


def job_exit(strategy):
    """Runs before entry time on expiry days."""
    try:
        today = datetime.date.today()
        if not strategy.chain.is_expiry_day(today):
            logger.debug("Not an expiry day - exit job skipped.")
            return
        if strategy.chain.is_monthly_expiry_day(today):
            logger.info("Monthly expiry - closing entire calendar.")
            strategy.exit_all()
        else:
            logger.info("Weekly expiry - squaring off weekly short legs.")
            strategy.exit_weekly_legs()
    except Exception:
        logger.exception("exit job failed")


def job_entry(strategy):
    """Runs after the exit job on expiry days: roll, or open a new cycle."""
    try:
        today = datetime.date.today()
        if not strategy.chain.is_expiry_day(today):
            logger.debug("Not an expiry day - entry job skipped.")
            return
        if strategy.state["phase"] == "ACTIVE":
            logger.info("Rolling to next weekly.")
            strategy.roll_weekly()
        else:
            logger.info("Opening a new calendar cycle.")
            strategy.enter()
    except Exception:
        logger.exception("entry job failed")


def job_sl(strategy):
    try:
        strategy.check_sl()
    except Exception:
        logger.exception("SL check failed")


def self_check(strategy):
    """Prove data + contract selection work. Places no orders."""
    spot = strategy._spot()
    legs = strategy.chain.select_calendar_legs(spot)
    out = {
        "spot": spot,
        "atm_strike": legs["atm_strike"],
        "lot_size": legs["lot_size"],
        "weekly_expiry": str(legs["weekly_expiry"]),
        "monthly_expiry": str(legs["monthly_expiry"]),
        "SELL_weekly": [legs["sell_legs"]["CE"]["symbol"], legs["sell_legs"]["PE"]["symbol"]],
        "BUY_monthly": [legs["buy_legs"]["CE"]["symbol"], legs["buy_legs"]["PE"]["symbol"]],
        "mode": credentials.trading_mode,
    }
    print(json.dumps(out, indent=2))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="enter immediately, then exit")
    ap.add_argument("--status", action="store_true", help="print state and exit")
    ap.add_argument("--check", action="store_true", help="connectivity self-test, no orders")
    args = ap.parse_args()

    settings = load_settings()

    if args.status:
        from config.settings import STATE_FILE
        if STATE_FILE.exists():
            print(STATE_FILE.read_text())
        else:
            print(json.dumps({"phase": "NOT_STARTED"}, indent=2))
        return

    broker, strategy = build()

    if args.check:
        self_check(strategy)
        return
    if args.once:
        strategy.enter()
        return

    ex_h, ex_m = _hhmm(settings["weekly_exit_time"], (15, 18))
    en_h, en_m = _hhmm(settings["entry_time"], (15, 20))
    tf = int(settings["sl_check_interval_min"])
    open_h, open_m = _hhmm(settings["market_open"], (9, 20))
    close_h, _ = _hhmm(settings["market_close"], (15, 25))

    sched = BlockingScheduler(timezone=IST)
    sched.add_job(job_exit, CronTrigger(day_of_week="mon-fri", hour=ex_h, minute=ex_m),
                  args=[strategy], id="exit", name="Exit at %02d:%02d" % (ex_h, ex_m))
    sched.add_job(job_entry, CronTrigger(day_of_week="mon-fri", hour=en_h, minute=en_m),
                  args=[strategy], id="entry", name="Enter/Roll at %02d:%02d" % (en_h, en_m))
    sched.add_job(job_sl, CronTrigger(day_of_week="mon-fri",
                                      hour="%d-%d" % (open_h, close_h),
                                      minute="*/%d" % tf),
                  args=[strategy], id="sl", name="SL check every %d min" % tf)

    logger.info("=" * 60)
    logger.info("Pragyan Calendar | %s | mode=%s", settings["index"], credentials.trading_mode)
    logger.info("Exit  %02d:%02d | Entry %02d:%02d | SL every %d min",
                ex_h, ex_m, en_h, en_m, tf)
    logger.info("Lots=%s  SL=%s%%  product=%s",
                settings["lots"], settings["combined_sl_pct"], settings["product_type"])
    logger.info("=" * 60)

    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped.")


if __name__ == "__main__":
    main()
