"""Stage 6 — Scoring.

Implements: SCORE-01, SCORE-02, SCORE-03.

Pure function of project_pois — no external calls, so a re-score after a
taxonomy or weight change is a deterministic replay (design doc §3.4).
Model parameters live in a versioned config keyed by model_version, never
hardcoded inline, so two model versions can run side by side (SCORE-03).
"""
from statistics import mean
from typing import Optional

import psycopg2

TAXONOMY_CATEGORIES = [
    "transport", "healthcare", "education", "employment",
    "shopping", "lifestyle", "civic", "hospitality", "future",
]

# Versioned model configs (SCORE-03). Weight/diversity constants here are
# reasonable starting points, not final — design doc §14 explicitly defers
# tuning these to the Benchmark-stage rollout (500 real properties), not
# fixed upfront. Add a new key (e.g. "location-v2") to run two models
# side by side during a migration rather than mutating this one in place.
MODEL_CONFIGS = {
    "location-v1": {
        "radius_km": 20,
        "category_weight": 20.0,   # per-POI weight before proximity decay
        "diversity_weight": 0.15,  # SCORE-02: reward category spread over concentration
    },
    # A second version, differing only in diversity_weight, exists so
    # SCORE-03's "two model versions can run side by side" claim is
    # actually exercised (see tests/test_map_services.py's cache
    # invalidation test) rather than asserted with only one version ever
    # having existed.
    "location-v2": {
        "radius_km": 20,
        "category_weight": 20.0,
        "diversity_weight": 0.25,
    },
}


def poi_contribution(distance_km: float, category_weight: float, radius_km: float, quality_factor: float = 1.0) -> float:
    """Design doc §5.4: linear proximity decay to 0 at the radius edge,
    scaled by category weight and a quality factor (POI classification
    confidence from Stage 4 — a fallback-matched POI contributes less
    than an exact-match one).
    """
    proximity = max(0.0, 1 - distance_km / radius_km)
    return category_weight * proximity * quality_factor


def category_score(dsn: str, project_id: int, category: str, model_version: str = "location-v1") -> float:
    """Sum of every in-category POI's decayed contribution, capped at 100
    so one exceptional POI can't single-handedly max out a category
    (SCORE-01).
    """
    config = MODEL_CONFIGS[model_version]
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT pp.distance_km, p.confidence
                FROM project_pois pp
                JOIN pois p ON p.id = pp.poi_id
                WHERE pp.project_id = %s AND p.category = %s;
                """,
                (project_id, category),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    contributions = [
        poi_contribution(float(d), config["category_weight"], config["radius_km"], float(confidence))
        for d, confidence in rows
    ]
    return min(100.0, round(sum(contributions), 2))


def compute_category_scores(dsn: str, project_id: int, model_version: str = "location-v1") -> dict[str, float]:
    """category_score() for every taxonomy category, in one dict."""
    return {c: category_score(dsn, project_id, c, model_version) for c in TAXONOMY_CATEGORIES}


def overall_score(category_scores: dict[str, float], model_version: str = "location-v1") -> float:
    """Design doc §5.5: average category score, boosted by a diversity
    bonus that rewards a project covered across many categories over one
    concentrated in a single category (SCORE-02) — this is the whole
    point of the diversity bonus, not raw POI count.
    """
    config = MODEL_CONFIGS[model_version]
    diversity_weight = config["diversity_weight"]

    avg = mean(category_scores.values()) if category_scores else 0.0
    covered = sum(1 for s in category_scores.values() if s > 0)
    total = len(category_scores) if category_scores else 1
    diversity_bonus = (covered / total) * diversity_weight

    return min(100.0, round(avg * (1 - diversity_weight) + diversity_bonus * 100, 2))
