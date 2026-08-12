"""Stage 1 — Geo Primitives: haversine distance.

Implements: GEO-04 (distance computed in-house, never sourced from a third party).
"""
from math import radians, sin, cos, asin, sqrt

EARTH_RADIUS_KM = 6371.0088  # mean Earth radius, per design doc §5.1


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two lat/lon points, in kilometers."""
    p1, p2 = radians(lat1), radians(lat2)
    dphi = radians(lat2 - lat1)
    dlambda = radians(lon2 - lon1)
    a = sin(dphi / 2) ** 2 + cos(p1) * cos(p2) * sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * asin(sqrt(a))
