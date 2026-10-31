"""Stage 7 — GeoJSON export.

Implements: MAP-I-02.

Standard GeoJSON FeatureCollection: project marker, boundary (if a
validated one exists), distance rings, and nearby POIs. This module
exists specifically so the client can feed it into a Google Maps JS
Data Layer — but the module itself never talks to any Google API or
imports any Google SDK (MAP-I-02's explicit requirement, enforced by
tests/test_map_services.py::test_geojson_feed_is_valid_and_google_free
and ::test_no_google_import_in_maps_module).

Ring geometry is generated via PostGIS ST_Buffer on a geography cast
(accurate metre buffering), at radii matching Stage 1's exact distance
bands (GEO-02) — 2/5/10/20km — so the rings drawn on a map always agree
with the band a POI was actually classified into.
"""
import json

import psycopg2
import psycopg2.extras

RING_RADII_KM = [2, 5, 10, 20]


def build_geojson(dsn: str, project_id: int) -> dict:
    """Return a standard GeoJSON FeatureCollection for one project."""
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                "SELECT id, name, ST_AsGeoJSON(geom) AS geojson FROM rera_projects WHERE id = %s;",
                (project_id,),
            )
            project = cur.fetchone()
            if project is None:
                raise ValueError(f"No rera_projects row with id={project_id}")

            features = [{
                "type": "Feature",
                "geometry": json.loads(project["geojson"]),
                "properties": {"feature_type": "project", "project_id": project["id"], "name": project["name"]},
            }]

            cur.execute(
                "SELECT ST_AsGeoJSON(geom) AS geojson, validated FROM project_boundaries WHERE project_id = %s;",
                (project_id,),
            )
            boundary = cur.fetchone()
            if boundary and boundary["geojson"]:
                features.append({
                    "type": "Feature",
                    "geometry": json.loads(boundary["geojson"]),
                    "properties": {"feature_type": "boundary", "validated": boundary["validated"]},
                })

            for radius_km in RING_RADII_KM:
                cur.execute(
                    """
                    SELECT ST_AsGeoJSON(
                        ST_Buffer(
                            (SELECT geom FROM rera_projects WHERE id = %s)::geography,
                            %s
                        )::geometry
                    ) AS geojson;
                    """,
                    (project_id, radius_km * 1000),
                )
                ring = cur.fetchone()
                features.append({
                    "type": "Feature",
                    "geometry": json.loads(ring["geojson"]),
                    "properties": {"feature_type": "ring", "radius_km": radius_km},
                })

            cur.execute(
                """
                SELECT p.id, p.name, p.category, p.subcategory, ST_AsGeoJSON(p.geom) AS geojson,
                       pp.distance_km, pp.bearing_deg, pp.direction, pp.band
                FROM project_pois pp
                JOIN pois p ON p.id = pp.poi_id
                WHERE pp.project_id = %s;
                """,
                (project_id,),
            )
            for poi in cur.fetchall():
                features.append({
                    "type": "Feature",
                    "geometry": json.loads(poi["geojson"]),
                    "properties": {
                        "feature_type": "poi",
                        "poi_id": poi["id"],
                        "name": poi["name"],
                        "category": poi["category"],
                        "subcategory": poi["subcategory"],
                        "distance_km": float(poi["distance_km"]),
                        "bearing_deg": float(poi["bearing_deg"]),
                        "direction": poi["direction"],
                        "band": poi["band"],
                    },
                })
    finally:
        conn.close()

    return {"type": "FeatureCollection", "features": features}


def validate_geojson_schema(fc: dict) -> bool:
    """Minimal structural validation — not a full RFC 7946 validator, but
    enough to catch a malformed export before it reaches the client.
    """
    if fc.get("type") != "FeatureCollection":
        return False
    if not isinstance(fc.get("features"), list):
        return False
    for feature in fc["features"]:
        if feature.get("type") != "Feature":
            return False
        if "geometry" not in feature or "type" not in feature.get("geometry", {}):
            return False
        if "properties" not in feature:
            return False
    return True
