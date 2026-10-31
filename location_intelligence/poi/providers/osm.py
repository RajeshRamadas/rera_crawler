"""Stage 3 — OSM Ingestion.

Implements: ING-01, ING-02, ING-03.

Design doc §3.1: fetch a regional .pbf extract, filter to taxonomy-relevant
tags before load (keeps staging volume manageable), bulk-insert into
poi_staging with raw tags as JSONB, extract_date recorded. Re-running
ingestion for the same extract_date is an overwrite, never a duplicate
append (idempotency, ING-03).

This module only touches the network in fetch_extract() — filtering and
loading work entirely on a local .osm.pbf file already on disk, per the
plan's "no live external API on a hot path" rule (only this stage's batch
job touches the network, ING-01).

Performance note: a regional extract (e.g. 100km around a city) can contain
tens of millions of nodes. A pure-Python pass over every node is too slow
at that scale. `run_ingestion()` therefore pre-filters with the `osmium`
CLI (a compiled tool, seconds not minutes) before pyosmium ever touches the
file — mirrors what a human operator would otherwise have to run by hand.
Falls back to the pure-Python filter automatically if the `osmium` binary
isn't on PATH (e.g. this build environment, or the small test fixture),
so behavior is identical either way — just slower without the binary.
"""
import hashlib
import json
import shutil
import subprocess
import tempfile
from datetime import date
from pathlib import Path
from typing import Optional

import osmium
import psycopg2
import psycopg2.extras


# ---------------------------------------------------------------------------
# Fetch (network — batch job only, per plan's global convention)
# ---------------------------------------------------------------------------

def verify_checksum(file_path: str, expected_sha256: str) -> bool:
    """Verify a downloaded extract's SHA-256 checksum before it's trusted."""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest() == expected_sha256


def fetch_extract(url: str, dest_path: str, expected_sha256: Optional[str] = None) -> str:
    """Download a regional .osm.pbf extract. Verifies checksum if provided.

    In production this hits Geofabrik (or the provider chosen per ING-02's
    evaluation). Kept separate from filter/load so the network call can be
    mocked in tests without touching the parsing logic.
    """
    import urllib.request

    urllib.request.urlretrieve(url, dest_path)
    if expected_sha256 and not verify_checksum(dest_path, expected_sha256):
        raise ValueError(f"Checksum mismatch for {dest_path} — download may be corrupt or tampered")
    return dest_path


# ---------------------------------------------------------------------------
# Fast native pre-filter (osmium CLI) — keeps large regional extracts fast
# ---------------------------------------------------------------------------

def _osmium_binary_available() -> bool:
    return shutil.which("osmium") is not None


def _prefilter_with_osmium_cli(pbf_path: str, relevant_tag_pairs: set[str], dest_path: str) -> str:
    """Shell out to `osmium tags-filter` to shrink a large extract before
    Python parses it. Node-only (n/key=value expressions), matching the
    node-based taxonomy this stage handles.
    """
    expressions = [f"n/{pair}" for pair in sorted(relevant_tag_pairs)]
    cmd = ["osmium", "tags-filter", pbf_path, *expressions, "-o", dest_path, "--overwrite"]
    subprocess.run(cmd, check=True, capture_output=True)
    return dest_path


# ---------------------------------------------------------------------------
# Filter — reduce to taxonomy-relevant tags before load (design doc §3.1)
# ---------------------------------------------------------------------------

class _TaxonomyFilterHandler(osmium.SimpleHandler):
    """Collects nodes whose tags match at least one known taxonomy key.

    A node is kept if ANY of its tags matches a 'key=value' pair present in
    category_rules — this mirrors `osmium tags-filter` behaviour but runs
    in-process so Stage 3 has no external-binary dependency beyond pyosmium.
    Used directly on small files, and as the final tag-extraction pass on
    files already shrunk by the native pre-filter above.
    """

    def __init__(self, relevant_tag_pairs: set[str]):
        super().__init__()
        self.relevant_tag_pairs = relevant_tag_pairs
        self.matched: list[dict] = []

    def node(self, n):
        if not n.location.valid():
            return
        tags = {t.k: t.v for t in n.tags}
        tag_pairs = {f"{k}={v}" for k, v in tags.items()}
        if tag_pairs & self.relevant_tag_pairs:
            self.matched.append({
                "osm_type": "node",
                "osm_id": n.id,
                "raw_tags": tags,
                "lon": n.location.lon,
                "lat": n.location.lat,
            })


