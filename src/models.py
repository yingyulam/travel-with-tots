"""Domain objects for the two-page flow.

``Plan``  — one candidate day (theme label, blurb, ordered stops). The planning
            page compares several of these; ``generate_plans`` produces them.
``Trip``  — created when the parent picks a plan to start. It holds the chosen
            plan as the immutable original plus any re-planned versions and the
            current time, and is what the in-trip page renders.

Both convert to/from plain dicts so they can travel through templates
(``|tojson``) and the JSON re-plan endpoint unchanged.
"""

from dataclasses import dataclass, field


@dataclass
class Plan:
    """A single candidate day: a theme label, a blurb, and ordered stops."""

    label: str
    blurb: str
    stops: list  # list of stop dicts: {time, kind, venue, reason}

    def preview(self, limit=3):
        """First few stops, for the comparison card on the planning page."""
        return self.stops[:limit]

    def to_dict(self):
        return {"label": self.label, "blurb": self.blurb, "stops": self.stops}

    @classmethod
    def from_dict(cls, data):
        return cls(
            label=data.get("label", "Plan"),
            blurb=data.get("blurb", ""),
            stops=data.get("stops", []),
        )


@dataclass
class Trip:
    """A chosen plan in progress.

    ``versions`` always starts with the original plan at index 0; re-planned
    versions are appended as the day unfolds. ``current_time`` is an 'HH:MM'
    string (blank means "let the page default it to the local clock").
    """

    destination: str
    transit: list
    original: Plan
    current_time: str = ""
    features: list = field(default_factory=list)
    versions: list = field(default_factory=list)

    def __post_init__(self):
        if not self.versions:
            self.versions = [self.original]

    def add_version(self, plan):
        """Append a re-planned version and return its index."""
        self.versions.append(plan)
        return len(self.versions) - 1

    def to_dict(self):
        return {
            "destination": self.destination,
            "transit": self.transit,
            "current_time": self.current_time,
            "features": self.features,
            "plans": [p.to_dict() for p in self.versions],
        }
