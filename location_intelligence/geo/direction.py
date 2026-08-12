"""Stage 1 — Geo Primitives: 16-point compass direction from a bearing.

Implements: GEO-03.
"""

COMPASS_16 = [
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
]


def compass_16(bearing_deg: float) -> str:
    """Map a bearing in degrees [0, 360) to one of the 16 compass points."""
    idx = round(bearing_deg / 22.5) % 16
    return COMPASS_16[idx]