def load_relevant_tag_pairs(dsn: str) -> set[str]:
    """Pull the set of 'key=value' pairs from category_rules — the filter's allowlist."""
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT osm_tag FROM category_rules;")
            return {row[0] for row in cur.fetchall()}
    finally:
        conn.close()


def filter_extract(pbf_path: str, relevant_tag_pairs: set[str], use_native_prefilter: bool = True) -> list[dict]:
    """Parse a .osm.pbf and return only nodes matching a taxonomy-relevant tag.

    Keeps staging volume manageable — junk/irrelevant tags never reach
    poi_staging (design doc §3.1, "Filter" step).

    For large regional extracts, shells out to the `osmium` CLI first
    (fast, compiled) to shrink the file before the pure-Python pass —
    the difference between minutes and seconds on a 100km-radius extract.
    Set use_native_prefilter=False to force the pure-Python path (used by
    Stage 3's tests against the small fixture, where the binary may not
    be installed).
    """
    working_path = pbf_path
    tmp_dir = None

    if use_native_prefilter and _osmium_binary_available():
        tmp_dir = tempfile.mkdtemp(prefix="homexo_osm_")
        prefiltered_path = str(Path(tmp_dir) / "prefiltered.osm.pbf")
        working_path = _prefilter_with_osmium_cli(pbf_path, relevant_tag_pairs, prefiltered_path)

    try:
        handler = _TaxonomyFilterHandler(relevant_tag_pairs)
        handler.apply_file(working_path)
        return handler.matched
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Load — idempotent bulk insert into poi_staging (ING-03)
# ---------------------------------------------------------------------------

def run_ingestion(dsn: str, pbf_path: str, extract_date: date, use_native_prefilter: bool = True) -> int:
    """Filter pbf_path to taxonomy-relevant records and load into poi_staging.

    Idempotent: re-running with the same extract_date first deletes any
    existing rows for that date, so a re-run overwrites rather than
    duplicates (ING-03).

    On large regional extracts, uses the `osmium` CLI as a fast pre-pass
    when available (see filter_extract) — falls back to pure Python
    automatically if the binary isn't installed.

    Returns the number of rows loaded.
    """
    relevant_tags = load_relevant_tag_pairs(dsn)
    records = filter_extract(pbf_path, relevant_tags, use_native_prefilter=use_native_prefilter)

    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            # Idempotency: wipe this extract_date's rows before reloading (ING-03)
            cur.execute("DELETE FROM poi_staging WHERE extract_date = %s;", (extract_date,))

            if records:
                psycopg2.extras.execute_values(
                    cur,
                    """
                    INSERT INTO poi_staging (osm_type, osm_id, raw_tags, geom, extract_date)
                    VALUES %s
                    ON CONFLICT (osm_type, osm_id, extract_date) DO UPDATE
                        SET raw_tags = EXCLUDED.raw_tags, geom = EXCLUDED.geom;
                    """,
                    [
                        (
                            r["osm_type"],
                            r["osm_id"],
                            json.dumps(r["raw_tags"]),
                            f"SRID=4326;POINT({r['lon']} {r['lat']})",
                            extract_date,
                        )
                        for r in records
                    ],
                    template="(%s, %s, %s::jsonb, ST_GeomFromEWKT(%s), %s)",
                )
        conn.commit()
    finally:
        conn.close()

    return len(records)


def staging_row_count(dsn: str, extract_date: Optional[date] = None) -> int:
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            if extract_date:
                cur.execute("SELECT count(*) FROM poi_staging WHERE extract_date = %s;", (extract_date,))
            else:
                cur.execute("SELECT count(*) FROM poi_staging;")
            return cur.fetchone()[0]
    finally:
        conn.close()


def get_staging_sample(dsn: str, limit: int = 50) -> list[dict]:
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT osm_type, osm_id, raw_tags FROM poi_staging LIMIT %s;", (limit,))
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
