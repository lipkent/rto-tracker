"""Google Sheets export for RTO Tracker.

Creates / updates a Google Sheets file called "RTO Tracker" in the user's
Google Drive.  Each calendar year gets its own sheet.  Every working day
is written as one row; a summary row is appended at the bottom of each
month block.

Sheet layout (per year tab):
  A: Date         B: Month        C: Status       D: WiFi (min)
  E: Office Day   F: WFH Day      G: Out Day      H: Attendance %
  I: Target Met

The sheet is re-synced for the current year on every push, and on first
run it back-fills all historical data found in state.json.
"""

import logging
from datetime import date

from .config import GCAL_CREDENTIALS_FILE, CONFIG_DIR, load_config
from .holidays_helper import is_public_holiday
from .state import get_status, get_wifi_seconds, STATUS_OFFICE, STATUS_WFH, STATUS_OUT, load_state
from .calculations import build_month_block, calculate_month

log = logging.getLogger(__name__)

# Reuse the same token file as calendar — scopes are combined at auth time
_TOKEN_FILE = CONFIG_DIR / "gdrive_token.json"

_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

SPREADSHEET_NAME = "RTO Tracker"

# Column header row
_HEADERS = [
    "Date", "Month", "Status", "WiFi (min)",
    "Office Day", "WFH Day", "Out Day",
    "Attendance %", "Target Met",
]

# Colour palette (RGB integers)
_COL_HEADER   = {"red": 0.42, "green": 0.25, "blue": 0.67}   # Datadog purple #6B40AC
_COL_SUMMARY  = {"red": 0.85, "green": 0.90, "blue": 0.97}   # light blue
_COL_OFFICE   = {"red": 0.85, "green": 0.95, "blue": 0.85}   # light green
_COL_WFH      = {"red": 1.00, "green": 1.00, "blue": 0.88}   # light yellow
_COL_OUT      = {"red": 0.95, "green": 0.88, "blue": 0.88}   # light red
_COL_WHITE    = {"red": 1.00, "green": 1.00, "blue": 1.00}
_COL_HDR_TEXT = {"red": 1.00, "green": 1.00, "blue": 1.00}   # white text for purple header

_cached_sheets_service = None
_cached_drive_service = None
_cached_credentials = None

# ── Auth ──────────────────────────────────────────────────────────────────────

def _build_services():
    """Return authenticated (sheets_service, drive_service) tuple.

    Service objects are cached to avoid recreating them on every call.
    Cached services are returned if credentials are still valid.
    """
    global _cached_sheets_service, _cached_drive_service, _cached_credentials

    if _cached_credentials and _cached_credentials.valid:
        log.info("🔄 Sheets/Drive services cache hit")
        return _cached_sheets_service, _cached_drive_service

    log.info("🆕 Sheets/Drive services cache miss — rebuilding")

    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    if _TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(_TOKEN_FILE), _SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not GCAL_CREDENTIALS_FILE.exists():
                raise FileNotFoundError(
                    f"Google credentials not found at {GCAL_CREDENTIALS_FILE}.\n"
                    "Download OAuth credentials from Google Cloud Console."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(GCAL_CREDENTIALS_FILE), _SCOPES
            )
            creds = flow.run_local_server(port=0)
        with open(_TOKEN_FILE, "w") as f:
            f.write(creds.to_json())

    _cached_credentials = creds
    _cached_sheets_service = build("sheets", "v4", credentials=creds)
    _cached_drive_service = build("drive", "v3", credentials=creds)
    return _cached_sheets_service, _cached_drive_service


# ── Spreadsheet helpers ───────────────────────────────────────────────────────

