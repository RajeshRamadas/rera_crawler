"""
RERA Karnataka PostgreSQL REST API Server
==========================================
Exposes production-ready REST endpoints for fetching scraped RERA project records,
searching by survey number/district/status, and querying spatial/analytics data.

Usage:
  # Install dependencies:
  pip install fastapi uvicorn psycopg2-binary

  # Run API server locally or on EC2:
  DATABASE_URL="postgresql://rera_user:rera_password@localhost:5432/rera_db" uvicorn api_server:app --host 0.0.0.0 --port 8000
"""

import os
import sys
import json
import urllib.parse
from typing import Optional, List
from datetime import datetime

try:
    from fastapi import FastAPI, HTTPException, Query, Response
    from fastapi.middleware.cors import CORSMiddleware
    import psycopg2
    import psycopg2.extras
except ImportError as err:
    print(f"[!] Missing required package: {err}", file=sys.stderr)
    print("[!] Install dependencies with: pip install fastapi uvicorn psycopg2-binary", file=sys.stderr)

DB_URI = os.getenv("DATABASE_URL", "postgresql://rera_user:rera_password@localhost:5432/rera_db")

app = FastAPI(
    title="RERA Karnataka Property API",
    description="REST API for querying RERA Karnataka scraped project data, spatial coordinates, documents, and analytics.",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# Enable CORS for web frontend integration (Next.js, React, Mobile Apps)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_db_connection():
    """Establish connection to PostgreSQL database."""
    try:
        conn = psycopg2.connect(DB_URI, cursor_factory=psycopg2.extras.RealDictCursor)
        return conn
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Database connection error: {str(exc)}"
        )


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #

@app.get("/health", summary="API & Database Healthcheck")
def healthcheck():
    """Verify API server operation and PostgreSQL connection health."""
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) as total_projects FROM rera_projects;")
            row = cur.fetchone()
        conn.close()
        return {
            "status": "healthy",
            "database": "connected",
            "total_projects": row["total_projects"] if row else 0,
            "timestamp": datetime.utcnow().isoformat()
        }
    except Exception as exc:
        return {
            "status": "unhealthy",
            "database_error": str(exc),
            "timestamp": datetime.utcnow().isoformat()
        }


