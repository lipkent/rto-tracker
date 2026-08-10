"""Background scheduler: WiFi monitor + EOD default + calendar sync + 10-minute push."""

import logging
import signal
import threading
from datetime import date, datetime, time as dtime

from .calendar_sync import run_sync
from .calculations import build_month_block, current_month_block_for
from .datadog_client import push_all_months, update_dashboard_month_filter
from .holidays_helper import is_public_holiday
from .config import load_config
from .state import get_day, mark_wfh, STATUS_PENDING
from .wifi import run_monitor, end_of_day_default

log = logging.getLogger(__name__)

_HOURLY_SYNC_INTERVAL = 3600   # seconds between hourly calendar syncs


def _push_callback():
    """Called when WiFi threshold is crossed or 10-minute tick fires."""
    try:
        push_all_months()
    except Exception as e:
        log.error("Push callback failed: %s", e)


def _backfill_pending_days():
    """
    On startup, mark any past working days still sitting at 'pending' as wfh.
    Covers the case where the script was not running at 23:59 EOD.
    Does not overwrite WiFi-confirmed office days or manual overrides.

    Scans the current calendar month AND the immediately-preceding one (not
    just current_month_block_for's single block) — otherwise a day left
    pending right at a month's end gets permanently orphaned the moment the
    calendar rolls into the next month, since every later run would only
    ever look at the new current month again.
    """
    cfg = load_config()
    country = cfg["country"]
    today = date.today()

    year, month = today.year, today.month
    if month == 1:
        prev_year, prev_month = year - 1, 12
    else:
        prev_year, prev_month = year, month - 1

    backfilled = 0
    for y, m in [(prev_year, prev_month), (year, month)]:
        block = build_month_block(y, m, country)
        for d in block.working_days:
            if d >= today:
                continue
            if is_public_holiday(d, country):
                continue
            day = get_day(d)
            if day.get("marked_office") or day.get("manually_set"):
                continue
            status = day.get("status")
            if status == STATUS_PENDING or not status:
                mark_wfh(d)
                log.info("Backfilled pending day %s → wfh", d)
                backfilled += 1

    if backfilled:
        log.info("Backfilled %d pending day(s) → wfh", backfilled)


def _run_calendar_sync():
    """Run calendar sync and push metrics if any changes were found."""
    try:
        changes = run_sync()
        if changes:
            log.info("Calendar sync updated %d day(s)", len(changes))
            push_all_months()
        return changes
    except Exception as e:
        log.error("Calendar sync failed: %s", e)
        return []


def _scheduler_thread(stop_event: threading.Event):
    """
    Background thread that:
      - Runs calendar sync every hour
      - Runs daily backfill once per day at 00:05
      - Updates Datadog dashboard month filter when month block changes
      - Runs EOD default at 23:59
    """
    last_sync_time = None
    eod_done_today = None
    backfill_done_today = None
    last_known_block = None   # tracks (year, month) of current block

    while not stop_event.is_set():
        now = datetime.now()
        today = now.date()
        current_time = now.time()

        # Hourly calendar sync
        import time as _time
        now_ts = _time.monotonic()
        if last_sync_time is None or (now_ts - last_sync_time) >= _HOURLY_SYNC_INTERVAL:
            _run_calendar_sync()
            last_sync_time = now_ts

        # Daily backfill — runs once per day at 00:05 (5 minutes after midnight)
        # so it catches any pending days from yesterday when the laptop wakes up
        # in the morning even without a script restart.
        if backfill_done_today != today and current_time >= dtime(0, 5):
            try:
                _backfill_pending_days()
                push_all_months()
            except Exception as e:
                log.error("Daily backfill failed: %s", e)
            backfill_done_today = today

        # Datadog dashboard month filter — update when the month block changes.
        # Uses current_month_block_for() so split-week days (e.g. June 29
        # assigned to July's block) are handled correctly.
        try:
            current_block = current_month_block_for(today)
            if current_block != last_known_block:
                log.info("Month block changed to %s-%02d — updating dashboard filter",
                         current_block[0], current_block[1])
                update_dashboard_month_filter()
                last_known_block = current_block
        except Exception as e:
            log.error("Dashboard month filter update failed: %s", e)

        # EOD default at 23:59
        if eod_done_today != today and current_time >= dtime(23, 59):
            end_of_day_default(_push_callback)
            eod_done_today = today

        stop_event.wait(30)


def run(daemonize: bool = False):
    """
    Start the RTO tracker.  If *daemonize* is False, blocks until SIGINT/SIGTERM.
    """
    stop_event = threading.Event()

    def _handle_signal(signum, frame):
        log.info("Signal %d received — stopping", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    log.info("RTO tracker starting")

    # Step 1: Backfill any past pending days → wfh
    try:
        _backfill_pending_days()
    except Exception as e:
        log.warning("Backfill pending days failed: %s", e)

    # Step 2: Calendar sync on startup
    try:
        changes = _run_calendar_sync()
        if changes:
            log.info("Startup calendar sync backfilled %d day(s)", len(changes))
    except Exception as e:
        log.warning("Startup calendar sync skipped: %s", e)

    # Step 3: Initial metrics push
    try:
        push_all_months()
    except Exception as e:
        log.warning("Initial push failed: %s", e)

    # Step 4: Start background scheduler thread (hourly sync + EOD)
    scheduler_thread = threading.Thread(
        target=_scheduler_thread, args=(stop_event,), daemon=True, name="scheduler"
    )
    scheduler_thread.start()

    # Step 5: WiFi monitor (blocks in this thread)
    run_monitor(push_callback=_push_callback, stop_event=stop_event)

    log.info("RTO tracker stopped")
