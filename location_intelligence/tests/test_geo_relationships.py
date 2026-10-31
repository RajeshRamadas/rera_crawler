"""STAGE 5 CHECK — Geo Relationship Engine.

Implements: GEO-01-GEO-04.

Requires Stage 1 (geo primitives) and Stage 4 (normalized pois). Seeds
its own synthetic project + POIs so this stage's tests are self-contained
and don't depend on real RERA/OSM data being loaded.
"""
import os

import psycopg2
import psycopg2.extras
import pytest

from intelligence.relationships import build_relationships, DEFAULT_RADIUS_KM
from geo.distance import haversine_km

DSN = os.environ.get(
    "HOMEXO_TEST_DSN",
    "host=localhost dbname=homexo_dev user=homexo password=homexo",
)

# A fixed project location and a handful of POIs at known, hand-computed
# distances/bearings — lets the test assert exact expected bands/directions,
# not just "some rows got written".
PROJECT_LAT, PROJECT_LON = 12.9121, 77.6446  # HSR Layout, Bengaluru


@pytest.fixture
def seeded_project_and_pois():
    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO rera_projects (rera_id, name, geom, status) "
            "VALUES ('STAGE5-TEST-001', 'Stage 5 Test Project', "
            "ST_SetSRID(ST_MakePoint(%s, %s), 4326), 'active') RETURNING id;",
            (PROJECT_LON, PROJECT_LAT),
        )
        project_id = cur.fetchone()[0]

        # POI A: ~1km north  -> should land in 'immediate' band, direction ~N
        cur.execute(
            "INSERT INTO pois (name, category, geom, confidence) "
            "VALUES ('Near Hospital', 'healthcare', ST_SetSRID(ST_MakePoint(%s, %s), 4326), 1.0) RETURNING id;",
            (PROJECT_LON, PROJECT_LAT + 0.009),  # ~1km north
        )
        poi_near_id = cur.fetchone()[0]

        # POI B: ~25km away -> outside the default 20km radius, must be excluded
        cur.execute(
            "INSERT INTO pois (name, category, geom, confidence) "
            "VALUES ('Far Mall', 'shopping', ST_SetSRID(ST_MakePoint(%s, %s), 4326), 1.0) RETURNING id;",
            (PROJECT_LON, PROJECT_LAT + 0.225),  # ~25km north
        )
        poi_far_id = cur.fetchone()[0]

    conn.commit()
    conn.close()

    yield project_id, poi_near_id, poi_far_id

    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM rera_projects WHERE id = %s;", (project_id,))
        cur.execute("DELETE FROM pois WHERE id IN (%s, %s);", (poi_near_id, poi_far_id))
    conn.commit()
    conn.close()


def _fetch_project_poi(project_id, poi_id):
    conn = psycopg2.connect(DSN)
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute(
            "SELECT * FROM project_pois WHERE project_id = %s AND poi_id = %s;",
            (project_id, poi_id),
        )
        row = cur.fetchone()
    conn.close()
    return row


def test_relationships_computed_for_pois_within_radius(seeded_project_and_pois):
    project_id, poi_near_id, poi_far_id = seeded_project_and_pois
    written = build_relationships(DSN, project_id)
    assert written >= 1

    near = _fetch_project_poi(project_id, poi_near_id)
    assert near is not None
    assert near["band"] == "immediate"
    assert near["direction"] == "N"
    assert 0.9 < float(near["distance_km"]) < 1.1


def test_poi_outside_default_radius_is_excluded(seeded_project_and_pois):
    project_id, poi_near_id, poi_far_id = seeded_project_and_pois
    build_relationships(DSN, project_id)

    far = _fetch_project_poi(project_id, poi_far_id)
    assert far is None  # ~25km > 20km default radius (GEO-01) — must not appear


def test_distance_matches_haversine_independently(seeded_project_and_pois):
    project_id, poi_near_id, poi_far_id = seeded_project_and_pois
    build_relationships(DSN, project_id)

    near = _fetch_project_poi(project_id, poi_near_id)
    expected = haversine_km(PROJECT_LAT, PROJECT_LON, PROJECT_LAT + 0.009, PROJECT_LON)
    assert float(near["distance_km"]) == pytest.approx(expected, abs=0.01)


def test_rerunning_is_idempotent_not_duplicated(seeded_project_and_pois):
    project_id, poi_near_id, poi_far_id = seeded_project_and_pois
    build_relationships(DSN, project_id)
    build_relationships(DSN, project_id)  # run again

    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM project_pois WHERE project_id = %s;", (project_id,))
        count = cur.fetchone()[0]
    conn.close()
    assert count == 1  # ON CONFLICT upsert, not a second row
