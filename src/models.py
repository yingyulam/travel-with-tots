"""Domain objects for the two-page flow.

``Plan``: one candidate day (label, blurb, ordered stops). The planning
            page compares several of these; ``generate_plans`` produces them.
``Day``:  one date within a trip, holding that date's plan plus any re-planned
            versions of it, and where the family is staying that night.
``Trip``: created when the parent picks a plan to start. A list of days and the
            answers they share, and what the in-trip page renders.

A one-day trip is a trip with one day. There is no separate single-day shape,
because a second shape is a second set of bugs: every reader iterates ``days``
whether there is one of them or seven.

All three convert to/from plain dicts so they can travel through templates
(``|tojson``) and the JSON re-plan endpoint unchanged.
"""

from dataclasses import dataclass, field


@dataclass
class Plan:
    """A single candidate day: a label, a blurb, and ordered stops."""

    label: str
    blurb: str
    stops: list  # list of stop dicts: {time, kind, venue, reason}
    source: str = "rule"  # "rule" or "ai" -- which planner produced this

    def preview(self, limit=2):
        """First few stops, for the comparison card on the planning page."""
        return self.stops[:limit]

    def remaining(self, limit=2):
        """Stops past the preview, revealed by the "+N more" link."""
        return self.stops[limit:]

    def to_dict(self):
        return {"label": self.label, "blurb": self.blurb, "stops": self.stops,
                "source": self.source}

    @classmethod
    def from_dict(cls, data):
        return cls(
            label=data.get("label", "Plan"),
            blurb=data.get("blurb", ""),
            stops=data.get("stops", []),
            source=data.get("source", "rule"),
        )


@dataclass
class Day:
    """One date within a trip: its plan, and where they are staying that night.

    ``versions`` always starts with that day's original plan at index 0;
    a re-planned version is added beside it, and nothing replaces the original,
    so a parent can always see what they are being offered instead of.

    The accommodation fields are per day even though the form currently asks
    once and writes the same answer to every day. They sit here rather than on
    the Trip because that is what makes "a different hotel on Thursday" a form
    change later instead of a model change: the planner already measures from
    whatever point it is handed (see itinerary.travel_rules).
    """

    date: str
    original: Plan
    index: int = 0
    accommodation: str = ""
    accommodation_lat: float = None
    accommodation_lng: float = None
    versions: list = field(default_factory=list)
    trip_id: int = None  # the saved row this day came from, when it was saved

    def __post_init__(self):
        if not self.versions:
            self.versions = [self.original]

    def add_version(self, plan):
        """Add a re-planned version of this day and return its index."""
        self.versions.append(plan)
        return len(self.versions) - 1

    def venue_names(self, index=0):
        """Every venue this day's plan visits, for keeping the next day off
        them. Reads one version, defaulting to the original."""
        plan = self.versions[index] if index < len(self.versions) else self.original
        return [stop["venue"]["name"] for stop in plan.stops if stop.get("venue")]

    def to_dict(self):
        return {
            "date": self.date,
            "index": self.index,
            "accommodation": self.accommodation,
            "accommodation_lat": self.accommodation_lat,
            "accommodation_lng": self.accommodation_lng,
            "trip_id": self.trip_id,
            "plans": [p.to_dict() for p in self.versions],
        }

    @classmethod
    def from_dict(cls, data):
        plans = [Plan.from_dict(p) for p in data.get("plans") or []]
        return cls(
            date=data.get("date", ""),
            original=plans[0] if plans else Plan.from_dict({}),
            index=data.get("index", 0),
            accommodation=data.get("accommodation", ""),
            accommodation_lat=data.get("accommodation_lat"),
            accommodation_lng=data.get("accommodation_lng"),
            versions=plans,
            trip_id=data.get("trip_id"),
        )


@dataclass
class Trip:
    """A chosen trip in progress: its days, and the answers they share.

    ``current_time`` is an 'HH:MM' string (blank means "let the page default it
    to the local clock"). ``trip_date`` is the first day, kept because a saved
    row, a replan and the chat extractor all describe a single date and none of
    them need to know a trip can be longer.
    """

    destination: str
    transit: str
    days: list = field(default_factory=list)
    current_time: str = ""
    features: list = field(default_factory=list)
    trip_date: str = ""
    bedtime: str = ""
    age_months: int = 0
    dining: str = ""
    nap_notes: str = ""
    extra_notes: str = ""
    group_id: str = ""

    def __post_init__(self):
        if not self.trip_date and self.days:
            self.trip_date = self.days[0].date

    @property
    def is_multi_day(self):
        return len(self.days) > 1

    def day(self, index):
        """One day by position, or None. Position, not date: two days cannot
        share an index, and a malformed date should not lose a day."""
        return self.days[index] if 0 <= index < len(self.days) else None

    def to_dict(self):
        return {
            "destination": self.destination,
            "transit": self.transit,
            "current_time": self.current_time,
            "features": self.features,
            "trip_date": self.trip_date,
            "bedtime": self.bedtime,
            "age_months": self.age_months,
            "dining": self.dining,
            "nap_notes": self.nap_notes,
            "extra_notes": self.extra_notes,
            "group_id": self.group_id,
            "days": [d.to_dict() for d in self.days],
        }
