"""
Pragyan Calendar - single-agent dashboard.

Deliberately small: status, PAPER/LIVE toggle, service control, live logs and a
Settings tab that writes to the same settings.json the strategy reads. Unlike a
display-only settings page, edits here actually change strategy behaviour.

    uvicorn dashboard.app:app --host 127.0.0.1 --port 8080
"""

import json
import secrets
import subprocess
import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from config.settings import (DEFAULT_SETTINGS, LOG_DIR, ROOT_DIR, STATE_FILE,
                             credentials, load_settings, save_settings,
                             set_trading_mode)

SERVICE = "pragyan-calendar"
LOG_FILE = LOG_DIR / "calendar.log"
STATIC = Path(__file__).resolve().parent / "static"

app = FastAPI(title="Pragyan Calendar")
security = HTTPBasic()


def auth(creds: HTTPBasicCredentials = Depends(security)):
    """HTTP basic auth. Refuses to serve at all if no password is configured."""
    if not credentials.dash_pass:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="DASH_PASS is not set in .env - dashboard disabled.",
        )
    ok_user = secrets.compare_digest(creds.username, credentials.dash_user)
    ok_pass = secrets.compare_digest(creds.password, credentials.dash_pass)
    if not (ok_user and ok_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return creds.username


def _systemctl(action):
    try:
        r = subprocess.run(["sudo", "systemctl", action, SERVICE],
                           capture_output=True, text=True, timeout=20)
        return r.returncode == 0, (r.stderr or r.stdout).strip()
    except Exception as exc:
        return False, str(exc)


def _service_state():
    try:
        r = subprocess.run(["systemctl", "is-active", SERVICE],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


@app.get("/")
def index(_: str = Depends(auth)):
    return FileResponse(str(STATIC / "index.html"))


@app.get("/api/status")
def api_status(_: str = Depends(auth)):
    state = {"phase": "NOT_STARTED"}
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {
        "service": _service_state(),
        "mode": credentials.trading_mode,
        "state": state,
        "settings": load_settings(),
        "server_time": datetime.datetime.now().isoformat(timespec="seconds"),
        "credentials_ok": not credentials.validate(),
        "missing_credentials": credentials.validate(),
    }


@app.get("/api/logs")
def api_logs(lines: int = 200, _: str = Depends(auth)):
    if not LOG_FILE.exists():
        return {"lines": []}
    lines = max(1, min(int(lines), 2000))
    try:
        content = LOG_FILE.read_text(errors="replace").splitlines()
    except Exception as exc:
        return {"lines": ["<log unreadable: %s>" % exc]}
    return {"lines": content[-lines:]}


@app.post("/api/settings")
def api_settings(payload: dict, _: str = Depends(auth)):
    unknown = [k for k in payload if k not in DEFAULT_SETTINGS]
    if unknown:
        raise HTTPException(400, "Unknown setting(s): %s" % ", ".join(unknown))
    updated = save_settings(payload)
    return {"ok": True, "settings": updated,
            "note": "Applied on the next scheduled cycle. Restart to reschedule job times."}


@app.post("/api/mode")
def api_mode(payload: dict, _: str = Depends(auth)):
    """
    Flip PAPER/LIVE. Going LIVE is intentionally explicit: the caller must send
    confirm=true, so a stray click cannot arm real money.
    """
    requested = str(payload.get("mode", "")).upper()
    if requested not in ("PAPER", "LIVE"):
        raise HTTPException(400, "mode must be PAPER or LIVE")
    if requested == "LIVE":
        if not payload.get("confirm"):
            raise HTTPException(400, "Going LIVE requires confirm=true")
        missing = credentials.validate()
        if missing:
            raise HTTPException(400, "Cannot go LIVE, missing: %s" % ", ".join(missing))
    mode = set_trading_mode(requested)
    restarted, msg = _systemctl("restart")
    return {"ok": True, "mode": mode, "restarted": restarted, "detail": msg}


@app.post("/api/service/{action}")
def api_service(action: str, _: str = Depends(auth)):
    if action not in ("start", "stop", "restart"):
        raise HTTPException(400, "action must be start, stop or restart")
    ok, msg = _systemctl(action)
    return JSONResponse({"ok": ok, "action": action, "detail": msg,
                         "service": _service_state()})
