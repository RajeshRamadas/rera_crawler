"""Stage 1 — Geo Primitives: distance band assignment.

Implements: GEO-02. Bands per requirements doc §6:
  immediate 0-2km, daily 2-5km, city 5-10km, strategic 10-20km.
Outside the default 20km radius returns None (excluded from scoring per GEO-01).
"""
from typing import Optional

BANDS = [
    (0, 2, "immediate"),
    (2, 5, "daily"),
    (5, 10, "city"),
    (10, 20, "strategic"),
]


def distance_band(distance_km: float) -> Optional[str]:
    """Bucket a distance in km into exactly one band, or None if outside 20km."""
    for lo, hi, name in BANDS:
        if lo <= distance_km < hi:
            return name
    return None
