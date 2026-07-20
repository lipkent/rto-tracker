"""State management — all daily data persisted to ~/.rto_tracker/state.json."""

import json
import logging
import shutil
from datetime import date, datetime
from typing import Optional

from .config import STATE_FILE, ensure_dirs

log = logging.getLogger(__name__)

# Status constants
STATUS_OFFICE = "office"
STATUS_WFH = "wfh"
STATUS_OUT = "out"
STATUS_PENDING = "pending"

# Source constants
SOURCE_WIFI = "wifi"
SOURCE_MANUAL = "manual"
SOURCE_GOOGLE_CALENDAR = "google_calendar"
SOURCE_EOD_DEFAULT = "eod_default"
SOURCE_PUBLIC_HOLIDAY = "public_holiday"


def _date_key(d: date) -> str:
    return d.isoformat()


def load_state() -> dict:
    ensure_dirs()
    if not STATE_FILE.exists():
        return {"days": {}}
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        data.setdefault("days", {})
        return data
    except (json.JSONDecodeError, OSError) as e:
        log.warning("state.json corrupted (%s) — reinitialising", e)
        backup = STATE_FILE.with_suffix(".json.bak")
        shutil.copy2(STATE_FILE, backup)
        return {"days": {}}


def save_state(state: dict):
    ensure_dirs()
    tmp = STATE_FILE.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, default=str)
    tmp.replace(STATE_FILE)


def get_day(d: date) -> dict:
    state = load_state()
    return state["days"].get(_date_key(d), {})


def set_day(d: date, updates: dict, *, overwrite: bool = False):
    """Merge *updates* into the day record, then persist."""
    state = load_state()
    key = _date_key(d)
    existing = state["days"].get(key, {})
    if overwrite:
        state["days"][key] = updates
    else:
        state["days"][key] = {**existing, **updates}
    save_state(state)


def get_wifi_seconds(d: date) -> int:
    return get_day(d).get("wifi_seconds", 0)


def add_wifi_seconds(d: date, seconds: int) -> int:
    """Add *seconds* to today's WiFi counter. Returns new total."""
    state = load_state()
    key = _date_key(d)
    day = state["days"].get(key, {})
    total = day.get("wifi_seconds", 0) + seconds
    day["wifi_seconds"] = total
    state["days"][key] = day
    save_state(state)
    return total


def mark_office(d: date):
    state = load_state()
    key = _date_key(d)
    day = state["days"].get(key, {})
    day["status"] = STATUS_OFFICE
    day["marked_office"] = True
    day["source"] = SOURCE_WIFI
    day["updated_at"] = datetime.utcnow().isoformat()
    state["days"][key] = day
    save_state(state)


def mark_wfh(d: date, source: str = SOURCE_EOD_DEFAULT):
    state = load_state()
    key = _date_key(d)
    day = state["days"].get(key, {})
    # Never overwrite WiFi-confirmed office or manual overrides
    if day.get("marked_office") or day.get("manually_set"):
        return
    day["status"] = STATUS_WFH
    day["marked_office"] = False
    day["source"] = source
    day["updated_at"] = datetime.utcnow().isoformat()
    state["days"][key] = day
    save_state(state)


def mark_out(d: date, source: str = SOURCE_PUBLIC_HOLIDAY, *, force: bool = False):
    state = load_state()
    key = _date_key(d)
    day = state["days"].get(key, {})
    # Priority 1: WiFi confirmed
    if not force and day.get("marked_office"):
        log.debug("Skipping mark_out for %s — WiFi confirmed office", d)
        return
    # Priority 2: Manual override
    if not force and day.get("manually_set"):
        log.debug("Skipping mark_out for %s — manually set", d)
        return
    day["status"] = STATUS_OUT
    day["marked_office"] = False
    day["source"] = source
    day["updated_at"] = datetime.utcnow().isoformat()
    state["days"][key] = day
    save_state(state)


def manual_override(d: date, status: str, wifi_seconds: int = 0):
    """Priority 2 — manual override via CLI command."""
    assert status in (STATUS_OFFICE, STATUS_WFH, STATUS_OUT), f"Invalid status: {status}"
    state = load_state()
    key = _date_key(d)
    day = state["days"].get(key, {})
    day["status"] = status
    day["marked_office"] = status == STATUS_OFFICE
    day["manually_set"] = True
    day["source"] = SOURCE_MANUAL
    day["updated_at"] = datetime.utcnow().isoformat()
    if wifi_seconds > 0:
        day["wifi_seconds"] = wifi_seconds
    state["days"][key] = day
    save_state(state)
    log.info("Manual override: %s → %s (wifi: %ds)", d, status, wifi_seconds)


def get_all_days() -> dict:
    return load_state().get("days", {})


def get_status(d: date) -> Optional[str]:
    return get_day(d).get("status")
