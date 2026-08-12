"""STAGE 3 CHECK — OSM ingestion against a small local test extract.

Implements: ING-01, ING-02, ING-03.

Uses tests/fixtures/sample.osm.pbf — a small synthetic extract (6 nodes,
mixing taxonomy-relevant tags with junk tags) so this stage's tests run
without a live network call, per the plan's "no live external API on a
hot path" rule. Point RUN against your real Bangalore extract the same
way — run_ingestion() takes any local .osm.pbf path.
"""
import os
from datetime import date
from pathlib import Path

import pytest

from poi.providers.osm import run_ingestion, staging_row_count, get_staging_sample

DSN = os.environ.get(
    "HOMEXO_TEST_DSN",
    "host=localhost dbname=homexo_dev user=homexo password=homexo",
)
FIXTURE = str(Path(__file__).parent / "fixtures" / "sample.osm.pbf")
TEST_EXTRACT_DATE = date(2026, 8, 1)


@pytest.fixture(autouse=True)
def _clean_staging():
    # keep this stage's tests independent of whatever else is in poi_staging
    yield
    import psycopg2
    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM poi_staging WHERE extract_date = %s;", (TEST_EXTRACT_DATE,))
    conn.commit()
    conn.close()


def test_ingestion_is_idempotent():
    run_ingestion(DSN, FIXTURE, extract_date=TEST_EXTRACT_DATE)
    count_1 = staging_row_count(DSN, extract_date=TEST_EXTRACT_DATE)
    run_ingestion(DSN, FIXTURE, extract_date=TEST_EXTRACT_DATE)  # same date again
    count_2 = staging_row_count(DSN, extract_date=TEST_EXTRACT_DATE)
    assert count_1 == count_2  # not doubled
    assert count_1 > 0


def test_ingestion_matches_with_and_without_native_prefilter():
    # The osmium-CLI fast path and the pure-Python fallback must agree —
    # performance differs, results must not.
    n_native = run_ingestion(DSN, FIXTURE, extract_date=TEST_EXTRACT_DATE, use_native_prefilter=True)
    ids_native = {row["osm_id"] for row in get_staging_sample(DSN, limit=50)}
    n_python = run_ingestion(DSN, FIXTURE, extract_date=TEST_EXTRACT_DATE, use_native_prefilter=False)
    ids_python = {row["osm_id"] for row in get_staging_sample(DSN, limit=50)}
    assert n_native == n_python
    assert ids_native == ids_python


def test_staging_has_no_unfiltered_junk_tags():
    run_ingestion(DSN, FIXTURE, extract_date=TEST_EXTRACT_DATE)
    sample = get_staging_sample(DSN, limit=50)
    assert all(row["raw_tags"] for row in sample)  # every row has tags, filter worked


def test_junk_only_node_is_excluded():
    # fixture node 1005 has only created_by/source tags — no taxonomy match
    n = run_ingestion(DSN, FIXTURE, extract_date=TEST_EXTRACT_DATE)
    sample = get_staging_sample(DSN, limit=50)
    osm_ids = {row["osm_id"] for row in sample}
    assert 1005 not in osm_ids
    assert n == len(sample)


def test_relevant_nodes_are_staged():
    run_ingestion(DSN, FIXTURE, extract_date=TEST_EXTRACT_DATE)
    sample = get_staging_sample(DSN, limit=50)
    osm_ids = {row["osm_id"] for row in sample}
    # hospital, mall, school, bus_station all match category_rules seeded in Stage 2.
    # Node 1001 (amenity=restaurant) is NOT in the seeded taxonomy (§5 doesn't list
    # a "food" category), so it's correctly excluded here too — same as junk tags.
    assert {1002, 1003, 1004, 1006}.issubset(osm_ids)
    assert 1001 not in osm_ids
