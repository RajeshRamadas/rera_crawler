"""STAGE 6 CHECK — Scoring & USP Engine.

Implements: SCORE-01-SCORE-04.
"""
import os

import psycopg2
import pytest

from intelligence.location_score import compute_scores, get_location_score
from intelligence.scoring import overall_score, compute_category_scores
from intelligence.usp_engine import select_usps, persist_usps

DSN = os.environ.get(
    "HOMEXO_TEST_DSN",
    "host=localhost dbname=homexo_dev user=homexo password=homexo",
)


def _make_project(cur, rera_id, lon=77.60, lat=12.97):
    cur.execute(
        "INSERT INTO rera_projects (rera_id, name, geom, status) "
        "VALUES (%s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326), 'active') RETURNING id;",
        (rera_id, rera_id, lon, lat),
    )
    return cur.fetchone()[0]


def _make_poi_and_relationship(cur, project_id, category, distance_km, name="Test POI"):
    cur.execute(
        "INSERT INTO pois (name, category, geom, confidence) "
        "VALUES (%s, %s, ST_SetSRID(ST_MakePoint(77.60, 12.97), 4326), 1.0) RETURNING id;",
        (name, category),
    )
    poi_id = cur.fetchone()[0]
    cur.execute(
        """
        INSERT INTO project_pois (project_id, poi_id, distance_km, bearing_deg, direction, band)
        VALUES (%s, %s, %s, 0, 'N', 'immediate');
        """,
        (project_id, poi_id, distance_km),
    )
    return poi_id


def _cleanup(project_ids, poi_ids):
    """Delete both the test projects AND every POI they created — leftover
    test POIs otherwise pollute other stages' 20km-radius candidate
    searches on later runs (they're real geometry rows, not scoped to a
    single test's project like project_pois/rera_projects are).
    """
    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        if project_ids:
            cur.execute("DELETE FROM rera_projects WHERE id = ANY(%s);", (project_ids,))
        if poi_ids:
            cur.execute("DELETE FROM pois WHERE id = ANY(%s);", (poi_ids,))
    conn.commit()
    conn.close()


@pytest.fixture
def fixed_fixture():
    """One project, one POI per category at a fixed distance — deterministic scoring input."""
    conn = psycopg2.connect(DSN)
    poi_ids = []
    with conn.cursor() as cur:
        project_id = _make_project(cur, "STAGE6-FIXED-001")
        poi_ids.append(_make_poi_and_relationship(cur, project_id, "transport", 1.0, "Fixed Metro"))
        poi_ids.append(_make_poi_and_relationship(cur, project_id, "healthcare", 2.0, "Fixed Hospital"))
    conn.commit()
    conn.close()

    yield project_id

    _cleanup([project_id], poi_ids)


@pytest.fixture
def concentration_vs_diversity_fixture():
    conn = psycopg2.connect(DSN)
    poi_ids = []
    with conn.cursor() as cur:
        concentrated_id = _make_project(cur, "STAGE6-CONC-001")
        for i in range(10):
            poi_ids.append(_make_poi_and_relationship(cur, concentrated_id, "shopping", 1.0, f"Shop {i}"))

        diverse_id = _make_project(cur, "STAGE6-DIV-001")
        for category in ["transport", "healthcare", "education", "employment", "shopping"]:
            poi_ids.append(_make_poi_and_relationship(cur, diverse_id, category, 1.0, f"{category} POI"))
    conn.commit()
    conn.close()

    yield concentrated_id, diverse_id

    _cleanup([concentrated_id, diverse_id], poi_ids)


def test_scoring_is_deterministic(fixed_fixture):
    project_id = fixed_fixture
    s1 = compute_scores(DSN, project_id, model_version="location-v1")
    s2 = compute_scores(DSN, project_id, model_version="location-v1")
    assert s1 == s2  # same input, same model version, same output — always


def test_diversity_beats_concentration(concentration_vs_diversity_fixture):
    concentrated_id, diverse_id = concentration_vs_diversity_fixture
    score_a = overall_score(compute_category_scores(DSN, concentrated_id))
    score_b = overall_score(compute_category_scores(DSN, diverse_id))
    assert score_b > score_a


def test_model_version_is_stamped(fixed_fixture):
    project_id = fixed_fixture
    compute_scores(DSN, project_id, model_version="location-v1")
    row = get_location_score(DSN, project_id, category="transport", model_version="location-v1")
    assert row is not None
    assert row[1] == "location-v1"


def test_usp_count_is_4_to_6(concentration_vs_diversity_fixture):
    _, diverse_id = concentration_vs_diversity_fixture
    usps = select_usps(DSN, diverse_id)
    covered_categories = 5  # transport, healthcare, education, employment, shopping seeded
    assert 4 <= len(usps) <= 6 or len(usps) == covered_categories


def test_usps_persist_with_headline_text(fixed_fixture):
    project_id = fixed_fixture
    usps = select_usps(DSN, project_id)
    persist_usps(DSN, project_id, usps)

    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute("SELECT rank, headline_text FROM project_usps WHERE project_id = %s ORDER BY rank;", (project_id,))
        rows = cur.fetchall()
    conn.close()

    assert len(rows) == len(usps)
    assert rows[0][0] == 1  # ranks start at 1
    assert all(text for _, text in rows)  # every USP has real headline copy
