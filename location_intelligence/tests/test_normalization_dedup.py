"""STAGE 4 CHECK — Normalization & Deduplication.

Implements: QUAL-01-QUAL-05.
"""
import os
from datetime import date

import psycopg2
import psycopg2.extras
import pytest

from poi.normalizer import run_normalization, apply_overrides
from poi.dedupe import find_and_merge_duplicates

DSN = os.environ.get(
    "HOMEXO_TEST_DSN",
    "host=localhost dbname=homexo_dev user=homexo password=homexo",
)
TEST_EXTRACT_DATE = date(2026, 8, 2)  # distinct from Stage 3's test date, keeps fixtures isolated


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def load_staging_row(lat, lon, tags, osm_id=None, extract_date=TEST_EXTRACT_DATE):
    """Insert a single row directly into poi_staging, bypassing ingestion —
    lets Stage 4's tests target specific edge cases without needing a real
    .osm.pbf fixture. Assumes valid coordinates; for invalid-coordinate
    testing see test_invalid_coordinates_are_quarantined_not_dropped, which
    inserts directly since poi_staging.geom is NOT NULL and can't hold an
    out-of-range point as a first-class geometry.
    """
    import random
    osm_id = osm_id or random.randint(10_000_000, 99_999_999)
    conn = psycopg2.connect(DSN)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO poi_staging (osm_type, osm_id, raw_tags, geom, extract_date)
                VALUES ('node', %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326), %s)
                ON CONFLICT (osm_type, osm_id, extract_date) DO NOTHING;
                """,
                (osm_id, psycopg2.extras.Json(tags), lon, lat, extract_date),
            )
        conn.commit()
    finally:
        conn.close()
    return osm_id


def get_any_poi():
    conn = psycopg2.connect(DSN)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT * FROM pois LIMIT 1;")
            return cur.fetchone()
    finally:
        conn.close()


def staging_row_in_rejects(osm_id) -> bool:
    conn = psycopg2.connect(DSN)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM poi_rejects WHERE osm_id = %s;", (osm_id,))
            return cur.fetchone() is not None
    finally:
        conn.close()


def staging_row_in_pois_by_osm_id(osm_id) -> bool:
    conn = psycopg2.connect(DSN)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pois WHERE %s = ANY(merged_from_osm_ids);", (osm_id,))
            return cur.fetchone() is not None
    finally:
        conn.close()


def seed_two_near_duplicate_metro_stations():
    """Two 'Majestic Metro Station' nodes ~15m apart, same name — must merge."""
    id1 = load_staging_row(12.9767, 77.5713, {"station": "subway", "name": "Majestic Metro Station"})
    id2 = load_staging_row(12.97682, 77.57142, {"station": "subway", "name": "Majestic Metro Station"})
    return id1, id2


@pytest.fixture(autouse=True)
def _clean_tables():
    yield
    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM poi_staging WHERE extract_date = %s;", (TEST_EXTRACT_DATE,))
        cur.execute("DELETE FROM poi_overrides;")
        cur.execute("DELETE FROM pois;")
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# STAGE 4 CHECK
# ---------------------------------------------------------------------------

def test_raw_tags_never_reach_pois_table():
    load_staging_row(12.97, 77.59, {"amenity": "hospital", "name": "Test Hospital"})
    run_normalization(DSN, extract_date=TEST_EXTRACT_DATE)
    poi = get_any_poi()
    assert poi is not None
    assert "raw_tags" not in poi.keys()  # only category/subcategory, no raw OSM tags


def test_invalid_coordinates_are_quarantined_not_dropped():
    # PostGIS doesn't enforce lat/lon domain on SRID 4326 by default, so this
    # inserts fine at the DB layer — validate_coordinates() is what catches it
    # during normalization, exactly like a real malformed OSM record would be caught.
    bad_id = load_staging_row(999, 999, {"amenity": "hospital", "name": "Bad Coords Hospital"})

    run_normalization(DSN, extract_date=TEST_EXTRACT_DATE)
    assert staging_row_in_rejects(bad_id)
    assert not staging_row_in_pois_by_osm_id(bad_id)


def test_unclassified_tag_is_quarantined_not_dropped():
    junk_id = load_staging_row(12.9, 77.5, {"created_by": "JOSM"})  # no taxonomy match
    run_normalization(DSN, extract_date=TEST_EXTRACT_DATE)
    assert staging_row_in_rejects(junk_id)
    assert not staging_row_in_pois_by_osm_id(junk_id)


def test_exact_match_gets_full_confidence():
    load_staging_row(12.9, 77.5, {"amenity": "hospital", "name": "Exact Match Hospital"})
    run_normalization(DSN, extract_date=TEST_EXTRACT_DATE)
    poi = get_any_poi()
    assert float(poi["confidence"]) == 1.0


def test_near_duplicate_pois_merge():
    id1, id2 = seed_two_near_duplicate_metro_stations()
    run_normalization(DSN, extract_date=TEST_EXTRACT_DATE)
    merged = find_and_merge_duplicates(DSN)
    assert merged == 1

    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM pois WHERE 'Majestic Metro Station' = name;")
        remaining = cur.fetchone()[0]
    conn.close()
    assert remaining == 1  # merged down to a single record

    survivor = get_any_poi()
    assert id1 in survivor["merged_from_osm_ids"]
    assert id2 in survivor["merged_from_osm_ids"]


def test_override_wins_over_ingested_data():
    load_staging_row(12.9, 77.5, {"amenity": "hospital", "name": "Original Name"})
    run_normalization(DSN, extract_date=TEST_EXTRACT_DATE)
    poi = get_any_poi()

    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO poi_overrides (poi_id, field, override_value, verified_by) VALUES (%s, %s, %s, %s);",
            (poi["id"], "name", "Verified Correct Name", "ops_team"),
        )
    conn.commit()
    conn.close()

    apply_overrides(DSN)
    updated = get_any_poi()
    assert updated["name"] == "Verified Correct Name"
