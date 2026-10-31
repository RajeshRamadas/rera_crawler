"""STAGE 9 CHECK — Compliance & Attribution Guardrails.

Implements: COMP-01-COMP-09, ARCH-05, ARCH-06.
"""
import os

import psycopg2
import pytest

from compliance.guardrails import (
    get_table_columns,
    delete_poi_source_metadata,
    get_interactive_map_config,
)
from compliance.attribution import render_property_page

DSN = os.environ.get(
    "HOMEXO_TEST_DSN",
    "host=localhost dbname=homexo_dev user=homexo password=homexo",
)


@pytest.fixture
def seeded_project_with_source_metadata():
    """A project + POI + relationship + score + USP + source_metadata row
    — enough real state for every Stage 9 check to run against actual
    data rather than an empty/mocked project.
    """
    from intelligence.location_score import compute_scores
    from intelligence.usp_engine import select_usps, persist_usps

    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO rera_projects (rera_id, name, geom, status) "
            "VALUES ('STAGE9-TEST-001', 'Stage 9 Test Project', "
            "ST_SetSRID(ST_MakePoint(77.6446, 12.9121), 4326), 'active') RETURNING id;"
        )
        project_id = cur.fetchone()[0]

        cur.execute(
            "INSERT INTO pois (name, category, geom, confidence) "
            "VALUES ('Stage9 Hospital', 'healthcare', ST_SetSRID(ST_MakePoint(77.65, 12.92), 4326), 1.0) "
            "RETURNING id;"
        )
        poi_id = cur.fetchone()[0]

        cur.execute(
            "INSERT INTO project_pois (project_id, poi_id, distance_km, bearing_deg, direction, band) "
            "VALUES (%s, %s, 2.0, 45, 'NE', 'immediate');",
            (project_id, poi_id),
        )

        cur.execute(
            "INSERT INTO poi_source_metadata (poi_id, provider, licence) VALUES (%s, 'osm', 'ODbL');",
            (poi_id,),
        )
    conn.commit()
    conn.close()

    compute_scores(DSN, project_id, model_version="location-v1")
    usps = select_usps(DSN, project_id)
    persist_usps(DSN, project_id, usps)

    yield project_id, poi_id

    # Test-only cleanup escape hatch: poi_source_metadata is genuinely
    # append-only (that's what this whole stage verifies), so a normal
    # DELETE on its parent poi correctly gets blocked by the cascade
    # hitting the same trigger. Only a table owner/superuser can disable
    # a trigger — the application's runtime role should never have that
    # privilege in production, so this pattern is safe specifically
    # because it's unreachable from anywhere except test teardown code
    # run with elevated local-dev credentials.
    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE poi_source_metadata DISABLE TRIGGER trg_poi_source_metadata_no_delete;")
        cur.execute("DELETE FROM poi_source_metadata WHERE poi_id = %s;", (poi_id,))
        cur.execute("ALTER TABLE poi_source_metadata ENABLE TRIGGER trg_poi_source_metadata_no_delete;")
        cur.execute("DELETE FROM rera_projects WHERE id = %s;", (project_id,))
        cur.execute("DELETE FROM pois WHERE id = %s;", (poi_id,))
    conn.commit()
    conn.close()


def test_no_google_places_fields_in_pois_table():
    schema = get_table_columns(DSN, "pois")
    assert not any("google" in c.lower() for c in schema)


def test_osm_attribution_always_rendered(seeded_project_with_source_metadata):
    project_id, _ = seeded_project_with_source_metadata
    page = render_property_page(DSN, project_id)
    assert "OpenStreetMap" in page  # not conditional on any flag or parameter


def test_source_metadata_is_append_only(seeded_project_with_source_metadata):
    _, poi_id = seeded_project_with_source_metadata
    with pytest.raises(PermissionError):
        delete_poi_source_metadata(DSN, poi_id)  # hard delete must be blocked


def test_source_metadata_truncate_is_also_blocked(seeded_project_with_source_metadata):
    # Real gap found while building this stage: PostgreSQL's TRUNCATE does
    # NOT fire row-level DELETE triggers, so the original Stage 2 trigger
    # alone did not stop a TRUNCATE from silently wiping the audit trail.
    # Migration 008 adds a statement-level trigger closing that hole —
    # this test proves it actually blocks TRUNCATE, not just DELETE.
    conn = psycopg2.connect(DSN)
    with pytest.raises(psycopg2.errors.RaiseException):
        with conn.cursor() as cur:
            cur.execute("TRUNCATE poi_source_metadata;")
    conn.rollback()
    conn.close()


def test_single_basemap_per_map_instance(seeded_project_with_source_metadata):
    project_id, _ = seeded_project_with_source_metadata
    map_config = get_interactive_map_config(DSN, project_id)
    assert map_config.basemap_provider == "google"
    assert map_config.overlay_layers_are_data_only is True  # no competing tile layer