def _find_or_create_spreadsheet(drive, sheets) -> str:
    """Return the spreadsheet ID, creating it if it does not exist.
    Creates the spreadsheet with an Info sheet as a permanent fallback tab.
    """
    resp = drive.files().list(
        q=f"name='{SPREADSHEET_NAME}' and mimeType='application/vnd.google-apps.spreadsheet' and trashed=false",
        spaces="drive",
        fields="files(id, name)",
    ).execute()

    files = resp.get("files", [])
    if files:
        sid = files[0]["id"]
        log.info("Found existing spreadsheet: %s", sid)
        return sid

    # Create spreadsheet with Info as the permanent fallback sheet
    body = {
        "properties": {"title": SPREADSHEET_NAME},
        "sheets": [{"properties": {"title": "Info"}}],
    }
    ss = sheets.spreadsheets().create(body=body, fields="spreadsheetId").execute()
    sid = ss["spreadsheetId"]
    log.info("Created new spreadsheet: %s", sid)
    return sid


def _get_or_create_sheet(spreadsheets_resource, spreadsheet_id: str, title: str) -> tuple[int, bool]:
    """
    Return (sheetId, is_new) for *title*, creating the tab only if it does not exist.
    Never deletes the sheet — updates in place to avoid redirecting the user.
    is_new = True means the sheet was just created (used to collapse groups on first load).

    *spreadsheets_resource*: the caller's single `sheets.spreadsheets()` object,
    reused rather than re-derived — see update_sheet() for why.
    """
    meta = spreadsheets_resource.get(spreadsheetId=spreadsheet_id).execute()
    for s in meta.get("sheets", []):
        if s["properties"]["title"] == title:
            return s["properties"]["sheetId"], False

    # Sheet does not exist yet — create it at index 0
    resp = spreadsheets_resource.batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"addSheet": {"properties": {"title": title, "index": 0}}}]},
    ).execute()
    return resp["replies"][0]["addSheet"]["properties"]["sheetId"], True


# ── Data builders ─────────────────────────────────────────────────────────────

def _target_label(target_met: int) -> str:
    return {1: "✅ Met", 0: "❌ Not Met", 2: "⏳ Pending"}.get(target_met, "")


def _get_day_from_state(state: dict, d: date) -> dict:
    """Get day record from cached state dict (avoids repeated disk reads)."""
    key = d.isoformat()
    return state.get("days", {}).get(key, {})


def _build_year_rows(year: int, today: date, country: str, state: dict, today_wifi_minutes: float | None = None) -> tuple[list[list], list[dict]]:
    """
    Build all data rows for *year* using cached state dict.

    Accepts pre-loaded state dict to avoid 250+ redundant disk reads per export.
    Uses _get_day_from_state() to query status/wifi from cache instead of re-reading JSON.

    Layout per month:
      Summary row   ← always visible; +/- button appears here (controlBefore)
      Day 1         ┐
      Day 2         │ grouped rows — collapse/expand with +/-
      ...           ┘

    Returns:
      - rows: list of rows matching _HEADERS (header row at index 0)
      - month_groups: list of dicts with start_row, end_row (0-based, exclusive)
        marking only the daily rows so the summary row stays visible when collapsed.
    """
    rows = [_HEADERS]
    month_groups = []

    for month in range(1, 13):
        block = build_month_block(year, month, country)
        if not block.working_days:
            continue

        # Skip months whose entire block is in the future.
        # Use block membership (not calendar month) so split-week days
        # (e.g. June 29 assigned to July's block) are included correctly.
        if block.working_days[0] > today:
            break

        metrics = calculate_month(year, month, today, state=state)

        # ── Summary row FIRST (stays visible, hosts the +/- button) ──────────
        att_pct = f"{metrics.attendance_pct * 100:.1f}%"
        wfh_days = sum(
            1 for d in block.working_days
            if _get_day_from_state(state, d).get("status") == STATUS_WFH
        )
        rows.append([
            f"── {date(year, month, 1).strftime('%B %Y')} Summary ──",
            "",
            "",
            "",
            metrics.in_office_days,
            wfh_days,
            metrics.out_days,
            att_pct,
            _target_label(metrics.target_met),
        ])

        # ── Daily rows (these are the grouped / collapsible rows) ─────────────
        group_start = len(rows)   # first daily row for this month (0-based)

        for d in block.working_days:
            day_record = _get_day_from_state(state, d)
            status = day_record.get("status") or ("pending" if d <= today else "")
            if d == today and today_wifi_minutes is not None:
                # Use the snapshotted value passed from push_metrics so the
                # sheet always shows the exact same WiFi minutes as Datadog
                wifi = round(today_wifi_minutes)
            else:
                wifi_seconds = day_record.get("wifi_seconds", 0)
                wifi = round(wifi_seconds / 60) if d <= today else 0
            is_holiday = is_public_holiday(d, country)

            if is_holiday:
                status = "holiday"

            office_day = 1 if status == STATUS_OFFICE else 0
            wfh_day    = 1 if status == STATUS_WFH    else 0
            out_day    = 1 if status in (STATUS_OUT, "holiday") else 0

            rows.append([
                d.isoformat(),
                d.strftime("%B"),
                status,
                wifi,
                office_day,
                wfh_day,
                out_day,
                "",   # attendance % — summary row only
                "",   # target met   — summary row only
            ])

        group_end = len(rows)     # exclusive end of daily rows
        month_groups.append({"start_row": group_start, "end_row": group_end})

    return rows, month_groups


