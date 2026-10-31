"""Stage 1 — Geo Primitives: initial bearing between two points.

Implements: GEO-03 (numeric bearing 0-360 degrees).
"""
from math import radians, degrees, sin, cos, atan2


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial bearing from point 1 to point 2, in degrees, normalized to [0, 360)."""
    p1, p2 = radians(lat1), radians(lat2)
    dlambda = radians(lon2 - lon1)
    x = sin(dlambda) * cos(p2)
    y = cos(p1) * sin(p2) - sin(p1) * cos(p2) * cos(dlambda)
    return (degrees(atan2(x, y)) + 360) % 360
