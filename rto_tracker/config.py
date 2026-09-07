"""Configuration management for RTO tracker."""

import json
import logging
import subprocess
import time
from pathlib import Path

log = logging.getLogger(__name__)

# Short-TTL cache for load_config() — config.json + Keychain rarely change,
# but load_config() is called every ~30-60s from background loops (WiFi tick,
# scheduler thread), each spawning a "security find-generic-password"
# subprocess. Caching for a few seconds eliminates most of that churn while
# still picking up changes made via `rto config set` / `rto setup` quickly
# (those explicitly invalidate the cache below).
_config_cache: dict | None = None
_config_cache_time: float | None = None
_CONFIG_CACHE_TTL = 15  # seconds


def _invalidate_config_cache():
    global _config_cache, _config_cache_time
    _config_cache = None
    _config_cache_time = None


# ── macOS Keychain constants ──────────────────────────────────────────────────
_KEYCHAIN_SERVICE     = "com.datadog.rto-tracker"
_KEYCHAIN_ACCOUNT     = "datadog_api_key"
_KEYCHAIN_ACCOUNT_APP = "datadog_app_key"

CONFIG_DIR = Path.home() / ".rto_tracker"
CONFIG_FILE = CONFIG_DIR / "config.json"
STATE_FILE = CONFIG_DIR / "state.json"
GCAL_TOKEN_FILE = CONFIG_DIR / "gcal_token.json"
GCAL_CREDENTIALS_FILE = CONFIG_DIR / "gcal_credentials.json"

DEFAULTS = {
    "country": "NL",
    "timezone": "Europe/Amsterdam",
    "wifi_ssid": "wi-fido",
    "wifi_threshold_minutes": 120,
    "rto_target_pct": 0.60,
    # datadog_api_key and datadog_app_key stored in macOS Keychain
    "datadog_site": "datadoghq.com",
    "datadog_tags": ["office:ams"],
    "datadog_dashboard_id": "",
    "google_calendar_id": "primary",
    "calendar_lookback_days": 7,
    "absence_keywords": [
        "out of office", "time off", "pto", "sick", "leave",
        "absence", "holiday", "vacation"
    ],
}

# Country code → holidays library country + subdivision mapping
COUNTRY_OPTIONS = {
    "NL": {"name": "Netherlands", "timezone": "Europe/Amsterdam"},
    "US": {"name": "United States", "timezone": "America/New_York"},
    "GB": {"name": "United Kingdom", "timezone": "Europe/London"},
    "DE": {"name": "Germany", "timezone": "Europe/Berlin"},
    "FR": {"name": "France", "timezone": "Europe/Paris"},
    "ES": {"name": "Spain", "timezone": "Europe/Madrid"},
    "IT": {"name": "Italy", "timezone": "Europe/Rome"},
    "AU": {"name": "Australia", "timezone": "Australia/Sydney"},
    "CA": {"name": "Canada", "timezone": "America/Toronto"},
    "JP": {"name": "Japan", "timezone": "Asia/Tokyo"},
    "SG": {"name": "Singapore", "timezone": "Asia/Singapore"},
    "IN": {"name": "India", "timezone": "Asia/Kolkata"},
    "IE": {"name": "Ireland", "timezone": "Europe/Dublin"},
}


# ── Keychain helpers ──────────────────────────────────────────────────────────

def get_api_key() -> str:
    """Read the Datadog API key from macOS Keychain. Returns empty string if not set."""
    result = subprocess.run(
        [
            "security", "find-generic-password",
            "-a", _KEYCHAIN_ACCOUNT,
            "-s", _KEYCHAIN_SERVICE,
            "-w",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        key = result.stdout.strip()
        if key:
            return key
    log.debug("Datadog API key not found in Keychain")
    return ""


def set_api_key(api_key: str):
    """Store the Datadog API key in macOS Keychain (creates or updates)."""
    result = subprocess.run(
        [
            "security", "add-generic-password",
            "-a", _KEYCHAIN_ACCOUNT,
            "-s", _KEYCHAIN_SERVICE,
            "-w", api_key,
            "-U",   # update if already exists
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to store API key in Keychain: {result.stderr.strip()}"
        )
    _invalidate_config_cache()
    log.info("Datadog API key stored securely in macOS Keychain")



def get_app_key() -> str:
    """Read the Datadog App key from macOS Keychain. Returns empty string if not set."""
    result = subprocess.run(
        [
            "security", "find-generic-password",
            "-a", _KEYCHAIN_ACCOUNT_APP,
            "-s", _KEYCHAIN_SERVICE,
            "-w",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        key = result.stdout.strip()
        if key:
            return key
    log.debug("Datadog App key not found in Keychain")
    return ""


def set_app_key(app_key: str):
    """Store the Datadog App key in macOS Keychain (creates or updates)."""
    result = subprocess.run(
        [
            "security", "add-generic-password",
            "-a", _KEYCHAIN_ACCOUNT_APP,
            "-s", _KEYCHAIN_SERVICE,
            "-w", app_key,
            "-U",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to store App key in Keychain: {result.stderr.strip()}"
        )
    _invalidate_config_cache()
    log.info("Datadog App key stored securely in macOS Keychain")



def ensure_dirs():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def load_config() -> dict:
    global _config_cache, _config_cache_time

    now = time.monotonic()
    if (_config_cache is not None and _config_cache_time is not None
            and (now - _config_cache_time) < _CONFIG_CACHE_TTL):
        return dict(_config_cache)   # shallow copy — callers may pop()/mutate top-level keys

    ensure_dirs()
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            saved = json.load(f)

        # One-time migration: move plain text keys to Keychain
        changed = False
        if saved.get("datadog_api_key"):
            log.info("Migrating Datadog API key from config.json to macOS Keychain")
            set_api_key(saved.pop("datadog_api_key"))
            changed = True
        if saved.get("datadog_app_key"):
            log.info("Migrating Datadog App key from config.json to macOS Keychain")
            set_app_key(saved.pop("datadog_app_key"))
            changed = True
        if changed:
            with open(CONFIG_FILE, "w") as f:
                json.dump(saved, f, indent=2)

        cfg = {**DEFAULTS, **saved}
    else:
        cfg = dict(DEFAULTS)

    # Always inject the API key from Keychain at runtime
    cfg["datadog_api_key"] = get_api_key()

    _config_cache = dict(cfg)
    _config_cache_time = now
    return cfg


def save_config(cfg: dict):
    ensure_dirs()
    _invalidate_config_cache()
    # Extract API key before saving — store in Keychain, not on disk
    api_key = cfg.pop("datadog_api_key", None)
    if api_key:
        set_api_key(api_key)
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)
    # Restore key in the in-memory dict so callers can still use it
    if api_key:
        cfg["datadog_api_key"] = api_key


def get(key: str, default=None):
    return load_config().get(key, default)


def set_value(key: str, value):
    cfg = load_config()
    cfg[key] = value
    save_config(cfg)
