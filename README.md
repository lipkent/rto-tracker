# RTO Tracker

A macOS background tool that monitors office attendance via office WiFi detection, Google Calendar sync, and automatic Datadog metric pushing. Tracks your monthly in-office days against the 60% RTO target and surfaces 13 metrics to a Datadog dashboard.

---

## Prerequisites

- macOS (Apple Silicon / M-series chip)
- Python 3.11 or later
- Homebrew
- Datadog account with API key and App key
- Google Workspace account (Datadog Google account)
- Access to [Google Cloud Console](https://console.cloud.google.com)

---

## Quick Start

1. **Clone the repo**
   ```bash
   git clone https://github.com/lipkent/rto-tracker.git
   cd rto-tracker
   ```

2. **Run the setup script** — verifies Python, creates venv, installs deps, launches wizard
   ```bash
   bash setup.sh
   ```

3. **Import the Datadog dashboard** — paste `datadog_dashboard.json` into Datadog → Dashboards → New Dashboard → gear → Import JSON. Note your Dashboard ID from the URL.

4. **Run the setup wizard** — enter country, WiFi SSID, Datadog keys, site, tags, Dashboard ID, and Calendar ID
   ```bash
   python3 main.py setup
   ```

5. **Set up Google Calendar OAuth** — download credentials from Google Cloud Console → save to `~/.rto_tracker/gcal_credentials.json`, then authorise:
   ```bash
   source .venv/bin/activate && python3 main.py sync
   ```

> 📖 For the complete step-by-step guide — see the [Setup Guide on Confluence](https://datadoghq.atlassian.net/wiki/x/pQGKmwE) or [RTO Tracker - Setup Guide.docx](./RTO%20Tracker%20-%20Setup%20Guide.docx).

---

## How It Works

- **WiFi monitor** — polls SSID every 60 s; marks day as office after 120 min on the office network
- **Scheduler thread** — calendar sync hourly, metrics push every 10 min, daily backfill at 00:05, EOD default at 23:59
- **Day status priority** — WiFi confirmed → manual override → Google Calendar → EOD default
- **Split-week rule** — when a week straddles two months, all 5 days are assigned to whichever month contains the majority (e.g. Mon–Tue in June + Wed–Fri in July → all 5 days count in July)
- **Dashboard month filter** — auto-updated when the month block changes

---

## Setup Guide

Full step-by-step instructions: **[RTO Tracker - Setup Guide.docx](./RTO%20Tracker%20-%20Setup%20Guide.docx)** or **[Confluence](https://datadoghq.atlassian.net/wiki/x/pQGKmwE)**

Covers: one-time setup (12 steps including Datadog dashboard import), Google OAuth, alias configuration, LaunchAgent install, and troubleshooting.

---

## Available Commands

| Command | Description |
|---|---|
| `rto status` | Show attendance report + WiFi minutes per day |
| `rto sync` | Sync Google Calendar and backfill absence days |
| `rto push` | Manually push metrics to Datadog + update Google Sheet |
| `rto report` | Full monthly attendance report |
| `rto export` | Manually export RTO status to Google Sheets |
| `rto override office --date 2026-06-09` | Override a single day |
| `rto override office --from 2026-05-01 --to 2026-05-15` | Override a date range |
| `rto config show` | Print current configuration |
| `rto config set KEY VALUE` | Update a config value |
| `rto install` | Register as macOS LaunchAgent |
| `rto uninstall` | Remove the LaunchAgent |

---

## Dashboard

Import `datadog_dashboard.json` into Datadog. Configure the `month` template variable (tag: `month`, default: current month, e.g. `2026-07`). The script auto-updates this filter each month.

The dashboard includes 13 widgets covering attendance %, target status, days in/out, WiFi minutes, and trend charts.

---

## Metrics

| Metric | Description |
|---|---|
| `rto.attendance_pct` | Current month attendance percentage |
| `rto.target_met` | 0=no, 1=yes, 2=pending |
| `rto.expected_days` | Days required in office this month |
| `rto.remaining_working_days` | Working days left |
| `rto.in_office_days` | Days confirmed in office |
| `rto.days_still_needed` | Days still needed to hit target |
| `rto.wifi_minutes_today` | Minutes on office WiFi today |
| `rto.status_today` | 0=wfh, 1=office, 2=out, 3=pending |
| `rto.in_office_today` | 1 if in office today |
| `rto.can_make_target` | 1 if target is still achievable |
| `rto.base_working_days` | Total working days in the month |
| `rto.out_days` | Days out (PTO, sick, public holidays) |
| `rto.adjusted_working_days` | Working days minus out days |

---

## Security

- Datadog API key and App key are stored in **macOS Keychain** — never written to disk in plaintext
- Google OAuth tokens are stored in `~/.rto_tracker/` (local only, not committed)
- `gcal_credentials.json` is gitignored — never commit it

---

## Troubleshooting

**pip/python not found after setup.sh:**
```bash
deactivate
rm -rf .venv
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 main.py setup
```

**Tracker not running:**
```bash
launchctl list | grep rto        # check if registered
launchctl start com.datadog.rto-tracker
```

**Stop permanently:**
```bash
python3 main.py uninstall
```
