"""Stage 6 — Location Score persistence.

Implements: SCORE-01-SCORE-03.

Orchestrates intelligence/scoring.py's pure functions and writes the
result to location_scores, every row stamped with model_version so a
historical score can be audited against the model version that produced
it (SCORE-03).
"""
import psycopg2

from intelligence.scoring import (
    TAXONOMY_CATEGORIES,
    compute_category_scores,
    overall_score,
)


def compute_scores(dsn: str, project_id: int, model_version: str = "location-v1") -> dict[str, float]:
    """Compute category scores + overall score for one project and persist
    them to location_scores (upsert on project_id+category+model_version).

    Deterministic: identical project_pois + model_version always produce
    identical output — pure function of stored data, no external calls,
    so a re-score after a taxonomy/weight change is a reproducible replay.

    Returns {category: score, ..., "overall": score}.
    """
    category_scores = compute_category_scores(dsn, project_id, model_version)
    overall = overall_score(category_scores, model_version)

    all_scores = {**category_scores, "overall": overall}

    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            for category, score in all_scores.items():
                cur.execute(
                    """
                    INSERT INTO location_scores (project_id, category, score, model_version)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (project_id, category, model_version) DO UPDATE
                        SET score = EXCLUDED.score, computed_at = now();
                    """,
                    (project_id, category, score, model_version),
                )
        conn.commit()
    finally:
        conn.close()

    return all_scores


def get_location_score(dsn: str, project_id: int, category: str, model_version: str = "location-v1"):
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT score, model_version FROM location_scores "
                "WHERE project_id = %s AND category = %s AND model_version = %s;",
                (project_id, category, model_version),
            )
            return cur.fetchone()
    finally:
        conn.close()
