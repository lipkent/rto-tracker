"""Monthly attendance calculations with split-week majority rule."""

import logging
from datetime import date, timedelta
from typing import NamedTuple, Optional

from .config import load_config
from .holidays_helper import is_public_holiday
from .state import (
    STATUS_OFFICE, STATUS_OUT, STATUS_WFH, STATUS_PENDING,
    get_status,
)

log = logging.getLogger(__name__)


class MonthBlock(NamedTuple):
    year: int
    month: int
    working_days: list[date]   # sorted Mon–Fri dates belonging to this block


class MonthMetrics(NamedTuple):
    year: int
    month: int
    base_working_days: int
    out_days: int
    in_office_days: int
    adjusted_working_days: int
    expected_days: int
    attendance_pct: float
    remaining_working_days: int
    days_still_needed: int
    can_make_target: int        # 1 or 0
    target_met: int             # 1=met, 0=not met, 2=pending
    in_office_today: int
    status_today: int           # 1=office, 0=wfh, 2=out, 3=pending
    wifi_minutes_today: float
    wfh_days: int               # days explicitly marked as wfh


def _iso_weeks_in_range(start: date, end: date):
    """Yield (iso_year, iso_week) tuples for all weeks covering [start, end]."""
    seen = set()
    d = start
    while d <= end:
        key = d.isocalendar()[:2]
        if key not in seen:
            seen.add(key)
            yield key
        d += timedelta(days=1)


def _week_dates(iso_year: int, iso_week: int) -> list[date]:
    """Return Mon–Fri dates for a given ISO week."""
    # ISO week starts on Monday; day 1 = Monday
    jan4 = date(iso_year, 1, 4)
    week_start = jan4 + timedelta(weeks=iso_week - jan4.isocalendar()[1], days=-jan4.weekday())
    return [week_start + timedelta(days=i) for i in range(5)]


def build_month_block(year: int, month: int, country: str) -> MonthBlock:
    """
    Return the set of working dates that belong to this month block,
    applying the split-week majority rule.

    A full Mon–Fri week that straddles two calendar months is assigned
    entirely to whichever month contains the majority of its working days.
    Since there are always 5 working days in a full week, ties cannot occur.
    """
    first = date(year, month, 1)
    if month == 12:
        last = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        last = date(year, month + 1, 1) - timedelta(days=1)

    # Expand range to include any Mon–Fri in partially-overlapping weeks
    range_start = first - timedelta(days=first.weekday())          # Monday of first week
    range_end = last + timedelta(days=(4 - last.weekday()) % 7)    # Friday of last week

    assigned: list[date] = []

    for iso_year, iso_week in _iso_weeks_in_range(range_start, range_end):
        week_days = _week_dates(iso_year, iso_week)

        days_in_this_month = [d for d in week_days if d.year == year and d.month == month]
        days_in_other = [d for d in week_days if not (d.year == year and d.month == month)]

        if not days_in_other:
            # Entire week belongs to this month
            assigned.extend(week_days)
        else:
            # Split week — apply majority rule
            if len(days_in_this_month) > len(days_in_other):
                assigned.extend(week_days)
            # else: week belongs to another month — skip

    # Deduplicate and sort
    assigned = sorted(set(assigned))
    return MonthBlock(year=year, month=month, working_days=assigned)


def _status_lookup(d: date, state: Optional[dict]) -> Optional[str]:
    """Return day status from a pre-loaded state dict if provided, else read disk."""
    if state is not None:
        return state.get("days", {}).get(d.isoformat(), {}).get("status")
    return get_status(d)


def _wifi_seconds_lookup(d: date, state: Optional[dict]) -> int:
    """Return wifi_seconds from a pre-loaded state dict if provided, else read disk."""
    if state is not None:
        return state.get("days", {}).get(d.isoformat(), {}).get("wifi_seconds", 0)
    from .state import get_wifi_seconds
    return get_wifi_seconds(d)


