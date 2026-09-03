"""Google Calendar sync — backfill absence days from the past 7 days."""

import logging
from datetime import date, datetime, timedelta, timezone

from .config import (
    GCAL_CREDENTIALS_FILE, GCAL_TOKEN_FILE, load_config,
)
from .holidays_helper import is_public_holiday
from .state import (
    STATUS_OUT, STATUS_PENDING, STATUS_WFH,
    mark_out, set_day, get_day,
    SOURCE_GOOGLE_CALENDAR,
)

log = logging.getLogger(__name__)

_GCAL_SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]

_cached_service = None
_cached_credentials = None


def _build_service():
    """Return an authenticated Google Calendar service, prompting OAuth if needed.

    Service objects are cached to avoid recreating them on every call.
    Cached service is returned if credentials are still valid.
    """
    global _cached_service, _cached_credentials

    if _cached_credentials and _cached_credentials.valid:
        log.info("🔄 Calendar service cache hit")
        return _cached_service

    log.info("🆕 Calendar service cache miss — rebuilding")

    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    if GCAL_TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(GCAL_TOKEN_FILE), _GCAL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not GCAL_CREDENTIALS_FILE.exists():
                raise FileNotFoundError(
                    f"Google Calendar credentials not found at {GCAL_CREDENTIALS_FILE}.\n"
                    "Download OAuth credentials from Google Cloud Console and save them there."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(GCAL_CREDENTIALS_FILE), _GCAL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        with open(GCAL_TOKEN_FILE, "w") as f:
            f.write(creds.to_json())

    _cached_credentials = creds
    _cached_service = build("calendar", "v3", credentials=creds)
    return _cached_service


def _fetch_events(service, calendar_id: str, since: date, until: date) -> list[dict]:
    time_min = datetime(since.year, since.month, since.day, tzinfo=timezone.utc).isoformat()
    time_max = datetime(until.year, until.month, until.day, 23, 59, 59, tzinfo=timezone.utc).isoformat()

    events = []
    page_token = None
    while True:
        resp = service.events().list(
            calendarId=calendar_id,
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy="startTime",
            pageToken=page_token,
        ).execute()
        events.extend(resp.get("items", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return events


def _is_absence_event(event: dict, keywords: list[str]) -> bool:
    title = (event.get("summary") or "").lower()
    return any(kw.lower() in title for kw in keywords)


def _event_dates(event: dict) -> list[date]:
    """Return list of calendar dates spanned by an event."""
    start = event.get("start", {})
    end = event.get("end", {})

    if "date" in start:
        s = date.fromisoformat(start["date"])
        e = date.fromisoformat(end["date"]) - timedelta(days=1)  # end is exclusive in all-day events
    else:
        s = datetime.fromisoformat(start["dateTime"]).date()
        e = datetime.fromisoformat(end["dateTime"]).date()

    days = []
    d = s
    while d <= e:
        days.append(d)
        d += timedelta(days=1)
    return days


def run_sync() -> list[dict]:
    """
    Fetch Google Calendar events for the past 7 days and backfill
    absence days in state.json.  Returns a list of change records.
    """
    cfg = load_config()
    country = cfg["country"]
    cal_id = cfg.get("google_calendar_id", "primary")
    keywords = cfg.get("absence_keywords", [])
    lookback = cfg.get("calendar_lookback_days", 7)

    today = date.today()
    since = today - timedelta(days=lookback)

    try:
        service = _build_service()
    except Exception as e:
        log.warning("Google Calendar auth failed — skipping sync: %s", e)
        return []

    try:
        events = _fetch_events(service, cal_id, since, today)
    except Exception as e:
        log.warning("Google Calendar fetch failed — skipping sync: %s", e)
        return []

    # Collect all absence dates from matching events
    absence_dates: set[date] = set()
    for event in events:
        if _is_absence_event(event, keywords):
            for d in _event_dates(event):
                if since <= d <= today:
                    absence_dates.add(d)

    changes = []
    for d in sorted(absence_dates):
        # Skip weekends
        if d.weekday() >= 5:
            continue
        # Skip public holidays (already marked out)
        if is_public_holiday(d, country):
            continue

        day = get_day(d)
        current_status = day.get("status")

        # Priority rules: skip if WiFi-confirmed or manually set
        if day.get("marked_office"):
            log.debug("Skipping %s — WiFi confirmed office", d)
            continue
        if day.get("manually_set"):
            log.debug("Skipping %s — manually set", d)
            continue

        # Only backfill if status is wfh, pending, or unset
        if current_status in (STATUS_WFH, STATUS_PENDING, None):
            mark_out(d, source=SOURCE_GOOGLE_CALENDAR)
            set_day(d, {"backfilled_at": datetime.utcnow().isoformat()})
            log.info("Backfilled %s → OUT (source: google_calendar)", d)
            changes.append({"date": d.isoformat(), "previous": current_status, "new": STATUS_OUT})

    return changes
