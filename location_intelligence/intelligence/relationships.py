"""Stage 5 — Geo Relationship Engine.

Implements: GEO-01-GEO-04.

For each active project, queries pois within the applicable radius using
a PostGIS spatial index, then computes distance, bearing, direction and
band for every candidate pair (design doc §3.3). Distance/bearing are
always computed in-house from Stage 1's primitives — never sourced from
a third party (GEO-04, ARCH-03).
"""
from typing import Optional

import psycopg2
import psycopg2.extras

from geo.distance import haversine_km
from geo.bearing import bearing_deg
from geo.direction import compass_16
from geo.distance_bands import distance_band

DEFAULT_RADIUS_KM = 20  # GEO-01

# Per-category radius overrides (GEO-01: "shall support category-specific
# radius overrides"). Empty by default — a category not listed here uses
# DEFAULT_RADIUS_KM. Populate as product tuning decisions are made.
CATEGORY_RADIUS_OVERRIDES_KM: dict[str, float] = {}


def _radius_for_category(category: str) -> float:
    return CATEGORY_RADIUS_OVERRIDES_KM.get(category, DEFAULT_RADIUS_KM)


def _pois_within(cur, project_lon: float, project_lat: float, radius_km: float) -> list[dict]:
    """Candidate POIs within radius_km of a project, via the GIST spatial index."""
    cur.execute(
        """
        SELECT id, category, ST_X(geom) AS lon, ST_Y(geom) AS lat
        FROM pois
        WHERE ST_DWithin(geom::geography, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s);
        """,
        (project_lon, project_lat, radius_km * 1000),  # metres
    )
    return cur.fetchall()


def build_relationships(dsn: str, project_id: int) -> int:
    """Compute and upsert project_pois rows for one project.

    Returns the number of relationships written. Skips any candidate
    whose actual haversine distance (computed here, independently of the
    PostGIS candidate search) falls outside that POI category's radius —
    the PostGIS query uses the largest applicable radius as a superset
    filter, so this is a legitimate second, precise pass, not redundant
    work.
    """
    conn = psycopg2.connect(dsn)
    written = 0
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT ST_X(geom) AS lon, ST_Y(geom) AS lat FROM rera_projects WHERE id = %s;", (project_id,))
            project = cur.fetchone()
            if project is None:
                raise ValueError(f"No rera_projects row with id={project_id}")

            # Use the widest configured radius as the PostGIS candidate filter,
            # then apply each POI's own category radius precisely below.
            widest_radius = max([DEFAULT_RADIUS_KM, *CATEGORY_RADIUS_OVERRIDES_KM.values()])
            candidates = _pois_within(cur, project["lon"], project["lat"], widest_radius)

            for poi in candidates:
                d = haversine_km(project["lat"], project["lon"], poi["lat"], poi["lon"])
                radius_km = _radius_for_category(poi["category"])
                if d >= radius_km:
                    continue  # outside this POI's applicable radius — GEO-01

                brg = bearing_deg(project["lat"], project["lon"], poi["lat"], poi["lon"])
                band = distance_band(d)  # GEO-02
                direction = compass_16(brg)  # GEO-03

                if band is None:
                    continue  # outside the 20km default banding range entirely

                cur.execute(
                    """
                    INSERT INTO project_pois (project_id, poi_id, distance_km, bearing_deg, direction, band, method)
                    VALUES (%s, %s, %s, %s, %s, %s, 'haversine')
                    ON CONFLICT (project_id, poi_id) DO UPDATE
                        SET distance_km = EXCLUDED.distance_km,
                            bearing_deg = EXCLUDED.bearing_deg,
                            direction = EXCLUDED.direction,
                            band = EXCLUDED.band,
                            computed_at = now();
                    """,
                    (project_id, poi["id"], round(d, 3), round(brg, 2), direction, band),
                )
                written += 1

        conn.commit()
    finally:
        conn.close()

    return written


def build_relationships_for_all_projects(dsn: str, project_ids: Optional[list[int]] = None) -> dict[int, int]:
    """Batch entry point — Stage 5's part of the design doc §6.1 pipeline flow.

    If project_ids is None, processes every active rera_projects row.
    Returns {project_id: relationships_written}.
    """
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            if project_ids is None:
                cur.execute("SELECT id FROM rera_projects WHERE status = 'active';")
                project_ids = [row[0] for row in cur.fetchall()]
    finally:
        conn.close()

    return {pid: build_relationships(dsn, pid) for pid in project_ids}
