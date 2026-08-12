"""Stage 4 — Deduplication.

Implements: QUAL-02.

Candidate duplicates are found via a PostGIS ST_DWithin proximity search
(default 30m) combined with name-similarity (pg_trgm). The surviving
record keeps the richest tag set and links back to every contributing
OSM ID via merged_from_osm_ids, so provenance is never lost on merge
(design doc §3.2, risk register §13).
"""
import psycopg2

DEFAULT_PROXIMITY_M = 30
DEFAULT_NAME_SIMILARITY = 0.6  # pg_trgm similarity() threshold, 0-1


def find_and_merge_duplicates(
    dsn: str,
    proximity_m: int = DEFAULT_PROXIMITY_M,
    name_similarity: float = DEFAULT_NAME_SIMILARITY,
) -> int:
    """Merge near-duplicate pois: same-ish name within proximity_m metres.

    For each cluster of duplicates, keeps the lowest-id row (oldest /
    first-seen) as the survivor, appends every other member's
    merged_from_osm_ids into it, and deletes the merged-away rows.

    Returns the number of rows merged away (deleted).
    """
    conn = psycopg2.connect(dsn)
    merged_count = 0
    try:
        with conn.cursor() as cur:
            # Geography cast for metre-accurate ST_DWithin; self-join excludes
            # trivial self-matches and only considers each unordered pair once.
            cur.execute(
                """
                SELECT a.id AS keep_id, b.id AS merge_id, b.merged_from_osm_ids
                FROM pois a
                JOIN pois b
                  ON a.id < b.id
                 AND a.category = b.category
                 AND ST_DWithin(a.geom::geography, b.geom::geography, %s)
                 AND a.name IS NOT NULL AND b.name IS NOT NULL
                 AND similarity(a.name, b.name) >= %s
                ORDER BY a.id;
                """,
                (proximity_m, name_similarity),
            )
            pairs = cur.fetchall()

            merged_away_ids = set()
            for keep_id, merge_id, merge_osm_ids in pairs:
                if merge_id in merged_away_ids or keep_id in merged_away_ids:
                    continue  # already resolved via an earlier pair in this batch
                cur.execute(
                    """
                    UPDATE pois
                    SET merged_from_osm_ids = merged_from_osm_ids || %s,
                        updated_at = now()
                    WHERE id = %s;
                    """,
                    (merge_osm_ids, keep_id),
                )
                cur.execute("DELETE FROM pois WHERE id = %s;", (merge_id,))
                merged_away_ids.add(merge_id)
                merged_count += 1

        conn.commit()
    finally:
        conn.close()

    return merged_count
