"""WiFi monitoring via CoreWLAN (macOS only).

Checks every 60 seconds whether the device is connected to the configured
SSID.  Accumulates connected time in state.json; once the 2-hour threshold
is reached the day is immediately marked as IN OFFICE and metrics are pushed.
"""

import logging
import time
from datetime import date

from .config import load_config
from .state import add_wifi_seconds, get_day, mark_office, mark_wfh, STATUS_OFFICE

log = logging.getLogger(__name__)

_TICK_SECONDS = 60


def _current_ssid() -> str | None:
    """Return the SSID of the active WiFi network, or None if disconnected."""
    try:
        import objc  # noqa: F401 — raises ImportError on non-macOS or missing PyObjC
        from CoreWLAN import CWWiFiClient

        client = CWWiFiClient.sharedWiFiClient()
        iface = client.interface()
        if iface is None:
            log.warning("CoreWLAN: no WiFi interface found")
            return None
        return iface.ssid()
    except Exception as e:
        log.warning("CoreWLAN unavailable: %s", e)
        return _current_ssid_fallback()


def _current_ssid_fallback() -> str | None:
    """Fallback using the airport CLI (available on all macOS versions)."""
    import subprocess
    try:
        result = subprocess.run(
            [
                "/System/Library/PrivateFrameworks/Apple80211.framework/"
                "Versions/Current/Resources/airport",
                "-I",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        for line in result.stdout.splitlines():
            if "SSID" in line and "BSSID" not in line:
                return line.split(":")[-1].strip()
    except Exception as e:
        log.warning("airport fallback failed: %s", e)
    return None


def tick(push_callback=None) -> bool:
    """
    Run one 60-second WiFi tick for today.

    Returns True if the office threshold was newly crossed this tick.
    *push_callback* is called with no args when the threshold is crossed
    or when we reach the hourly push boundary.
    """
    cfg = load_config()
    target_ssid: str = cfg["wifi_ssid"]
    threshold_seconds: int = cfg["wifi_threshold_minutes"] * 60
    today = date.today()

    cfg_day = get_day(today)
    already_marked = cfg_day.get("marked_office", False)

    ssid = _current_ssid()
    if ssid == target_ssid:
        total = add_wifi_seconds(today, _TICK_SECONDS)
        log.debug("WiFi tick — SSID: %s, cumulative: %ds", ssid, total)

        if total >= threshold_seconds and not already_marked:
            mark_office(today)
            log.info("2-hour WiFi threshold reached — marked as IN OFFICE")
            if push_callback:
                push_callback()
            return True
    else:
        log.debug("WiFi tick — not on %s (saw: %s)", target_ssid, ssid)

    return False


def end_of_day_default(push_callback=None):
    """Called at 23:59 — mark today as WFH if no other status set."""
    today = date.today()
    day = get_day(today)
    if not day.get("marked_office") and not day.get("manually_set"):
        mark_wfh(today)
        log.info("EOD default — marked %s as WFH", today)
        if push_callback:
            push_callback()


def run_monitor(push_callback=None, stop_event=None):
    """
    Blocking loop: ticks every 60 s and fires push_callback every 10 minutes.
    Push is now synchronous to eliminate unbounded thread creation
    (was creating 144 threads/day, causing ~1 GB memory accumulation per month).
    Push is fast (~1-2s), so blocking for this brief period is acceptable.
    Pass a threading.Event to *stop_event* to gracefully stop.
    """
    import threading
    if stop_event is None:
        stop_event = threading.Event()

    def _fire_push():
        """Fire push_callback synchronously."""
        if push_callback:
            push_callback()

    tick_count = 0
    ticks_per_push = 600 // _TICK_SECONDS  # every 10 minutes

    while not stop_event.is_set():
        crossed = tick(_fire_push)
        if not crossed:
            tick_count += 1
            if tick_count >= ticks_per_push:
                tick_count = 0
                log.info("10-minute push triggered")
                _fire_push()

        stop_event.wait(_TICK_SECONDS)
