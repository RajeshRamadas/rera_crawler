"""Stage 8 — Location API (read-only).

Implements: API-01, UX-01-UX-03.

Every endpoint here is a read against tables Stages 4-7 already
populated (pois, project_pois, location_scores, project_usps) — never a
call into intelligence/scoring.py, intelligence/relationships.py, or
poi/normalizer.py. That's not just a convention: it's why
tests/test_location_api.py can monkeypatch category_score to raise and
still expect /location to return 200 — the code path genuinely never
touches it (API-01's "zero inline computation" requirement).

The one exception is /location/geojson and /location/map.png, which do
run a query at request time (ring buffering, tile compositing) — but
neither one re-derives a distance, bearing, score, or USP; they format
data that Stages 5-6 already computed and stored. map.png additionally
relies on Stage 7's own cache, so a warm cache hit there is also just a
file read, not a render.
"""
from contextlib import asynccontextmanager, contextmanager
import json
import os
from typing import Optional

import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool
from fastapi import FastAPI, HTTPException, Query, Response

from maps.geojson import build_geojson
from maps.static_map import render_static_map

DSN = os.environ.get(
    "HOMEXO_DSN",
    os.environ.get("HOMEXO_TEST_DSN", "host=localhost dbname=homexo_dev user=homexo password=homexo")
)
DEFAULT_MODEL_VERSION = "location-v1"
DEFAULT_TILE_SERVER_URL = os.environ.get("HOMEXO_TILE_SERVER_URL", "http://localhost:8080")

_db_pool: Optional[ThreadedConnectionPool] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    global _db_pool
    if _db_pool is not None and not _db_pool.closed:
        _db_pool.closeall()


app = FastAPI(title="HOMEXO Location Intelligence API", lifespan=lifespan)


def get_db_pool() -> ThreadedConnectionPool:
    global _db_pool
    if _db_pool is None or _db_pool.closed:
        _db_pool = ThreadedConnectionPool(minconn=2, maxconn=20, dsn=DSN)
    return _db_pool


@contextmanager
def get_db_conn():
    pool = get_db_pool()
    conn = pool.getconn()
    try:
        yield conn
    finally:
        pool.putconn(conn)



def _require_project(cur, project_id: int) -> dict:
    cur.execute("SELECT id, name, status, city FROM rera_projects WHERE id = %s;", (project_id,))
    row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"No project with id={project_id}")
    return dict(row)



@app.get("/api/projects/{project_id}/location")
def get_location(project_id: int, model_version: str = DEFAULT_MODEL_VERSION):
    """Score summary + top USPs + top POIs — a pure read, no scoring call."""
    with get_db_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            project = _require_project(cur, project_id)

            cur.execute(
                "SELECT category, score FROM location_scores WHERE project_id = %s AND model_version = %s;",
                (project_id, model_version),
            )
            score_rows = cur.fetchall()
            category_scores = {r["category"]: float(r["score"]) for r in score_rows if r["category"] != "overall"}
            overall = next((float(r["score"]) for r in score_rows if r["category"] == "overall"), None)

            cur.execute(
                """
                SELECT pu.rank, pu.headline_text, p.id AS poi_id, p.name, p.category, pp.distance_km
                FROM project_usps pu
                JOIN pois p ON p.id = pu.poi_id
                JOIN project_pois pp ON pp.project_id = pu.project_id AND pp.poi_id = pu.poi_id
                WHERE pu.project_id = %s AND pu.model_version = %s
                ORDER BY pu.rank ASC;
                """,
                (project_id, model_version),
            )
            top_usps = []
            for r in cur.fetchall():
                d = dict(r)
                d["distance_km"] = float(d["distance_km"])
                top_usps.append(d)

            cur.execute(
                """
                SELECT p.id AS poi_id, p.name, p.category, p.subcategory,
                       pp.distance_km, pp.direction, pp.band
                FROM project_pois pp
                JOIN pois p ON p.id = pp.poi_id
                WHERE pp.project_id = %s
                ORDER BY pp.distance_km ASC
                LIMIT 20;
                """,
                (project_id,),
            )
            pois = []
            for r in cur.fetchall():
                d = dict(r)
                d["distance_km"] = float(d["distance_km"])
                pois.append(d)

            cur.execute(
                "SELECT ST_AsGeoJSON(geom) AS geojson, validated FROM project_boundaries WHERE project_id = %s;",
                (project_id,),
            )
            boundary_row = cur.fetchone()
            boundary = None
            if boundary_row and boundary_row["geojson"]:
                boundary = {"geometry": json.loads(boundary_row["geojson"]), "validated": boundary_row["validated"]}

    return {
        "project_id": project["id"],
        "location_score": overall,
        "category_scores": category_scores,
        "top_usps": top_usps,
        "pois": pois,
        "boundary": boundary,
        "map": {"url": f"/api/projects/{project_id}/location/map.png"},
        "model_version": model_version,
    }


@app.get("/api/projects/{project_id}/location/pois")
def get_pois(
    project_id: int,
    category: Optional[str] = None,
    band: Optional[str] = None,
    min_distance_km: Optional[float] = None,
    max_distance_km: Optional[float] = None,
    limit: int = Query(default=200, le=1000),
):
    """Full filterable POI list — still a pure read, no scoring/geo call."""
    with get_db_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            _require_project(cur, project_id)

            query = """
                SELECT p.id AS poi_id, p.name, p.category, p.subcategory, p.confidence,
                       pp.distance_km, pp.bearing_deg, pp.direction, pp.band
                FROM project_pois pp
                JOIN pois p ON p.id = pp.poi_id
                WHERE pp.project_id = %s
            """
            params: list = [project_id]
            if category:
                query += " AND p.category = %s"
                params.append(category)
            if band:
                query += " AND pp.band = %s"
                params.append(band)
            if min_distance_km is not None:
                query += " AND pp.distance_km >= %s"
                params.append(min_distance_km)
            if max_distance_km is not None:
                query += " AND pp.distance_km <= %s"
                params.append(max_distance_km)
            query += " ORDER BY pp.distance_km ASC LIMIT %s;"
            params.append(limit)

            cur.execute(query, params)
            rows = []
            for r in cur.fetchall():
                d = dict(r)
                d["distance_km"] = float(d["distance_km"])
                d["bearing_deg"] = float(d["bearing_deg"])
                d["confidence"] = float(d["confidence"])
                rows.append(d)

    return {"project_id": project_id, "count": len(rows), "pois": rows}


@app.get("/api/projects/{project_id}/location/geojson")
def get_geojson(project_id: int):
    """Formats already-computed project_pois into GeoJSON — no distance,
    bearing, or score is re-derived here, only PostGIS ring buffering
    (deterministic geometry formatting) on the project's stored point.
    """
    with get_db_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            _require_project(cur, project_id)

    return build_geojson(DSN, project_id)


@app.get("/api/projects/{project_id}/location/map.png")
def get_map_png(project_id: int, tile_server_url: str = DEFAULT_TILE_SERVER_URL):
    """Serves the cached static map (Stage 7); a cache hit is a plain file
    read. A cache miss re-renders — matches design doc §3.5's own "cached;
    invalidated only when relationships/scores change" behavior, not a
    violation of the read-path's no-live-computation rule (nothing here
    recomputes a score or relationship, only composites an image from
    what's already stored).
    """
    with get_db_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            _require_project(cur, project_id)

    meta = render_static_map(DSN, project_id, tile_server_url=tile_server_url)
    with open(meta.image_path, "rb") as f:
        image_bytes = f.read()
    return Response(content=image_bytes, media_type="image/png")

