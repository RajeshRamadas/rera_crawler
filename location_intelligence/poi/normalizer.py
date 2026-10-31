"""Stage 4 — Normalization.

Implements: QUAL-01, QUAL-03, QUAL-04, POI-01, POI-02.

poi_staging rows (raw OSM tags) become clean `pois` rows (category/
subcategory only). Raw tags never cross that boundary (QUAL-01) — a
`pois` row has no raw_tags column at all, so this isn't just a
convention, it's structurally enforced by the Stage 2 schema.
"""
from dataclasses import dataclass
from typing import Optional

import psycopg2
import psycopg2.extras

FALLBACK_CONFIDENCE = 0.5  # heuristic/key-only match, vs. 1.0 for an exact tag=value match (QUAL-03)


@dataclass(frozen=True)
class Classification:
    category: str
    subcategory: Optional[str]
    confidence: float


def _load_category_rules(cur) -> list[tuple[str, str, Optional[str]]]:
    cur.execute("SELECT osm_tag, category, subcategory FROM category_rules;")
    return cur.fetchall()


def classify_tags(raw_tags: dict, category_rules: list[tuple[str, str, Optional[str]]]) -> Optional[Classification]:
    """Map raw OSM tags to (category, subcategory, confidence).

    Exact 'key=value' match against category_rules -> confidence 1.0.
    Fallback: same tag *key* present in category_rules (different value)
    -> confidence 0.5, using the first matching rule's category.
    No match at all -> None (caller quarantines to poi_rejects with
    reason 'unclassified').
    """
    tag_pairs = {f"{k}={v}" for k, v in raw_tags.items()}
    rule_by_pair = {r[0]: r for r in category_rules}

    exact_matches = tag_pairs & rule_by_pair.keys()
    if exact_matches:
        osm_tag, category, subcategory = rule_by_pair[sorted(exact_matches)[0]]
        return Classification(category, subcategory, confidence=1.0)

    known_keys = {r[0].split("=", 1)[0]: r for r in category_rules}
    for k in raw_tags:
        if k in known_keys:
            osm_tag, category, subcategory = known_keys[k]
            return Classification(category, subcategory, confidence=FALLBACK_CONFIDENCE)

    return None


def validate_coordinates(lon: float, lat: float) -> Optional[str]:
    """Return None if valid, else a reason_code string for poi_rejects (QUAL-04).

    Catches out-of-range values and the common lat/lon-swapped mistake
    (a value >90 can only be a swapped longitude, since lat must be
    within [-90, 90]).
    """
    if lat is None or lon is None:
        return "missing_coordinates"
    if abs(lat) > 90 or abs(lon) > 180:
        return "coordinates_out_of_range"
    return None


def run_normalization(dsn: str, extract_date=None) -> dict:
    """Process poi_staging rows into pois (classified + valid) or
    poi_rejects (invalid coords or unclassified — never silently dropped).

    Returns a summary dict of counts, matching the pipeline_runs
    observability signal (design doc §10).
    """
    conn = psycopg2.connect(dsn)
    counts = {"classified": 0, "rejected_coords": 0, "rejected_unclassified": 0}
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            rules = _load_category_rules(cur)

            if extract_date:
                cur.execute(
                    "SELECT osm_type, osm_id, raw_tags, ST_X(geom) AS lon, ST_Y(geom) AS lat "
                    "FROM poi_staging WHERE extract_date = %s;",
                    (extract_date,),
                )
            else:
                cur.execute(
                    "SELECT osm_type, osm_id, raw_tags, ST_X(geom) AS lon, ST_Y(geom) AS lat FROM poi_staging;"
                )
            staging_rows = cur.fetchall()

            for row in staging_rows:
                reason = validate_coordinates(row["lon"], row["lat"])
                if reason:
                    cur.execute(
                        """
                        INSERT INTO poi_rejects (osm_type, osm_id, reason_code, raw_tags)
                        VALUES (%s, %s, %s, %s);
                        """,
                        (row["osm_type"], row["osm_id"], reason, psycopg2.extras.Json(row["raw_tags"])),
                    )
                    counts["rejected_coords"] += 1
                    continue

                classification = classify_tags(row["raw_tags"], rules)
                if classification is None:
                    cur.execute(
                        """
                        INSERT INTO poi_rejects (osm_type, osm_id, reason_code, raw_tags)
                        VALUES (%s, %s, %s, %s);
                        """,
                        (row["osm_type"], row["osm_id"], "unclassified", psycopg2.extras.Json(row["raw_tags"])),
                    )
                    counts["rejected_unclassified"] += 1
                    continue

                name = row["raw_tags"].get("name")
                is_future = "construction" in (row["raw_tags"].get("railway", "") + row["raw_tags"].get("highway", ""))
                cur.execute(
                    """
                    INSERT INTO pois (name, category, subcategory, geom, confidence,
                                       merged_from_osm_ids, is_future)
                    VALUES (%s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326), %s, %s, %s)
                    RETURNING id;
                    """,
                    (
                        name, classification.category, classification.subcategory,
                        row["lon"], row["lat"], classification.confidence,
                        [row["osm_id"]], is_future,
                    ),
                )
                counts["classified"] += 1

        conn.commit()
    finally:
        conn.close()

    return counts


# ---------------------------------------------------------------------------
# Override resolution (QUAL-05) — applied after normalization, since an
# override always targets an existing pois.id
# ---------------------------------------------------------------------------

def apply_overrides(dsn: str) -> int:
    """poi_overrides wins over ingested data when present. Returns rows updated."""
    conn = psycopg2.connect(dsn)
    updated = 0
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT poi_id, field, override_value FROM poi_overrides;")
            for poi_id, field, value in cur.fetchall():
                if field not in {"name", "category", "subcategory"}:
                    continue  # only allow overriding known safe columns
                cur.execute(f"UPDATE pois SET {field} = %s, updated_at = now() WHERE id = %s;", (value, poi_id))
                updated += cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return updated