@app.get("/api/v1/projects", summary="List and filter RERA projects")
def list_projects(
    district: Optional[str] = Query(None, description="Filter by district (e.g. Bengaluru Urban)"),
    status: Optional[str] = Query(None, description="Filter by project status (e.g. New Project Launch)"),
    promoter: Optional[str] = Query(None, description="Filter by promoter / builder name"),
    q: Optional[str] = Query(None, description="Search term matching project name or promoter name"),
    min_quality_score: Optional[int] = Query(None, ge=0, le=100, description="Filter by minimum data quality score (0-100)"),
    limit: int = Query(20, ge=1, le=100, description="Page limit (default: 20, max: 100)"),
    offset: int = Query(0, ge=0, description="Offset index for pagination"),
):
    """Fetch paginated RERA projects with search and quality filters."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            query = """
                SELECT 
                    registration_no,
                    acknowledgement_no,
                    project_name,
                    firm_name as promoter_name,
                    crawled_at,
                    data->'status' as status,
                    data->'district' as district,
                    data->'taluk' as taluk,
                    data->'project_location'->>'latitude' as latitude,
                    data->'project_location'->>'longitude' as longitude,
                    data->'project_location'->>'coordinate_source' as coordinate_source,
                    (data->'data_quality'->>'quality_score')::int as quality_score,
                    (data->'data_quality'->>'is_complete')::boolean as is_complete
                FROM rera_projects
                WHERE 1=1
            """
            params = []

            if district:
                query += " AND LOWER(data->'project_location'->>'district') LIKE %s"
                params.append(f"%{district.lower()}%")

            if status:
                query += " AND LOWER(data->>'status') LIKE %s"
                params.append(f"%{status.lower()}%")

            if promoter:
                query += " AND LOWER(firm_name) LIKE %s"
                params.append(f"%{promoter.lower()}%")

            if q:
                query += " AND (LOWER(project_name) LIKE %s OR LOWER(firm_name) LIKE %s)"
                params.append(f"%{q.lower()}%")
                params.append(f"%{q.lower()}%")

            if min_quality_score is not None:
                query += " AND (data->'data_quality'->>'quality_score')::int >= %s"
                params.append(min_quality_score)

            query += " ORDER BY crawled_at DESC LIMIT %s OFFSET %s;"
            params.extend([limit, offset])

            cur.execute(query, params)
            rows = cur.fetchall()

            # Count total query matches for pagination metadata
            count_query = "SELECT COUNT(*) as total FROM rera_projects WHERE 1=1;"
            cur.execute(count_query)
            total_count = cur.fetchone()["total"]

        return {
            "total": total_count,
            "limit": limit,
            "offset": offset,
            "count": len(rows),
            "results": rows
        }
    finally:
        conn.close()


@app.get("/api/v1/projects/{registration_no:path}", summary="Get complete project details")
def get_project(registration_no: str):
    """Fetch full structured record for a project by registration number."""
    decoded_reg = urllib.parse.unquote(registration_no)
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT registration_no, acknowledgement_no, project_name, firm_name, crawled_at, data FROM rera_projects WHERE registration_no = %s;",
                (decoded_reg,)
            )
            row = cur.fetchone()

        if not row:
            raise HTTPException(status_code=404, detail=f"Project with registration_no '{decoded_reg}' not found.")

        return row["data"]
    finally:
        conn.close()


@app.get("/api/v1/projects/search/by-survey", summary="Search projects by survey number")
def search_by_survey(survey_no: str = Query(..., description="Survey number to search for (e.g. 59/1)")):
    """Find all projects associated with a given land survey number."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT 
                    registration_no,
                    project_name,
                    firm_name,
                    data->'project_location'->>'district' as district,
                    data->'project_location'->'survey_numbers' as survey_numbers
                FROM rera_projects
                WHERE EXISTS (
                    SELECT 1 FROM jsonb_array_elements_text(data->'project_location'->'survey_numbers') elem
                    WHERE elem ILIKE %s
                );
            """, (f"%{survey_no}%",))
            rows = cur.fetchall()

        return {
            "query_survey_no": survey_no,
            "count": len(rows),
            "results": rows
        }
    finally:
        conn.close()


@app.get("/api/v1/analytics/summary", summary="Get overall data quality and crawler analytics")
def get_analytics_summary():
    """Fetch summary analytics of scraped RERA projects, completeness ratios, and district breakdowns."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT 
                    COUNT(*) as total_projects,
                    COUNT(*) FILTER (WHERE (data->'data_quality'->>'is_complete')::boolean = true) as complete_projects,
                    COUNT(*) FILTER (WHERE (data->'data_quality'->>'is_complete')::boolean = false) as incomplete_projects,
                    AVG((data->'data_quality'->>'quality_score')::int) as avg_quality_score,
                    COUNT(*) FILTER (WHERE data->'project_location'->>'coordinate_source' IS NOT NULL) as geocoded_projects
                FROM rera_projects;
            """)
            summary_stats = cur.fetchone()

            cur.execute("""
                SELECT 
                    data->'project_location'->>'district' as district,
                    COUNT(*) as project_count
                FROM rera_projects
                GROUP BY data->'project_location'->>'district'
                ORDER BY project_count DESC;
            """)
            district_breakdown = cur.fetchall()

        return {
            "summary": summary_stats,
            "district_breakdown": district_breakdown,
            "generated_at": datetime.utcnow().isoformat()
        }
    finally:
        conn.close()


@app.get("/api/v1/projects/geojson", summary="Export properties as standard GeoJSON FeatureCollection")
def export_geojson(
    district: Optional[str] = Query(None, description="Filter by district"),
    coordinate_source: Optional[str] = Query(None, description="Filter by coordinate source ('rera_html' or 'google_maps_iframe')")
):
    """Export all geocoded projects as a standardized GeoJSON FeatureCollection for Mapbox, Leaflet, or QGIS."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            query = """
                SELECT 
                    registration_no,
                    project_name,
                    firm_name as promoter_name,
                    data->'status' as status,
                    data->'project_location'->>'district' as district,
                    (data->'project_location'->>'latitude')::float as latitude,
                    (data->'project_location'->>'longitude')::float as longitude,
                    data->'project_location'->>'coordinate_source' as coordinate_source,
                    data->'project_location'->'geometry'->'bbox_polygon' as bbox_polygon,
                    (data->'data_quality'->>'quality_score')::int as quality_score
                FROM rera_projects
                WHERE data->'project_location'->>'latitude' IS NOT NULL
                  AND data->'project_location'->>'longitude' IS NOT NULL
            """
            params = []
            if district:
                query += " AND LOWER(data->'project_location'->>'district') LIKE %s"
                params.append(f"%{district.lower()}%")
            if coordinate_source:
                query += " AND data->'project_location'->>'coordinate_source' = %s"
                params.append(coordinate_source)

            cur.execute(query, params)
            rows = cur.fetchall()

        features = []
        for r in rows:
            feature = {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [r["longitude"], r["latitude"]]
                },
                "properties": {
                    "registration_no": r["registration_no"],
                    "project_name": r["project_name"],
                    "promoter_name": r["promoter_name"],
                    "status": r["status"],
                    "district": r["district"],
                    "coordinate_source": r["coordinate_source"],
                    "quality_score": r["quality_score"],
                    "bbox_polygon": r["bbox_polygon"]
                }
            }
            features.append(feature)

        return {
            "type": "FeatureCollection",
            "features": features
        }
    finally:
        conn.close()


@app.get("/api/v1/documents", summary="Search project documents and download URLs")
def search_documents(
    doc_type: Optional[str] = Query(None, description="Document type filter (e.g. Encumbrance, Title, Plan)"),
    reg_no: Optional[str] = Query(None, description="Registration number filter"),
    limit: int = Query(50, ge=1, le=200, description="Limit results (default 50)")
):
    """Query uploaded property document URLs (Encumbrance Certificates, Title Deeds, Conversion Orders)."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            query = """
                SELECT 
                    p.registration_no,
                    p.project_name,
                    doc->>'document_type' as document_type,
                    doc->>'financial_year' as document_name,
                    doc->>'file' as file_identifier,
                    doc->'file_url' as file_urls
                FROM rera_projects p,
                     jsonb_array_elements(data->'uploaded_documents') doc
                WHERE 1=1
            """
            params = []
            if reg_no:
                query += " AND p.registration_no = %s"
                params.append(reg_no)
            if doc_type:
                query += " AND LOWER(doc->>'financial_year') LIKE %s"
                params.append(f"%{doc_type.lower()}%")

            query += " LIMIT %s;"
            params.append(limit)

            cur.execute(query, params)
            rows = cur.fetchall()

        return {
            "count": len(rows),
            "results": rows
        }
    finally:
        conn.close()


@app.get("/api/v1/promoters", summary="List real estate developers and promoter statistics")
def list_promoters(
    q: Optional[str] = Query(None, description="Search promoter by name"),
    limit: int = Query(20, ge=1, le=100)
):
    """Directory of real estate promoters/builders with total project counts and quality ratings."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            query = """
                SELECT 
                    firm_name as promoter_name,
                    COUNT(*) as total_projects,
                    COUNT(*) FILTER (WHERE (data->'data_quality'->>'is_complete')::boolean = true) as complete_projects,
                    AVG((data->'data_quality'->>'quality_score')::int)::int as avg_quality_score
                FROM rera_projects
                WHERE firm_name IS NOT NULL AND firm_name != ''
            """
            params = []
            if q:
                query += " AND LOWER(firm_name) LIKE %s"
                params.append(f"%{q.lower()}%")

            query += " GROUP BY firm_name ORDER BY total_projects DESC LIMIT %s;"
            params.append(limit)

            cur.execute(query, params)
            rows = cur.fetchall()

        return {
            "count": len(rows),
            "results": rows
        }
    finally:
        conn.close()


@app.get("/api/v1/projects/search/by-subregistrar", summary="Search projects by Sub-Registrar jurisdiction")
def search_by_subregistrar(
    sro: str = Query(..., description="Sub Registrar Office name (e.g. Indiranagar, JP Nagar, Yelahanka)")
):
    """Lookup projects registered under a specific Sub-Registrar Office jurisdiction."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT 
                    registration_no,
                    project_name,
                    firm_name,
                    data->'project_location'->>'district' as district,
                    data->'project_location'->'sub_registrar_offices' as sub_registrar_offices
                FROM rera_projects
                WHERE EXISTS (
                    SELECT 1 FROM jsonb_array_elements_text(data->'project_location'->'sub_registrar_offices') elem
                    WHERE elem ILIKE %s
                );
            """, (f"%{sro}%",))
            rows = cur.fetchall()

        return {
            "query_sro": sro,
            "count": len(rows),
            "results": rows
        }
    finally:
        conn.close()


@app.get("/api/v1/complaints", summary="Search RERA complaints on projects or promoters")
def list_complaints(
    limit: int = Query(20, ge=1, le=100)
):
    """Retrieve projects that have registered complaints against the promoter or project."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT 
                    registration_no,
                    project_name,
                    firm_name,
                    data->'complaints'->'on_promoter' as complaints_on_promoter,
                    data->'complaints'->'on_project' as complaints_on_project
                FROM rera_projects
                WHERE jsonb_array_length(data->'complaints'->'on_promoter') > 0
                   OR jsonb_array_length(data->'complaints'->'on_project') > 0
                LIMIT %s;
            """, (limit,))
            rows = cur.fetchall()

        return {
            "count": len(rows),
            "results": rows
        }
    finally:
        conn.close()

