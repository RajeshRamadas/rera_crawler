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
from typing import Any, Dict, List, Optional

import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool
from fastapi import FastAPI, HTTPException, Query, Response
from pydantic import BaseModel, Field

import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))
try:
    from project_analyzer import analyze_by_name_or_reg
except ImportError:
    analyze_by_name_or_reg = None

from config import settings
from maps.geojson import build_geojson
from maps.static_map import render_static_map

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
        _db_pool = ThreadedConnectionPool(minconn=2, maxconn=20, dsn=settings.dsn)
    return _db_pool


@contextmanager
def get_db_conn():
    pool = get_db_pool()
    conn = pool.getconn()
    try:
        yield conn
    finally:
        pool.putconn(conn)


# ── Pydantic Response Schemas ──────────────────────────────────────────────────

class USPItem(BaseModel):
    rank: int
    headline_text: str
    poi_id: int
    name: Optional[str] = None
    category: str
    distance_km: float


class POIItem(BaseModel):
    poi_id: int
    name: Optional[str] = None
    category: str
    subcategory: Optional[str] = None
    distance_km: float
    direction: Optional[str] = None
    band: Optional[str] = None
    bearing_deg: Optional[float] = None
    confidence: Optional[float] = None


class BoundaryItem(BaseModel):
    geometry: Dict[str, Any]
    validated: Optional[bool] = True


class MapInfo(BaseModel):
    url: str


class LocationSummaryResponse(BaseModel):
    project_id: int
    location_score: Optional[float] = None
    category_scores: Dict[str, float]
    top_usps: List[USPItem]
    pois: List[POIItem]
    boundary: Optional[BoundaryItem] = None
    map: MapInfo
    model_version: str


class POIListResponse(BaseModel):
    project_id: int
    count: int
    pois: List[POIItem]


class DeveloperProjectSummary(BaseModel):
    project_id: int
    rera_id: str
    name: str
    developer: Optional[str] = None
    city: Optional[str] = None
    status: Optional[str] = None
    location_score: Optional[float] = None
    map_url: str


class DeveloperProjectsResponse(BaseModel):
    developer: str
    total_projects: int
    projects: List[DeveloperProjectSummary]


class DeveloperItem(BaseModel):
    developer: str
    project_count: int


class DeveloperListResponse(BaseModel):
    total_developers: int
    developers: List[DeveloperItem]


class FullProjectDetailResponse(BaseModel):
    project_id: int
    rera_id: str
    name: str
    developer: Optional[str] = None
    city: Optional[str] = None
    status: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    rera_details: Optional[Dict[str, Any]] = None
    location_score: Optional[float] = None
    category_scores: Dict[str, float]
    top_usps: List[USPItem]
    pois: List[POIItem]
    boundary: Optional[BoundaryItem] = None
    map: MapInfo
    model_version: str


def _require_project(cur, project_id: int) -> dict:
    cur.execute("SELECT id, name, status, city FROM rera_projects WHERE id = %s;", (project_id,))
    row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"No project with id={project_id}")
    return dict(row)


def _get_project_by_identifier(cur, identifier: str) -> dict:
    if identifier.isdigit():
        cur.execute(
            "SELECT id, rera_id, name, developer, status, city, ST_X(geom) AS lon, ST_Y(geom) AS lat, rera_data FROM rera_projects WHERE id = %s;",
            (int(identifier),),
        )
    else:
        cur.execute(
            "SELECT id, rera_id, name, developer, status, city, ST_X(geom) AS lon, ST_Y(geom) AS lat, rera_data FROM rera_projects WHERE rera_id = %s OR name ILIKE %s LIMIT 1;",
            (identifier, f"%{identifier}%"),
        )
    row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"No project found matching '{identifier}'")
    return dict(row)



# ── Developer Endpoints ─────────────────────────────────────────────────────────

@app.get("/api/developers", response_model=DeveloperListResponse)
def list_developers():
    """Returns a list of all developers with their project counts."""
    with get_db_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                """
                SELECT COALESCE(developer, 'Unknown Developer') AS developer, count(*) AS project_count
                FROM rera_projects
                GROUP BY COALESCE(developer, 'Unknown Developer')
                ORDER BY project_count DESC, developer ASC;
                """
            )
            rows = [dict(r) for r in cur.fetchall()]
    return {"total_developers": len(rows), "developers": rows}