# ── Formatting helpers ────────────────────────────────────────────────────────

def _cell_color_requests(sheet_id: int, rows: list[list]) -> list[dict]:
    """Return batchUpdate requests that colour each data row."""
    requests = []

    # Header row (row 0)
    requests.append(_color_range(sheet_id, 0, 1, 0, len(_HEADERS), _COL_HEADER))

    for i, row in enumerate(rows[1:], start=1):   # skip header
        label = str(row[0]) if row else ""
        status = str(row[2]).lower() if len(row) > 2 else ""

        if "Summary" in label:
            color = _COL_SUMMARY
        elif status == STATUS_OFFICE:
            color = _COL_OFFICE
        elif status == STATUS_WFH:
            color = _COL_WFH
        elif status in (STATUS_OUT, "holiday"):
            color = _COL_OUT
        else:
            color = _COL_WHITE

        requests.append(_color_range(sheet_id, i, i + 1, 0, len(_HEADERS), color))

    return requests


def _color_range(sheet_id, start_row, end_row, start_col, end_col, color) -> dict:
    return {
        "repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": start_row,
                "endRowIndex": end_row,
                "startColumnIndex": start_col,
                "endColumnIndex": end_col,
            },
            "cell": {
                "userEnteredFormat": {"backgroundColor": color}
            },
            "fields": "userEnteredFormat.backgroundColor",
        }
    }


def _bold_row_request(sheet_id: int, row_index: int) -> dict:
    return {
        "repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": row_index,
                "endRowIndex": row_index + 1,
            },
            "cell": {
                "userEnteredFormat": {
                    "textFormat": {"bold": True}
                }
            },
            "fields": "userEnteredFormat.textFormat.bold",
        }
    }


def _freeze_row_request(sheet_id: int) -> dict:
    return {
        "updateSheetProperties": {
            "properties": {
                "sheetId": sheet_id,
                "gridProperties": {"frozenRowCount": 1},
            },
            "fields": "gridProperties.frozenRowCount",
        }
    }



def _set_column_widths_request(sheet_id: int) -> list[dict]:
    """Set minimum column widths (in pixels) so header text is never cut off."""
    # Widths mapped to: Date, Month, Status, WiFi(min), Office Day, WFH Day, Out Day, Attendance%, Target Met
    widths = [120, 100, 100, 110, 110, 100, 100, 130, 120]
    requests = []
    for i, width in enumerate(widths):
        requests.append({
            "updateDimensionProperties": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "COLUMNS",
                    "startIndex": i,
                    "endIndex": i + 1,
                },
                "properties": {"pixelSize": width},
                "fields": "pixelSize",
            }
        })
    return requests


