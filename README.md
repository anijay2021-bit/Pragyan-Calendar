# Pragyan Calendar

An index **calendar spread** trading agent for **AngelOne (SmartAPI)**, built to run
unattended on a small Linux box (EC2, or anything with Python 3.10+).

AngelOne is used for *everything* - market data, contracts and order execution.
There is no second broker and no external data feed to configure.

---

## The strategy

At entry, on the ATM strike:

| Leg | Expiry | Action |
|-----|--------|--------|
| CE + PE | **near / weekly** | **SELL** |
| CE + PE | **far / monthly**  | **BUY**  |

Then:

- **Weekly expiry day** - buy back the sold weekly legs, and sell the *next*
  weekly straddle. The bought monthly legs are deliberately left alone; that
  long far-dated position is the point of the calendar.
- **Monthly expiry day** - close everything, then open a fresh cycle.
- **Stop loss** - a combined SL on the short premium (CE + PE sold together).
  Breaching it squares off the short legs only. Evaluated on candle close by
  default rather than tick-by-tick, so one spike does not knock you out.

### Expiries are read from the broker, not hardcoded

NSE has changed both the weekly expiry weekday and the NIFTY lot size more than
once. Anything that hardcodes "last Thursday" or "lot size 75" breaks silently
the day the exchange changes it. This agent derives expiries, strikes and lot
sizes from AngelOne's own scrip master, so it follows whatever is actually
listed. A monthly expiry is simply "the last expiry in that calendar month".

---

## Install (Ubuntu / EC2)

```bash
git clone https://github.com/anijay2021-bit/Pragyan-Calendar.git
cd Pragyan-Calendar
bash install.sh
```

The installer creates a virtualenv, installs dependencies, writes `.env`, and
registers two systemd services.

Then:

```bash
nano .env                          # credentials (see below)
venv/bin/python main.py --check    # self-test: places NO orders
sudo systemctl enable --now pragyan-calendar pragyan-dashboard
```

### EC2 notes

- Ubuntu 22.04/24.04, `t3.micro` is enough.
- Open **8080** in the security group for the dashboard, ideally restricted to
  your own IP. Port 8080 is the only inbound port needed.
- Set the clock to IST or leave it on UTC - schedule times are IST-aware
  regardless of the server timezone.

---

## Configuration

### `.env` - secrets (never committed)

| Key | Meaning |
|-----|---------|
| `ANGEL_API_KEY` | SmartAPI key from the AngelOne developer portal |
| `ANGEL_CLIENT_CODE` | Your AngelOne login / client code |
| `ANGEL_PIN` | Your login PIN |
| `ANGEL_TOTP_SECRET` | The **base32 secret** behind the TOTP QR, not the 6-digit code |
| `TRADING_MODE` | `PAPER` or `LIVE` |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | Optional alerts |
| `DASH_USER` / `DASH_PASS` | Dashboard login. Without `DASH_PASS` the dashboard refuses to serve. |

### `settings.json` - strategy parameters

Editable by hand or from the dashboard's Settings tab. The strategy re-reads
this file **every cycle**, so parameter changes take effect without a restart
(changing the schedule *times* needs a restart so the jobs get rescheduled).

| Setting | Default | Meaning |
|---------|---------|---------|
| `index` | `NIFTY` | `NIFTY` or `BANKNIFTY` |
| `lots` | `1` | Lots per leg (all four legs) |
| `combined_sl_pct` | `30.0` | SL as % above the combined short premium |
| `sl_enabled` | `true` | Master switch for the stop loss |
| `sl_check_interval_min` | `5` | Candle timeframe / check frequency |
| `sl_check_mode` | `candle_close` | `candle_close` or `ltp` |
| `entry_time` | `15:20` | Entry / roll time, IST |
| `weekly_exit_time` | `15:18` | Weekly squareoff time, IST |
| `product_type` | `CARRYFORWARD` | `CARRYFORWARD` or `INTRADAY` |
| `order_type` | `MARKET` | `MARKET` or `LIMIT` |
| `max_lots_guard` | `20` | Hard clamp - `lots` can never exceed this |

---

## PAPER / LIVE

Ships in **PAPER** mode: every order is simulated and logged, nothing reaches
the broker. Flip it either by editing `TRADING_MODE` in `.env`, or with the
dashboard toggle (which asks for confirmation and restarts the agent).

Going LIVE is refused outright if any AngelOne credential is missing.

---

## Dashboard

`http://<server-ip>:8080` - HTTP basic auth.

- Agent status, phase and current legs
- Start / stop / restart
- PAPER &harr; LIVE toggle with confirmation
- Settings tab wired to the real `settings.json`
- Live log tail

---

## CLI

```bash
venv/bin/python main.py            # run the scheduler (what systemd does)
venv/bin/python main.py --check    # connectivity + contract self-test, no orders
venv/bin/python main.py --status   # print saved state
venv/bin/python main.py --once     # force an entry now (respects PAPER/LIVE)
```

Logs: `logs/calendar.log`, or `journalctl -u pragyan-calendar -f`.

---

## Layout

```
main.py                     scheduler + CLI
notify.py                   Telegram
config/settings.py          .env + settings.json loader
brokers/angelone.py         SmartAPI adapter (login, LTP, candles, orders)
strategies/instruments.py   scrip master -> expiries, strikes, lot size
strategies/calendar_spread.py   the strategy
dashboard/                  FastAPI app + single-page UI
systemd/                    service unit templates
```

Swapping brokers means writing one class with `ltp`, `candles` and
`place_order`; the strategy never talks to a broker SDK directly.

---

## Disclaimer

For educational purposes. Options selling carries unlimited risk. Test in PAPER
mode and understand the strategy before risking money. Not financial advice.