@app.get("/api/developers/projects", response_model=DeveloperProjectsResponse)
@app.get("/api/developers/{developer_name}/projects", response_model=DeveloperProjectsResponse)
def get_developer_projects(developer: Optional[str] = None, developer_name: Optional[str] = None, model_version: str = settings.default_model_version):
    """Given a developer name (query param or path param), returns all projects by that developer
    along with location scores and map links.
    """
    target_developer = developer_name or developer
    if not target_developer:
        raise HTTPException(status_code=400, detail="Must provide 'developer' parameter or path variable.")

    with get_db_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                """
                SELECT p.id, p.rera_id, p.name, p.developer, p.status, p.city,
                       (SELECT score FROM location_scores WHERE project_id = p.id AND category = 'overall' AND model_version = %s LIMIT 1) AS location_score
                FROM rera_projects p
                WHERE p.developer ILIKE %s
                ORDER BY p.name ASC;
                """,
                (model_version, f"%{target_developer}%"),
            )
            rows = cur.fetchall()

            projects = []
            for r in rows:
                p_dict = dict(r)
                loc_score = float(p_dict["location_score"]) if p_dict["location_score"] is not None else None
                projects.append({
                    "project_id": p_dict["id"],
                    "rera_id": p_dict["rera_id"],
                    "name": p_dict["name"],
                    "developer": p_dict["developer"],
                    "city": p_dict["city"],
                    "status": p_dict["status"],
                    "location_score": loc_score,
                    "map_url": f"/api/projects/{p_dict['id']}/location/map.png",
                })

    matched_dev_name = projects[0]["developer"] if projects else target_developer
    return {
        "developer": matched_dev_name or target_developer,
        "total_projects": len(projects),
        "projects": projects,
    }


# ── Full Project Details & Analysis Endpoints ─────────────────────────────────────

@app.get("/api/projects/{identifier}/analysis", summary="Analyze project info, positives and negatives")
def get_project_analysis(identifier: str):
    """Given a project name, ID, or registration number, returns full analysis with Positives and Negatives."""
    if analyze_by_name_or_reg is None:
        raise HTTPException(status_code=500, detail="Project analyzer engine not loaded.")
    res = analyze_by_name_or_reg(identifier)
    if res.get("error"):
        raise HTTPException(status_code=404, detail=res.get("message"))
    return res


@app.get("/api/projects/{identifier}/full", response_model=FullProjectDetailResponse)
def get_full_project_detail(identifier: str, model_version: str = settings.default_model_version):
    """Given a project ID or RERA Registration Number or Name, returns full project information
    including developer, location lat/lon, overall & category scores, top location USPs, key POIs,
    site boundary, and static map URL.
    """
    with get_db_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            project = _get_project_by_identifier(cur, identifier)
            project_id = project["id"]

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

    lat = float(project["lat"]) if project.get("lat") is not None else None
    lon = float(project["lon"]) if project.get("lon") is not None else None

    rera_details = project.get("rera_data")
    if rera_details is None and project.get("rera_id"):
        try:
            with psycopg2.connect(settings.rera_db_url) as src_conn:
                with src_conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as src_cur:
                    src_cur.execute(
                        "SELECT data FROM rera_projects WHERE registration_no = %s LIMIT 1;",
                        (project["rera_id"],),
                    )
                    src_row = src_cur.fetchone()
                    if src_row and src_row["data"]:
                        rera_details = src_row["data"] if isinstance(src_row["data"], dict) else json.loads(src_row["data"])
        except Exception as exc:
            pass


    return {
        "project_id": project["id"],
        "rera_id": project["rera_id"],
        "name": project["name"],
        "developer": project.get("developer"),
        "city": project.get("city"),
        "status": project.get("status"),
        "latitude": lat,
        "longitude": lon,
        "rera_details": rera_details,
        "location_score": overall,
        "category_scores": category_scores,
        "top_usps": top_usps,
        "pois": pois,
        "boundary": boundary,
        "map": {"url": f"/api/projects/{project['id']}/location/map.png"},
        "model_version": model_version,
    }



@app.get("/api/projects/{project_id}/location", response_model=LocationSummaryResponse)
def get_location(project_id: int, model_version: str = settings.default_model_version):

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


@app.get("/api/projects/{project_id}/location/pois", response_model=POIListResponse)
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

    return build_geojson(settings.dsn, project_id)


@app.get("/api/projects/{project_id}/location/map.png")
def get_map_png(project_id: int, tile_server_url: str = settings.tile_server_url):
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

    meta = render_static_map(settings.dsn, project_id, tile_server_url=tile_server_url)
    with open(meta.image_path, "rb") as f:
        image_bytes = f.read()
    return Response(content=image_bytes, media_type="image/png")