def _header_text_color_request(sheet_id: int) -> dict:
    """Make header row text white and bold for readability on the purple background."""
    return {
        "repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 0,
                "endRowIndex": 1,
            },
            "cell": {
                "userEnteredFormat": {
                    "textFormat": {
                        "bold": True,
                        "foregroundColor": _COL_HDR_TEXT,
                    }
                }
            },
            "fields": "userEnteredFormat.textFormat.bold,userEnteredFormat.textFormat.foregroundColor",
        }
    }


def _add_row_group_request(sheet_id: int, start_row: int, end_row: int) -> dict:
    """Group rows [start_row, end_row) so they can be collapsed."""
    return {
        "addDimensionGroup": {
            "range": {
                "sheetId": sheet_id,
                "dimension": "ROWS",
                "startIndex": start_row,
                "endIndex": end_row,
            }
        }
    }


def _collapse_row_group_request(sheet_id: int, start_row: int, end_row: int) -> dict:
    """Collapse a row group so it starts in the collapsed state."""
    return {
        "updateDimensionGroup": {
            "dimensionGroup": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "ROWS",
                    "startIndex": start_row,
                    "endIndex": end_row,
                },
                "depth": 1,
                "collapsed": True,
            },
            "fields": "collapsed",
        }
    }



def _get_existing_group_starts(spreadsheets_resource, spreadsheet_id: str, sheet_id: int) -> set:
    """
    Return a set of startIndex values for all existing row groups on the sheet.
    Used to detect which month groups are new so only they get collapsed.

    *spreadsheets_resource*: the caller's single `sheets.spreadsheets()` object,
    reused rather than re-derived — see update_sheet() for why.
    """
    meta = spreadsheets_resource.get(
        spreadsheetId=spreadsheet_id,
        includeGridData=False,
    ).execute()
    starts = set()
    for s in meta.get("sheets", []):
        if s["properties"]["sheetId"] != sheet_id:
            continue
        for group in s.get("rowGroups", []):
            starts.add(group["range"]["startIndex"])
    return starts


def _clear_all_row_groups(spreadsheets_resource, spreadsheet_id: str, sheet_id: int):
    """
    Remove all existing row groups by reading sheet metadata and deleting
    each group by its exact range in one batch. Deterministic — no loops.

    *spreadsheets_resource*: the caller's single `sheets.spreadsheets()` object,
    reused rather than re-derived — see update_sheet() for why.
    """
    meta = spreadsheets_resource.get(
        spreadsheetId=spreadsheet_id,
        includeGridData=False,
    ).execute()

    delete_requests = []
    for s in meta.get("sheets", []):
        if s["properties"]["sheetId"] != sheet_id:
            continue
        for group in s.get("rowGroups", []):
            delete_requests.append({
                "deleteDimensionGroup": {
                    "range": {
                        "sheetId": sheet_id,
                        "dimension": "ROWS",
                        "startIndex": group["range"]["startIndex"],
                        "endIndex":   group["range"]["endIndex"],
                    }
                }
            })

    if delete_requests:
        spreadsheets_resource.batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": delete_requests},
        ).execute()
        log.debug("Cleared %d existing row group(s)", len(delete_requests))


# ── Main export function ──────────────────────────────────────────────────────

