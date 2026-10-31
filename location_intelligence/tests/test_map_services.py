"""STAGE 7 CHECK — Map Services.

Implements: MAP-S-01, MAP-S-02, MAP-I-01-MAP-I-03.
"""
import json
import os
import shutil
from pathlib import Path

import psycopg2
import pytest

from maps.static_map import render_static_map, CACHE_DIR
from maps.geojson import build_geojson, validate_geojson_schema
from intelligence.location_score import compute_scores

DSN = os.environ.get(
    "HOMEXO_TEST_DSN",
    "host=localhost dbname=homexo_dev user=homexo password=homexo",
)


def _make_project_with_poi(cur, rera_id):
    cur.execute(
        "INSERT INTO rera_projects (rera_id, name, geom, status) "
        "VALUES (%s, %s, ST_SetSRID(ST_MakePoint(77.6446, 12.9121), 4326), 'active') RETURNING id;",
        (rera_id, rera_id),
    )
    project_id = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO pois (name, category, geom, confidence) "
        "VALUES ('Stage7 Test Hospital', 'healthcare', ST_SetSRID(ST_MakePoint(77.65, 12.915), 4326), 1.0) RETURNING id;"
    )
    poi_id = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO project_pois (project_id, poi_id, distance_km, bearing_deg, direction, band) "
        "VALUES (%s, %s, 1.2, 45, 'NE', 'immediate');",
        (project_id, poi_id),
    )
    return project_id, poi_id


@pytest.fixture
def seeded_project():
    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        project_id, poi_id = _make_project_with_poi(cur, "STAGE7-TEST-001")
    conn.commit()
    conn.close()

    yield project_id

    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM rera_projects WHERE id = %s;", (project_id,))
        cur.execute("DELETE FROM pois WHERE id = %s;", (poi_id,))
    conn.commit()
    conn.close()
    shutil.rmtree(CACHE_DIR, ignore_errors=True)


def test_static_map_contains_required_elements(seeded_project):
    project_id = seeded_project
    meta = render_static_map(DSN, project_id, debug_layers=True)
    assert meta.has_rings == [2, 5, 10, 20]
    assert meta.has_osm_attribution is True
    assert Path(meta.image_path).exists()


def test_geojson_feed_is_valid_and_google_free(seeded_project):
    project_id = seeded_project
    fc = build_geojson(DSN, project_id)
    assert validate_geojson_schema(fc)
    assert "google" not in json.dumps(fc).lower()


def test_no_google_import_in_maps_module():
    src = Path("maps/interactive_map.py").read_text()
    assert "googlemaps" not in src
    assert "requests.get('https://maps.googleapis" not in src
    assert "maps.googleapis.com" not in src


def test_static_map_cache_invalidates_on_rescore(seeded_project):
    project_id = seeded_project
    v1 = render_static_map(DSN, project_id)
    compute_scores(DSN, project_id, model_version="location-v2")
    v2 = render_static_map(DSN, project_id)
    assert v1.cache_key != v2.cache_key


def test_static_map_is_cached_when_nothing_changed(seeded_project):
    project_id = seeded_project
    v1 = render_static_map(DSN, project_id)
    v2 = render_static_map(DSN, project_id)  # no relationship/score change in between
    assert v1.cache_key == v2.cache_key
    assert v2.from_cache is True
