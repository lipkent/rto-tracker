"""Public holiday lookups using the `holidays` library."""

import logging
from datetime import date
from functools import lru_cache

import holidays as hol

from .config import COUNTRY_OPTIONS

log = logging.getLogger(__name__)


@lru_cache(maxsize=32)
def _get_holiday_set(country: str, year: int) -> set[date]:
    """Return the set of public holiday dates for *country* and *year*.

    Datadog NL override: Liberation Day (May 5) is always a company holiday
    regardless of whether it is a lustrum year.
    """
    try:
        h = hol.country_holidays(country, years=year)
        holiday_set = set(h.keys())

        # Datadog NL — always include Liberation Day (May 5)
        if country == "NL":
            holiday_set.add(date(year, 5, 5))

        return holiday_set
    except Exception as e:
        log.warning("Could not load holidays for %s/%d: %s", country, year, e)
        return set()


def is_public_holiday(d: date, country: str) -> bool:
    return d in _get_holiday_set(country, d.year)


def get_holiday_name(d: date, country: str) -> str:
    try:
        h = hol.country_holidays(country, years=d.year)
        return h.get(d, "")
    except Exception:
        return ""


def list_country_options() -> list[dict]:
    return [
        {"code": code, "name": info["name"], "timezone": info["timezone"]}
        for code, info in COUNTRY_OPTIONS.items()
    ]
