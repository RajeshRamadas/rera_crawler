"""Stage 6 — USP Engine.

Implements: SCORE-04.

Picks the single best POI per category (by contribution score), ranks
those across categories, and selects the top 4-6 as marketing-ready USPs
(design doc §5.6). Also generates the headline_text stored alongside
each USP — a minimal version of the "Marketing Output" copy generation,
enough to prove the pipeline end-to-end; richer templates/tone can be
layered on without changing the selection logic below.
"""
import psycopg2

from intelligence.scoring import MODEL_CONFIGS, TAXONOMY_CATEGORIES, poi_contribution

HEADLINE_TEMPLATES = {
    "transport": "Excellent connectivity — {name} just {distance}km away",
    "healthcare": "Peace of mind nearby — {name} within {distance}km",
    "education": "Great schooling options — {name} just {distance}km away",
    "employment": "Close to work hubs — {name} within {distance}km",
    "shopping": "Retail therapy nearby — {name} just {distance}km away",
    "lifestyle": "Unwind close to home — {name} within {distance}km",
    "civic": "Essential services nearby — {name} just {distance}km away",
    "hospitality": "Premium hospitality nearby — {name} within {distance}km",
    "future": "Future-ready infrastructure — {name} coming {distance}km away",
}


def _headline_for(category: str, name: str, distance_km: float) -> str:
    template = HEADLINE_TEMPLATES.get(category, "{name} nearby ({distance}km)")
    return template.format(name=name or category.title(), distance=round(distance_km, 1))


def select_usps(dsn: str, project_id: int, model_version: str = "location-v1", min_n: int = 4, max_n: int = 6) -> list[dict]:
    """Best POI per category, ranked by contribution, top 4-6 overall
    (design doc §5.6). If fewer than min_n categories are covered at all,
    returns everything available rather than padding — matches the
    pseudocode's `ranked[:max_n] if len(ranked) >= min_n else ranked`.
    """
    config = MODEL_CONFIGS[model_version]
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            best_per_category = []
            for category in TAXONOMY_CATEGORIES:
                cur.execute(
                    """
                    SELECT p.id, p.name, p.confidence, pp.distance_km
                    FROM project_pois pp
                    JOIN pois p ON p.id = pp.poi_id
                    WHERE pp.project_id = %s AND p.category = %s
                    ORDER BY pp.distance_km ASC;
                    """,
                    (project_id, category),
                )
                candidates = cur.fetchall()
                if not candidates:
                    continue

                best = max(
                    candidates,
                    key=lambda row: poi_contribution(
                        float(row[3]), config["category_weight"], config["radius_km"], float(row[2])
                    ),
                )
                poi_id, name, confidence, distance_km = best
                contribution = poi_contribution(
                    float(distance_km), config["category_weight"], config["radius_km"], float(confidence)
                )
                best_per_category.append({
                    "category": category,
                    "poi_id": poi_id,
                    "name": name,
                    "distance_km": float(distance_km),
                    "contribution": contribution,
                })
    finally:
        conn.close()

    ranked = sorted(best_per_category, key=lambda u: u["contribution"], reverse=True)
    selected = ranked[:max_n] if len(ranked) >= min_n else ranked

    for usp in selected:
        usp["headline_text"] = _headline_for(usp["category"], usp["name"], usp["distance_km"])

    return selected


def persist_usps(dsn: str, project_id: int, usps: list[dict], model_version: str = "location-v1") -> int:
    """Write select_usps() output to project_usps, ranked 1..N (SCORE-04).

    Replaces any prior USP set for this project+model_version — a rescore
    should reflect the current best selection, not accumulate stale rows.
    """
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM project_usps WHERE project_id = %s AND model_version = %s;",
                (project_id, model_version),
            )
            for rank, usp in enumerate(usps, start=1):
                cur.execute(
                    """
                    INSERT INTO project_usps (project_id, poi_id, rank, headline_text, model_version)
                    VALUES (%s, %s, %s, %s, %s);
                    """,
                    (project_id, usp["poi_id"], rank, usp["headline_text"], model_version),
                )
        conn.commit()
    finally:
        conn.close()
    return len(usps)
