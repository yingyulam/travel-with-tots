"""Date/age utilities, independent of any storage layer."""

from datetime import date

# Which months count as summer for a venue's opening hours. Vancouver's own
# park washroom data splits the year this way, and attraction hours follow the
# same shape: longer in the light months, shorter once it is dark by five.
# A constant rather than a literal so the boundary is arguable in one place.
SUMMER_MONTHS = (5, 6, 7, 8, 9)

SEASONS = ("summer", "winter")
DAY_TYPES = ("weekday", "weekend")


def compute_age(date_of_birth, on=None):
    """Age as (years, months) from an ISO 'YYYY-MM-DD' date of birth, on a given
    date (default today). Age is derived here, never stored."""
    dob = date.fromisoformat(date_of_birth)
    on = on or date.today()
    months = (on.year - dob.year) * 12 + (on.month - dob.month) - (on.day < dob.day)
    return months // 12, months % 12


def season_for(on):
    """"summer" or "winter" for a date, for picking a venue's hours."""
    return "summer" if on.month in SUMMER_MONTHS else "winter"


def day_type_for(on):
    """"weekend" for Saturday or Sunday, otherwise "weekday".

    Public holidays are not handled: a venue on a holiday Monday may keep its
    Sunday hours, and nothing here knows the calendar. Named rather than
    silently wrong, because a plan built on weekday hours for a closed Monday is
    a confidently wrong plan.
    """
    return "weekend" if on.weekday() >= 5 else "weekday"


def parse_date(value, default=None):
    """An ISO date, or `default` (today when not given) if it is unusable.

    A bad date must not cost the parent their plan: it degrades to today, which
    is the same thing an absent date does.
    """
    try:
        return date.fromisoformat((value or "").strip())
    except (TypeError, ValueError):
        return default or date.today()


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