def update_sheet(today_wifi_minutes: float | None = None):
    """
    Create or update the RTO Tracker Google Sheet.
    Called automatically on every Datadog push.
    today_wifi_minutes: snapshotted WiFi value from push_metrics — when provided,
    today's row uses this value so the sheet matches Datadog exactly.
    """
    cfg     = load_config()
    country = cfg["country"]
    today   = date.today()
    year    = today.year
    sheet_title = str(year)

    try:
        sheets, drive = _build_services()
    except Exception as e:
        log.error("Google Drive auth failed — skipping sheet export: %s", e)
        return

    try:
        # sheets.spreadsheets() does NOT return a cached sub-resource — it
        # constructs a brand-new googleapiclient Resource object (with all
        # of its methods freshly regenerated) on every call. Call it ONCE
        # here and reuse it for every operation below, instead of paying
        # that cost up to 7x per push (confirmed via live memory profiling:
        # ~170 MB/push from this alone — see plan doc for the full trace).
        spreadsheets_resource = sheets.spreadsheets()

        spreadsheet_id = _find_or_create_spreadsheet(drive, sheets)
        sheet_id, is_new = _get_or_create_sheet(spreadsheets_resource, spreadsheet_id, sheet_title)

        # Only reorder if the year sheet is not already at index 0 —
        # calling updateSheetProperties unnecessarily causes a visible tab shuffle.
        meta_check = spreadsheets_resource.get(spreadsheetId=spreadsheet_id).execute()
        current_index = next(
            (s["properties"]["index"] for s in meta_check.get("sheets", [])
             if s["properties"]["sheetId"] == sheet_id),
            None,
        )
        if current_index != 0:
            spreadsheets_resource.batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"requests": [{
                    "updateSheetProperties": {
                        "properties": {"sheetId": sheet_id, "index": 0},
                        "fields": "index",
                    }
                }]},
            ).execute()
            log.info("Reordered year sheet to index 0")

        # Load state ONCE instead of 250+ times during _build_year_rows
        state = load_state()
        rows, month_groups = _build_year_rows(year, today, country, state, today_wifi_minutes)

        # ── Write data (update in place — no clear step, no empty window) ──
        # values().update() from A1 always overwrites all existing cells.
        # Skipping values().clear() eliminates the empty-sheet window that
        # caused Google Sheets to redirect the user to the Info tab.
        spreadsheets_resource.values().update(
            spreadsheetId=spreadsheet_id,
            range=f"{sheet_title}!A1",
            valueInputOption="RAW",
            body={"values": rows},
        ).execute()

        # ── Formatting ───────────────────────────────────────────────────────
        fmt_requests = (
            _cell_color_requests(sheet_id, rows)
            + [_header_text_color_request(sheet_id)]
            + [_freeze_row_request(sheet_id)]
            + _set_column_widths_request(sheet_id)
        )

        # Bold all summary rows
        for i, row in enumerate(rows):
            if row and "Summary" in str(row[0]):
                fmt_requests.append(_bold_row_request(sheet_id, i))

        spreadsheets_resource.batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": fmt_requests},
        ).execute()

        # ── Row grouping (collapsible months) ────────────────────────────────
        # Read existing group start indices BEFORE clearing — used to detect
        # which months are new so only they get collapsed.
        existing_starts = _get_existing_group_starts(spreadsheets_resource, spreadsheet_id, sheet_id)

        # Clear all existing groups then re-add fresh ones
        _clear_all_row_groups(spreadsheets_resource, spreadsheet_id, sheet_id)

        group_requests = [
            _add_row_group_request(sheet_id, g["start_row"], g["end_row"])
            for g in month_groups
        ]

        if group_requests:
            spreadsheets_resource.batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"requests": group_requests},
            ).execute()

            # Collapse groups that are new (not previously in the sheet)
            # or all groups if this is the first time the sheet is created.
            # Existing months preserve the user's expand/collapse state.
            if is_new:
                # First creation — collapse everything
                new_groups = month_groups
            else:
                # Only collapse months whose group did not exist before
                new_groups = [
                    g for g in month_groups
                    if g["start_row"] not in existing_starts
                ]

            if new_groups:
                collapse_requests = [
                    _collapse_row_group_request(sheet_id, g["start_row"], g["end_row"])
                    for g in new_groups
                ]
                spreadsheets_resource.batchUpdate(
                    spreadsheetId=spreadsheet_id,
                    body={"requests": collapse_requests},
                ).execute()
                log.info("Collapsed %d new month group(s)", len(new_groups))

        log.info(
            "Google Sheet updated — %d rows written to '%s' tab  |  "
            "https://docs.google.com/spreadsheets/d/%s",
            len(rows), sheet_title, spreadsheet_id,
        )

    except Exception as e:
        log.error("Failed to update Google Sheet: %s", e)
