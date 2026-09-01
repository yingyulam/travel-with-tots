"""Date/age utilities, independent of any storage layer."""

from datetime import date, timedelta

# Which months count as summer for a venue's opening hours. Vancouver's own
# park washroom data splits the year this way, and attraction hours follow the
# same shape: longer in the light months, shorter once it is dark by five.
# A constant rather than a literal so the boundary is arguable in one place.


# A holiday is its own day type, not a weekday that happens to be quiet. Most
# attractions keep different hours or shut entirely, and guessing weekday hours
# for Christmas Day is exactly the confidently wrong answer to avoid.
DAY_TYPES = ("weekday", "weekend", "holiday")


def compute_age(date_of_birth, on=None):
    """Age as (years, months) from an ISO 'YYYY-MM-DD' date of birth, on a given
    date (default today). Age is derived here, never stored."""
    dob = date.fromisoformat(date_of_birth)
    on = on or date.today()
    months = (on.year - dob.year) * 12 + (on.month - dob.month) - (on.day < dob.day)
    return months // 12, months % 12


def _easter(year):
    """Easter Sunday, by the anonymous Gregorian algorithm.

    Needed because Good Friday is a BC statutory holiday and moves each year.
    Computed rather than tabulated so the calendar never goes stale.
    """
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f, g = (b + 8) // 25, (b - (b + 8) // 25 + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _nth_weekday(year, month, weekday, nth):
    """The nth given weekday of a month, e.g. the third Monday in February."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return date(year, month, 1 + offset + 7 * (nth - 1))


def bc_holidays(year):
    """British Columbia's statutory holidays for a year, as a set of dates.

    Computed from their rules rather than listed, so it stays correct without
    anyone remembering to extend a table. Boxing Day and Easter Monday are not
    statutory here and are left out; a venue that closes on one needs a
    date-specific entry, which is a gap named in the README.
    """
    easter = _easter(year)
    return {
        date(year, 1, 1),                              # New Year's Day
        _nth_weekday(year, 2, 0, 3),                   # Family Day
        easter - timedelta(days=2),                    # Good Friday
        _victoria_day(year),                           # Victoria Day
        date(year, 7, 1),                              # Canada Day
        _nth_weekday(year, 8, 0, 1),                   # BC Day
        _nth_weekday(year, 9, 0, 1),                   # Labour Day
        date(year, 9, 30),                             # Truth and Reconciliation
        _nth_weekday(year, 10, 0, 2),                  # Thanksgiving
        date(year, 11, 11),                            # Remembrance Day
        date(year, 12, 25),                            # Christmas Day
    }


def _victoria_day(year):
    """The Monday before 25 May."""
    may25 = date(year, 5, 25)
    return may25 - timedelta(days=(may25.weekday() - 0) % 7 or 7)


def day_type_for(on):
    """"holiday", "weekend" or "weekday" for a date.

    Holiday wins over the day of the week: Christmas Day on a Friday is not a
    weekday for a museum's purposes.
    """
    if on in bc_holidays(on.year):
        return "holiday"
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


# The longest trip the planner will lay out in one go. Each day costs its own
# AI adjustment call, run one after another, so a fortnight is a minute of
# somebody watching a spinner on a single Render worker. A week is also about as
# long as a family visit to one city runs.
MAX_TRIP_DAYS = 7


def date_range(start, end):
    """Every date from `start` to `end` inclusive, as ISO strings.

    A list rather than the pair it came from, deliberately: the form asks for
    two dates because a visit is contiguous, but the plan is a list of days, and
    keeping the list as the real value is what lets days be picked individually
    later without anything downstream noticing.

    Backwards, missing or absurd input degrades to a single day, the same way
    parse_date degrades to today: a bad date must not cost the parent a plan.
    """
    first = parse_date(start)
    last = parse_date(end, default=first)
    if last < first:
        last = first
    span = min((last - first).days, MAX_TRIP_DAYS - 1)
    return [(first + timedelta(days=n)).isoformat() for n in range(span + 1)]


def days_between(start, end):
    """How many days that range covers, before it is capped. For telling a
    parent their trip is too long rather than silently shortening it."""
    first = parse_date(start)
    last = parse_date(end, default=first)
    return max(1, (last - first).days + 1)


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
