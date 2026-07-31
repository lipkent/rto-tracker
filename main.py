#!/usr/bin/env python3
"""RTO Tracker — CLI entry point."""

import logging
import sys
from datetime import date

import click

from rto_tracker.config import (
    CONFIG_DIR, load_config, save_config, set_value,
)
from rto_tracker.holidays_helper import is_public_holiday, list_country_options


def _setup_logging(verbose: bool):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(CONFIG_DIR / "rto_tracker.log", encoding="utf-8"),
        ],
    )


@click.group()
@click.option("--verbose", "-v", is_flag=True, help="Enable debug logging")
@click.pass_context
def cli(ctx, verbose):
    """RTO Tracker."""
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose
    _setup_logging(verbose)


# ── Setup ────────────────────────────────────────────────────────────────────

@cli.command()
def setup():
    """Interactive first-time setup wizard."""
    click.echo("\n── RTO Tracker Setup ─────────────────────────────────────\n")

    cfg = load_config()

    # Country / timezone
    click.echo("Available countries:")
    options = list_country_options()
    for i, opt in enumerate(options, 1):
        click.echo(f"  {i:2d}. {opt['name']} ({opt['code']})  [{opt['timezone']}]")

    current_idx = next(
        (i for i, o in enumerate(options, 1) if o["code"] == cfg["country"]), 1
    )
    choice = click.prompt(
        f"Select country number [{current_idx}]", default=str(current_idx)
    )
    selected = options[int(choice) - 1]
    cfg["country"] = selected["code"]
    cfg["timezone"] = selected["timezone"]

    # WiFi SSID
    cfg["wifi_ssid"] = click.prompt("Office WiFi SSID", default=cfg["wifi_ssid"])

    # Datadog API key — stored in macOS Keychain, not config.json
    from rto_tracker.config import get_api_key, set_api_key, get_app_key, set_app_key

    # Datadog API key
    existing_key = get_api_key()
    new_key = click.prompt(
        "Datadog API key (leave blank to keep existing)" if existing_key else "Datadog API key (leave blank to skip)",
        default="********" if existing_key else "",
        hide_input=True,
    )
    if new_key and new_key != "********":
        set_api_key(new_key)
        click.echo("✓ Datadog API key stored securely in macOS Keychain")
    elif existing_key:
        click.echo("✓ Datadog API key unchanged")
    else:
        click.echo("⚠ No Datadog API key set — metrics push will be skipped")

    # Datadog App key (needed to auto-update dashboard month filter)
    existing_app_key = get_app_key()
    new_app_key = click.prompt(
        "Datadog App key (leave blank to keep existing)" if existing_app_key else "Datadog App key (leave blank to skip)",
        default="********" if existing_app_key else "",
        hide_input=True,
    )
    if new_app_key and new_app_key != "********":
        set_app_key(new_app_key)
        click.echo("✓ Datadog App key stored securely in macOS Keychain")
    elif existing_app_key:
        click.echo("✓ Datadog App key unchanged")
    else:
        click.echo("⚠ No Datadog App key set — dashboard month filter auto-update will be skipped")

    cfg["datadog_site"] = click.prompt("Datadog site", default=cfg.get("datadog_site", "datadoghq.com"))

    tags_input = click.prompt(
        "Datadog tags (comma-separated)", default=",".join(cfg.get("datadog_tags", ["office:ams"]))
    )
    cfg["datadog_tags"] = [t.strip() for t in tags_input.split(",") if t.strip()]

    # Datadog Dashboard ID (from dashboard URL: /dashboard/<id>)
    cfg["datadog_dashboard_id"] = click.prompt(
        "Datadog Dashboard ID (from dashboard URL, leave blank to skip)",
        default=cfg.get("datadog_dashboard_id", ""),
    )
    if cfg["datadog_dashboard_id"]:
        click.echo("✓ Dashboard ID saved — month filter will be auto-updated")
    else:
        click.echo("⚠ No Dashboard ID set — month filter auto-update will be skipped")

    # Google Calendar
    cfg["google_calendar_id"] = click.prompt(
        "Google Calendar ID", default=cfg.get("google_calendar_id", "primary")
    )

    save_config(cfg)
    click.echo(f"\nConfig saved to {CONFIG_DIR}/config.json")

    if not (CONFIG_DIR / "gcal_credentials.json").exists():
        click.echo(
            "\nGoogle Calendar credentials not found.\n"
            "  1. Go to https://console.cloud.google.com\n"
            "  2. Create a project, enable Google Calendar API\n"
            "  3. Create OAuth 2.0 credentials (Desktop app type)\n"
            f"  4. Download as JSON and save to: {CONFIG_DIR}/gcal_credentials.json\n"
            "  5. Run: python3 main.py sync   (to authorise & test)"
        )
    click.echo("\nSetup complete. Run:  python3 main.py start\n")


