"""Stage 7 — Interactive map payload.

Implements: MAP-I-01, MAP-I-03.

HOMEXO's interactive map is Google-basemap-only, rendered entirely
client-side (design doc §3.5, ARCH-04). This module's ONLY job is to
assemble what the client needs to build that view — project metadata,
top USPs, and the GeoJSON feed from maps/geojson.py.

STRUCTURAL GUARANTEE, not just convention: this file contains no import
of a Google Maps SDK, no `requests`/`urllib` call to any Google mapping
API endpoint, and no Google API key handling of any kind. A grep-based
test enforces this directly against this file's source. If you're
tempted to add a server-side call to a Google mapping service here —
don't; that's exactly the COMP-06/07 risk the design doc calls out, and
it belongs in the client's JS, never here.
"""
import psycopg2
import psycopg2.extras

from maps.geojson import build_geojson


def build_interactive_map_payload(dsn: str, project_id: int) -> dict:
    """Assemble everything the client needs to render its own Google Maps
    JS Data Layer view — this function does not call any map provider.
    """
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                "SELECT id, name, status, city, ST_X(geom) AS lon, ST_Y(geom) AS lat "
                "FROM rera_projects WHERE id = %s;",
                (project_id,),
            )
            project = cur.fetchone()
            if project is None:
                raise ValueError(f"No rera_projects row with id={project_id}")

            cur.execute(
                """
                SELECT pu.rank, pu.headline_text, p.name, p.category, pp.distance_km
                FROM project_usps pu
                JOIN pois p ON p.id = pu.poi_id
                JOIN project_pois pp ON pp.project_id = pu.project_id AND pp.poi_id = pu.poi_id
                WHERE pu.project_id = %s
                ORDER BY pu.rank ASC;
                """,
                (project_id,),
            )
            top_usps = [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()

    return {
        "project": dict(project),
        "top_usps": top_usps,
        "geojson": build_geojson(dsn, project_id),
        "render_note": "Client renders this against the Google Maps JS Data Layer. No Google API is ever called server-side.",
    }
