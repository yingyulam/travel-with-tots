"""Filter venues by the family-friendly features a parent cares about."""

from .data_loader import FEATURE_KEYS


def filter_by_features(venues, selected_features):
    """Return only venues that have every one of the selected features.

    ``selected_features`` is a list of feature keys (a subset of
    ``FEATURE_KEYS``). A venue matches when all of those keys are True on it.
    With no features selected, every venue matches.
    """
    wanted = [f for f in selected_features if f in FEATURE_KEYS]
    return [v for v in venues if all(v.get(key) for key in wanted)]
