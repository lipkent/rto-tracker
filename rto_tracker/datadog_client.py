"""Push RTO metrics to Datadog via the HTTP API."""

import logging
import time
from datetime import date

from .calculations import MonthMetrics, calculate_month, current_month_block_for
from .config import load_config

log = logging.getLogger(__name__)


def _send_metrics(metrics: list[dict], api_key: str, site: str):
    """POST a batch of gauge metrics to the Datadog v2 metrics API."""
    import json
    import urllib.request

    url = f"https://api.{site}/api/v2/series"
    payload = json.dumps({"series": metrics}).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "DD-API-KEY": api_key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status not in (200, 202):
                log.error("Datadog push failed — HTTP %d", resp.status)
    except Exception as e:
        log.error("Datadog push error: %s", e)
        raise


def _build_series(name: str, value: float, tags: list[str]) -> dict:
    return {
        "metric": name,
        "type": 3,  # gauge
        "points": [{"timestamp": int(time.time()), "value": value}],
        "tags": tags,
    }


def push_metrics(month_metrics: MonthMetrics | None = None):
    """Push all 13 RTO metrics to Datadog.  Calculates current month if not provided."""
    cfg = load_config()
    api_key = cfg.get("datadog_api_key", "")
    if not api_key:
        log.warning("Datadog API key not set — skipping push")
        return

    site = cfg.get("datadog_site", "datadoghq.com")
    today = date.today()

    if month_metrics is None:
        year, month = current_month_block_for(today)
        month_metrics = calculate_month(year, month, today)

    m = month_metrics
    base_tags = list(cfg.get("datadog_tags", ["office:ams"])) + [
        f"month:{m.year}-{m.month:02d}",
        f"year:{m.year}",
    ]

    series = [
        _build_series("rto.base_working_days",      m.base_working_days,      base_tags),
        _build_series("rto.out_days",               m.out_days,               base_tags),
        _build_series("rto.in_office_days",         m.in_office_days,         base_tags),
        _build_series("rto.adjusted_working_days",  m.adjusted_working_days,  base_tags),
        _build_series("rto.expected_days",          m.expected_days,          base_tags),
        _build_series("rto.attendance_pct",         m.attendance_pct,         base_tags),
        _build_series("rto.remaining_working_days", m.remaining_working_days, base_tags),
        _build_series("rto.days_still_needed",      m.days_still_needed,      base_tags),
        _build_series("rto.can_make_target",        m.can_make_target,        base_tags),
        _build_series("rto.target_met",             m.target_met,             base_tags),
        _build_series("rto.in_office_today",        m.in_office_today,        base_tags),
        _build_series("rto.status_today",           m.status_today,           base_tags),
        _build_series("rto.wifi_minutes_today",     m.wifi_minutes_today,     base_tags),
        _build_series("rto.wfh_days",               m.wfh_days,               base_tags),
    ]

    try:
        _send_metrics(series, api_key, site)
        log.info("Pushed %d RTO metrics (month %d-%02d)", len(series), m.year, m.month)
    except Exception:
        log.error("Failed to push metrics — will retry on next scheduled push")
        return

    # ── Update Google Sheet ───────────────────────────────────────────────────
    try:
        from .gdrive_export import update_sheet
        update_sheet(today_wifi_minutes=m.wifi_minutes_today)
    except Exception as e:
        log.warning("Google Sheet update skipped: %s", e)


def push_all_months():
    """Recalculate and push metrics for the current month and, if needed, the previous one."""
    today = date.today()
    year, month = current_month_block_for(today)
    push_metrics(calculate_month(year, month, today))

    # Also push previous month if we're in the first week (split-week spillover)
    if today.day <= 7:
        prev = date(year, month, 1) - __import__("datetime").timedelta(days=1)
        push_metrics(calculate_month(prev.year, prev.month, today))


def update_dashboard_month_filter():
    """
    Update the Datadog dashboard's 'month' template variable default to the
    current month block. Uses current_month_block_for() so split-week days
    (e.g. June 29 assigned to July's block) are handled correctly.

    Requires:
      - Datadog API key (Keychain)
      - Datadog App key (Keychain)
      - datadog_dashboard_id in config.json
    """
    import json
    import urllib.request
    from .config import get_app_key

    cfg = load_config()
    api_key      = cfg.get("datadog_api_key", "")
    app_key      = get_app_key()
    dashboard_id = cfg.get("datadog_dashboard_id", "")
    site         = cfg.get("datadog_site", "datadoghq.com")

    if not api_key or not app_key:
        log.warning("Datadog API key or App key not set — skipping dashboard update")
        return
    if not dashboard_id:
        log.warning("datadog_dashboard_id not set — skipping dashboard update")
        return

    today = date.today()
    year, month = current_month_block_for(today)
    month_value = f"{year}-{month:02d}"

    headers = {
        "Content-Type": "application/json",
        "DD-API-KEY": api_key,
        "DD-APPLICATION-KEY": app_key,
    }
    url = f"https://api.{site}/api/v1/dashboard/{dashboard_id}"

    try:
        # Step 1: GET current dashboard definition
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            dashboard = json.loads(resp.read())

        # Step 2: Update the 'month' template variable default value
        updated = False
        for tv in dashboard.get("template_variables", []):
            if tv.get("name") == "month":
                if tv.get("default") == month_value:
                    log.debug("Dashboard month filter already set to %s", month_value)
                    return
                tv["default"] = month_value
                updated = True
                break

        if not updated:
            log.warning("Dashboard has no 'month' template variable — skipping update")
            return

        # Step 3: PUT updated dashboard back
        req = urllib.request.Request(
            url,
            data=json.dumps(dashboard).encode(),
            headers=headers,
            method="PUT",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status in (200, 202):
                log.info("Dashboard month filter updated to %s", month_value)
            else:
                log.error("Dashboard update failed — HTTP %d", resp.status)

    except Exception as e:
        log.error("Failed to update dashboard month filter: %s", e)
