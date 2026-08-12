"""STAGE 8 CHECK — Location API (read-only).

Implements: API-01, UX-01-UX-03.
"""
import os
import time

import psycopg2
import pytest
from fastapi.testclient import TestClient

from api.location import app
from intelligence.location_score import compute_scores
from intelligence.usp_engine import select_usps, persist_usps

DSN = os.environ.get(
    "HOMEXO_TEST_DSN",
    "host=localhost dbname=homexo_dev user=homexo password=homexo",
)

client = TestClient(app)


@pytest.fixture
def fully_seeded_project():
    """A project with relationships, scores, and USPs already computed —
    exactly the state Stage 8 assumes exists before a request ever
    arrives (API-01's whole premise: the API never does this work itself).
    """
    conn = psycopg2.connect(DSN)
    poi_ids = []
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO rera_projects (rera_id, name, geom, status) "
            "VALUES ('STAGE8-TEST-001', 'Stage 8 Test Project', "
            "ST_SetSRID(ST_MakePoint(77.6446, 12.9121), 4326), 'active') RETURNING id;"
        )
        project_id = cur.fetchone()[0]

        for category, distance_km, name in [
            ("transport", 1.0, "Stage8 Metro"),
            ("healthcare", 2.0, "Stage8 Hospital"),
            ("education", 3.0, "Stage8 School"),
        ]:
            cur.execute(
                "INSERT INTO pois (name, category, geom, confidence) "
                "VALUES (%s, %s, ST_SetSRID(ST_MakePoint(77.65, 12.92), 4326), 1.0) RETURNING id;",
                (name, category),
            )
            poi_id = cur.fetchone()[0]
            poi_ids.append(poi_id)
            cur.execute(
                "INSERT INTO project_pois (project_id, poi_id, distance_km, bearing_deg, direction, band) "
                "VALUES (%s, %s, %s, 45, 'NE', 'immediate');",
                (project_id, poi_id, distance_km),
            )
    conn.commit()
    conn.close()

    compute_scores(DSN, project_id, model_version="location-v1")
    usps = select_usps(DSN, project_id)
    persist_usps(DSN, project_id, usps)

    yield project_id

    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM rera_projects WHERE id = %s;", (project_id,))
        cur.execute("DELETE FROM pois WHERE id = ANY(%s);", (poi_ids,))
    conn.commit()
    conn.close()


def test_location_endpoint_shape(fully_seeded_project):
    project_id = fully_seeded_project
    resp = client.get(f"/api/projects/{project_id}/location")
    assert resp.status_code == 200
    body = resp.json()
    assert {"project_id", "location_score", "category_scores", "top_usps", "pois", "boundary", "map"}.issubset(body)


def test_endpoint_does_no_live_computation(fully_seeded_project, monkeypatch):
    project_id = fully_seeded_project

    def _boom(*a, **k):
        raise AssertionError("scoring called on read path")

    monkeypatch.setattr("intelligence.scoring.category_score", _boom)
    resp = client.get(f"/api/projects/{project_id}/location")
    assert resp.status_code == 200  # must succeed WITHOUT calling scoring


def test_latency_budget(fully_seeded_project):
    project_id = fully_seeded_project
    client.get(f"/api/projects/{project_id}/location")  # warm any connection setup
    t0 = time.perf_counter()
    client.get(f"/api/projects/{project_id}/location")
    assert time.perf_counter() - t0 < 0.2  # 200ms budget, single project, warm cache


def test_pois_endpoint_filters_by_category(fully_seeded_project):
    project_id = fully_seeded_project
    resp = client.get(f"/api/projects/{project_id}/location/pois", params={"category": "healthcare"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] >= 1
    assert all(p["category"] == "healthcare" for p in body["pois"])


def test_geojson_endpoint_returns_feature_collection(fully_seeded_project):
    project_id = fully_seeded_project
    resp = client.get(f"/api/projects/{project_id}/location/geojson")
    assert resp.status_code == 200
    body = resp.json()
    assert body["type"] == "FeatureCollection"
    assert len(body["features"]) > 0


def test_map_png_endpoint_returns_image(fully_seeded_project):
    project_id = fully_seeded_project
    resp = client.get(f"/api/projects/{project_id}/location/map.png")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert len(resp.content) > 0


def test_unknown_project_returns_404():
    resp = client.get("/api/projects/999999999/location")
    assert resp.status_code == 404