def calculate_month(year: int, month: int, today: Optional[date] = None, state: Optional[dict] = None) -> MonthMetrics:
    """
    *state*: optional pre-loaded state dict (from state.load_state()) to avoid
    re-reading state.json from disk for every working day. When omitted,
    falls back to the original per-day get_status()/get_wifi_seconds() disk
    reads — used by CLI commands (status/report/push/export) that call this
    without a cached state.
    """
    cfg = load_config()
    country = cfg["country"]
    rto_target = cfg["rto_target_pct"]

    if today is None:
        today = date.today()

    block = build_month_block(year, month, country)

    base_working_days = 0
    public_holiday_days = 0
    regular_out_days = 0    # PTO, sick, manual out — excludes public holidays
    in_office_days = 0
    remaining_working_days = 0

    for d in block.working_days:
        if is_public_holiday(d, country):
            public_holiday_days += 1
            continue

        base_working_days += 1

        status = _status_lookup(d, state)

        if status == STATUS_OUT:
            regular_out_days += 1
        elif status == STATUS_OFFICE:
            in_office_days += 1

        if d > today and status != STATUS_OUT:
            remaining_working_days += 1

    # out_days for reporting = public holidays + regular out (PTO/sick)
    out_days = public_holiday_days + regular_out_days

    # adjusted_working_days only subtracts regular out days (not public holidays
    # which are already excluded from base_working_days)
    adjusted_working_days = base_working_days - regular_out_days
    expected_days = round(adjusted_working_days * rto_target)

    days_still_needed = max(0, expected_days - in_office_days)
    can_make_target = 1 if remaining_working_days >= days_still_needed else 0

    attendance_pct = (in_office_days / expected_days) if expected_days > 0 else 0.0

    # target_met: 2=pending (month not finished), 1=met, 0=not met
    month_finished = today > block.working_days[-1] if block.working_days else True
    if month_finished:
        target_met = 1 if attendance_pct >= 1.0 else 0
    elif in_office_days >= expected_days:
        target_met = 1  # target already hit even though month is not over
    else:
        target_met = 2  # still pending

    # Today's daily metrics — use block membership instead of month number so
    # split-week days (e.g. June 29 assigned to July's block) are handled correctly.
    is_current_block = today in block.working_days
    today_status = _status_lookup(today, state) if is_current_block else None
    status_today_map = {
        STATUS_OFFICE: 1,
        STATUS_WFH: 0,
        STATUS_OUT: 2,
        STATUS_PENDING: 3,
        None: 3,
    }
    status_today = status_today_map.get(today_status, 3)
    in_office_today = 1 if today_status == STATUS_OFFICE else 0

    wifi_seconds = _wifi_seconds_lookup(today, state) if is_current_block else 0
    wifi_minutes_today = wifi_seconds / 60.0

    wfh_days = sum(
        1 for d in block.working_days
        if _status_lookup(d, state) == STATUS_WFH
    )

    return MonthMetrics(
        year=year,
        month=month,
        base_working_days=base_working_days,
        out_days=out_days,
        in_office_days=in_office_days,
        adjusted_working_days=adjusted_working_days,
        expected_days=expected_days,
        attendance_pct=attendance_pct,
        remaining_working_days=remaining_working_days,
        days_still_needed=days_still_needed,
        can_make_target=can_make_target,
        target_met=target_met,
        in_office_today=in_office_today,
        status_today=status_today,
        wifi_minutes_today=wifi_minutes_today,
        wfh_days=wfh_days,
    )


def current_month_block_for(today: Optional[date] = None) -> tuple[int, int]:
    """Return (year, month) of the month block that *today* belongs to."""
    if today is None:
        today = date.today()

    year, month = today.year, today.month
    block = build_month_block(year, month, load_config()["country"])

    if today in block.working_days:
        return year, month

    # Check adjacent months if today is in a split week that went elsewhere
    for dy in [-1, 1]:
        adj = date(year, month, 1) + timedelta(days=31 * dy)
        adj_block = build_month_block(adj.year, adj.month, load_config()["country"])
        if today in adj_block.working_days:
            return adj.year, adj.month

    return year, month


def format_report(m: MonthMetrics) -> str:
    target_labels = {2: "PENDING 🔄", 1: "MET ✅", 0: "NOT MET ❌"}
    lines = [
        f"── RTO Report: {m.year}-{m.month:02d} ──────────────────────────",
        f"  Base working days    : {m.base_working_days}",
        f"  Out days             : {m.out_days}",
        f"  WFH days             : {m.wfh_days}",
        f"  Adjusted working days: {m.adjusted_working_days}",
        f"  In-office days       : {m.in_office_days}",
        f"  Expected days (60%)  : {m.expected_days}",
        f"  Attendance %         : {m.attendance_pct * 100:.1f}%",
        f"  Remaining work days  : {m.remaining_working_days}",
        f"  Days still needed    : {m.days_still_needed}",
        f"  Can make target      : {'Yes' if m.can_make_target else 'No'}",
        f"  Target               : {target_labels[m.target_met]}",
        f"────────────────────────────────────────────────",
        f"  Today — office: {m.in_office_today}  WiFi: {m.wifi_minutes_today:.0f} min",
    ]
    return "\n".join(lines)