# ── Start daemon ─────────────────────────────────────────────────────────────

@cli.command()
@click.pass_context
def start(ctx):
    """Start the WiFi monitor and background scheduler."""
    from rto_tracker.scheduler import run
    click.echo("Starting RTO tracker (Ctrl+C to stop)…")
    run()


# ── Status ───────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--month", "-m", default=None, help="Month as YYYY-MM (default: current)")
def status(month):
    """Show attendance status for the current (or given) month."""
    from rto_tracker.calculations import calculate_month, current_month_block_for, format_report, build_month_block
    from rto_tracker.state import get_status, get_wifi_seconds
    from rto_tracker.holidays_helper import get_holiday_name

    cfg = load_config()
    country = cfg["country"]
    today = date.today()

    if month:
        year, mon = (int(p) for p in month.split("-"))
    else:
        year, mon = current_month_block_for(today)

    metrics = calculate_month(year, mon, today)
    click.echo(format_report(metrics))

    # Per-day table
    block = build_month_block(year, mon, country)
    click.echo("\n  Day         Status   WiFi(min)  Note")
    click.echo("  ──────────  ───────  ─────────  ────────────────────")
    for d in block.working_days:
        if is_public_holiday(d, country):
            note = get_holiday_name(d, country) or "Public holiday"
            click.echo(f"  {d}  {'OUT':7s}  {'':9s}  {note}")
            continue
        st = get_status(d) or "pending"
        wifi = get_wifi_seconds(d) / 60 if d <= today else 0
        marker = "◀ today" if d == today else ""
        click.echo(f"  {d}  {st:7s}  {wifi:>9.0f}  {marker}")


# ── Override ─────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("status_value", metavar="STATUS", type=click.Choice(["office", "wfh", "out"]))
@click.option("--date", "-d", "day", default=None, help="Single date as YYYY-MM-DD (default: today)")
@click.option("--from", "date_from", default=None, help="Start of date range as YYYY-MM-DD")
@click.option("--to", "date_to", default=None, help="End of date range as YYYY-MM-DD (inclusive)")
def override(status_value, day, date_from, date_to):
    """Manually set status for a day or a date range.

    Single day:   rto override office --date 2026-05-01
    Date range:   rto override office --from 2026-05-01 --to 2026-05-05
    """
    from rto_tracker.state import manual_override
    from rto_tracker.datadog_client import push_all_months
    from datetime import timedelta

    # Determine list of dates to override
    if date_from and date_to:
        start = date.fromisoformat(date_from)
        end   = date.fromisoformat(date_to)
        if start > end:
            click.echo("Error: --from date must be before or equal to --to date.", err=True)
            sys.exit(1)
        days = []
        d = start
        while d <= end:
            if d.weekday() < 5:   # skip weekends
                days.append(d)
            d += timedelta(days=1)
    elif date_from or date_to:
        click.echo("Error: please provide both --from and --to for a date range.", err=True)
        sys.exit(1)
    else:
        days = [date.fromisoformat(day) if day else date.today()]

    # Default WiFi to 120 min when using date range override for office days
    wifi_seconds = 7200 if (date_from and date_to and status_value == "office") else 0

    for d in days:
        manual_override(d, status_value, wifi_seconds=wifi_seconds)
        wifi_note = "  (WiFi set to 120 min)" if wifi_seconds > 0 else ""
        click.echo(f"Override set: {d} → {status_value}{wifi_note}")

    click.echo(f"\nTotal: {len(days)} day(s) updated.")

    # Push updated metrics
    try:
        push_all_months()
        click.echo("Metrics pushed to Datadog.")
    except Exception as e:
        click.echo(f"Warning: metrics push failed: {e}")


