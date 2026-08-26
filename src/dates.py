"""Date/age utilities, independent of any storage layer."""

from datetime import date


def compute_age(date_of_birth, on=None):
    """Age as (years, months) from an ISO 'YYYY-MM-DD' date of birth, on a given
    date (default today). Age is derived here, never stored."""
    dob = date.fromisoformat(date_of_birth)
    on = on or date.today()
    months = (on.year - dob.year) * 12 + (on.month - dob.month) - (on.day < dob.day)
    return months // 12, months % 12


def format_age(years, months):
    """An age in words, for showing a parent what was recalled about their own
    child. Singular where it should be, and the zero half dropped, because
    "2 years 0 months" reads like a form field rather than a sentence."""
    parts = []
    if years:
        parts.append(f"{years} year" + ("s" if years != 1 else ""))
    if months or not parts:
        parts.append(f"{months} month" + ("s" if months != 1 else ""))
    return " ".join(parts)
