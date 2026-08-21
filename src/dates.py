"""Date/age utilities, independent of any storage layer."""

from datetime import date


def compute_age(date_of_birth, on=None):
    """Age as (years, months) from an ISO 'YYYY-MM-DD' date of birth, on a given
    date (default today). Age is derived here, never stored."""
    dob = date.fromisoformat(date_of_birth)
    on = on or date.today()
    months = (on.year - dob.year) * 12 + (on.month - dob.month) - (on.day < dob.day)
    return months // 12, months % 12