# ── Sync ─────────────────────────────────────────────────────────────────────

@cli.command()
def sync():
    """Run Google Calendar sync and backfill absence days."""
    from rto_tracker.calendar_sync import run_sync
    from rto_tracker.datadog_client import push_all_months

    click.echo("Running Google Calendar sync…")
    try:
        changes = run_sync()
    except Exception as e:
        click.echo(f"Sync failed: {e}", err=True)
        sys.exit(1)

    if changes:
        click.echo(f"Backfilled {len(changes)} day(s):")
        for ch in changes:
            click.echo(f"  {ch['date']}  {ch['previous'] or 'unset'} → {ch['new']}")
        push_all_months()
        click.echo("Metrics pushed.")
    else:
        click.echo("No changes.")


# ── Config ───────────────────────────────────────────────────────────────────

@cli.group()
def config():
    """View or update configuration."""


@config.command("show")
def config_show():
    """Print current configuration."""
    import json
    click.echo(json.dumps(load_config(), indent=2))


@config.command("set")
@click.argument("key")
@click.argument("value")
def config_set(key, value):
    """Set a config key to value."""
    set_value(key, value)
    click.echo(f"Set {key} = {value}")


# ── Report ───────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--month", "-m", default=None, help="Month as YYYY-MM (default: current)")
def report(month):
    """Print a full monthly attendance report."""
    from rto_tracker.calculations import calculate_month, current_month_block_for, format_report

    today = date.today()
    if month:
        year, mon = (int(p) for p in month.split("-"))
    else:
        year, mon = current_month_block_for(today)

    metrics = calculate_month(year, mon, today)
    click.echo(format_report(metrics))


# ── Push ─────────────────────────────────────────────────────────────────────

@cli.command()
def push():
    """Manually push metrics to Datadog right now."""
    from rto_tracker.datadog_client import push_all_months

    click.echo("Pushing metrics to Datadog…")
    push_all_months()
    click.echo("Done.")


# ── Export ────────────────────────────────────────────────────────────────────

@cli.command()
def export():
    """Manually export RTO status report to Google Sheets."""
    from rto_tracker.gdrive_export import update_sheet

    click.echo("Exporting RTO status to Google Sheets…")
    try:
        update_sheet()
        click.echo("Done. Open Google Drive and look for 'RTO Tracker'.")
    except Exception as e:
        click.echo(f"Export failed: {e}", err=True)
        sys.exit(1)


# ── Install LaunchAgent ───────────────────────────────────────────────────────

@cli.command()
def install():
    """Install as a macOS LaunchAgent so it starts automatically at login."""
    import os
    import stat
    import subprocess
    from pathlib import Path

    python = sys.executable
    script = Path(__file__).resolve()
    plist_dir = Path.home() / "Library" / "LaunchAgents"
    plist_path = plist_dir / "com.datadog.rto-tracker.plist"

    plist_dir.mkdir(parents=True, exist_ok=True)

    plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.datadog.rto-tracker</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python}</string>
        <string>{script}</string>
        <string>start</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{CONFIG_DIR}/launchd_stdout.log</string>
    <key>StandardErrorPath</key>
    <string>{CONFIG_DIR}/launchd_stderr.log</string>
    <key>WorkingDirectory</key>
    <string>{script.parent}</string>
</dict>
</plist>
"""
    plist_path.write_text(plist_content)
    click.echo(f"LaunchAgent written to {plist_path}")

    result = subprocess.run(
        ["launchctl", "load", "-w", str(plist_path)],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        click.echo("LaunchAgent loaded — tracker will start at every login.")
    else:
        click.echo(f"launchctl load failed: {result.stderr.strip()}")
        click.echo(f"You can manually load it with:  launchctl load -w {plist_path}")


@cli.command()
def uninstall():
    """Unload and remove the macOS LaunchAgent."""
    import subprocess
    from pathlib import Path

    plist_path = Path.home() / "Library" / "LaunchAgents" / "com.datadog.rto-tracker.plist"
    if plist_path.exists():
        subprocess.run(["launchctl", "unload", str(plist_path)])
        plist_path.unlink()
        click.echo("LaunchAgent unloaded and removed.")
    else:
        click.echo("LaunchAgent not found.")


if __name__ == "__main__":
    cli()
