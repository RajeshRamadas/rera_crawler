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

import re
import ssl
import urllib.request
from fastapi.responses import FileResponse, StreamingResponse
from project_analyzer import search_project, analyze_project_data, analyze_by_name_or_reg
from legal_due_diligence_agent import KarnatakaPropertyLegalAgent, SAMPLE_PROPERTIES

legal_agent_instance = KarnatakaPropertyLegalAgent()

DB_URI = os.getenv("DATABASE_URL", "postgresql://rera_user:rera_password@localhost:5432/rera_db")

app = FastAPI(
    title="RERA Karnataka Property API",
    description="REST API for querying RERA Karnataka scraped project data, spatial coordinates, documents, analytics, and AI legal due-diligence.",
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


@app.get("/legal-report", summary="Karnataka Property Legal Due-Diligence Audit Report Dashboard")
@app.get("/legal-due-diligence", summary="Karnataka Property Legal Due-Diligence Audit Report Dashboard")
def get_legal_report_page():
    """Serves the single-file HTML Karnataka Property Legal Due-Diligence interactive report interface."""
    html_path = os.path.join(os.path.dirname(__file__), "property_legal_report.html")
    if not os.path.exists(html_path):
        raise HTTPException(status_code=404, detail="property_legal_report.html not found.")
    return FileResponse(html_path, media_type="text/html")


@app.get("/api/legal-due-diligence/sample", summary="Get Pre-configured Sample Property Legal Due-Diligence Reports")
def get_sample_legal_report(preset: str = Query("sarjapur_plot", description="Preset key: sarjapur_plot | whitefield_apartment | kengeri_illegal_plot")):
    """Returns evaluated legal due-diligence assessment for sample Karnataka properties."""
    if preset not in SAMPLE_PROPERTIES:
        raise HTTPException(status_code=404, detail=f"Preset '{preset}' not found. Available: {list(SAMPLE_PROPERTIES.keys())}")
    
    payload = SAMPLE_PROPERTIES[preset]
    return legal_agent_instance.run_due_diligence(payload)


@app.get("/api/legal-due-diligence/suggest", summary="Get Auto-Suggestions for Property Search")
def suggest_properties(q: str = Query("", description="Search term")):
    """Returns top matching project names from PostgreSQL database for live UI auto-complete."""
    if not q or len(q.strip()) < 2:
        return {"suggestions": []}

    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            clean_q = q.strip()
            cur.execute(
                "SELECT DISTINCT project_name, firm_name, registration_no FROM rera_projects WHERE project_name ILIKE %s OR firm_name ILIKE %s LIMIT 10;",
                (f"%{clean_q}%", f"%{clean_q}%")
            )
            rows = cur.fetchall()
            suggestions = []
            for r in rows:
                suggestions.append({
                    "project_name": r["project_name"],
                    "firm_name": r["firm_name"],
                    "registration_no": r["registration_no"]
                })
            conn.close()
            return {"suggestions": suggestions}
    except Exception as exc:
        return {"suggestions": [], "error": str(exc)}


@app.get("/api/legal-due-diligence/evaluate-query", summary="Search & Evaluate Real Property Legal Due-Diligence")
def evaluate_property_by_query(q: str = Query("Chartered Windsong - 2", description="Project Name or RERA Registration Number")):
    """
    Searches PostgreSQL RERA database for real scraped project details,
    extracts real uploaded document URLs, and returns complete 27-check legal assessment.
    """
    try:
        return legal_agent_instance.evaluate_project_query(q)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Legal Engine Error: {str(exc)}")


@app.post("/api/legal-due-diligence/evaluate", summary="Run AI Property Legal Due-Diligence Assessment")
def evaluate_property_legal_risk(payload: dict):
    """
    Evaluates property metadata across 27 Karnataka legal due-diligence checks,
    cross-verifies document sources, detects contradictions & missing evidence,
    and returns overall Legal Risk Score & BUY Gate Decision.
    """
    try:
        return legal_agent_instance.run_due_diligence(payload)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Legal Engine Error: {str(exc)}")


NO_CACHE_HEADERS = {
    "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0"
}


@app.get("/radar-chart", summary="Property Composite Risk & Quality Radar Dashboard")
def get_radar_chart():
    """Serves the single-file HTML property composite risk radar dashboard."""
    html_path = os.path.join(os.path.dirname(__file__), "property_radar_chart.html")
    if not os.path.exists(html_path):
        raise HTTPException(status_code=404, detail="property_radar_chart.html not found.")
    return FileResponse(html_path, media_type="text/html", headers=NO_CACHE_HEADERS)


@app.get("/register", summary="Property Risk & Compliance Register Listing")
@app.get("/listing", summary="Property Risk & Compliance Register Listing")
def get_property_listing():
    """Serves the single-file HTML Property Risk & Compliance Register dashboard."""
    html_path = os.path.join(os.path.dirname(__file__), "rera_property_listing.html")
    if not os.path.exists(html_path):
        raise HTTPException(status_code=404, detail="rera_property_listing.html not found.")
    return FileResponse(html_path, media_type="text/html", headers=NO_CACHE_HEADERS)


@app.get("/property-detail", summary="Detailed Property Audit & Compliance Page")
@app.get("/detail", summary="Detailed Property Audit & Compliance Page")
def get_property_detail_page():
    """Serves the single-file HTML Detailed Property Audit & Compliance page."""
    html_path = os.path.join(os.path.dirname(__file__), "property_detail.html")
    if not os.path.exists(html_path):
        raise HTTPException(status_code=404, detail="property_detail.html not found.")
    return FileResponse(html_path, media_type="text/html", headers=NO_CACHE_HEADERS)


@app.get("/compare", summary="Property Comparison Dashboard")
@app.get("/property-compare", summary="Property Comparison Dashboard")
def get_property_compare_page():
    """Serves the single-file HTML Property Comparison Dashboard page."""
    html_path = os.path.join(os.path.dirname(__file__), "property_compare.html")
    if not os.path.exists(html_path):
        raise HTTPException(status_code=404, detail="property_compare.html not found.")
    return FileResponse(html_path, media_type="text/html", headers=NO_CACHE_HEADERS)





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


from collections import defaultdict

# Canonical label set used both for extraction and filtering
_PLOT_KEYS = {'Plot Type (Site Dimension in Mtr)', 'Plot Size', 'Plot Type'}
_CONFIG_KEYS = ['Unit Type', 'Type of Inventory', 'Plot Type (Site Dimension in Mtr)', 'Plot Size', 'Plot Type']
_SKIP_VALS = {'TOTAL', 'RESIDENTIAL', 'ODD', 'EVEN', 'ODD SITE', 'ODD SITES',
              'OTHER-ROOMS', 'OTHERS-ROOMS', 'EWS', '0.00', 'NA', 'N/A', ''}

def _normalize_config_label(raw: str, is_plot_key: bool = False) -> str:
    """
    Normalise a raw inventory label into one of the canonical filter values:
      '1 BHK' | '2 BHK' | '3 BHK' | '4 BHK+' | 'Plots' | 'Villa'
    Returns None if the label is not recognisable as a unit configuration.
    """
    s = raw.strip()
    upper = s.upper()

    # Plot / site types
    if is_plot_key or any(k in upper for k in ['PLOT SIZE', 'SITE DIMENSION', 'PLOTTED']):
        return 'Plots'

    # Villa
    if 'VILLA' in upper:
        return 'Villa'

    # BHK patterns — search anywhere in the string (handles "TYPE-H - 3BHK+3T", etc.)
    import re as _re
    # Match "3 BHK", "3BHK", "3.5BHK", "3.5 BHK", "3 BEDROOM", "3 BED" etc.
    m = _re.search(r'(\d+(?:\.\d+)?)\s*(?:BHK|BED(?:ROOM)?)', upper)
    if m:
        try:
            n = float(m.group(1))
        except ValueError:
            n = 0
        if n < 1:
            return None
        n_int = int(n)  # 3.5BHK -> 3 BHK (closest canonical)
        if n_int >= 4:
            return '4 BHK+'
        if n_int == 0:
            return None
        return f'{n_int} BHK'

    # Shop / commercial unit inside a mixed project — skip
    if any(k in upper for k in ['SHOP', 'COMMERCIAL', 'OFFICE', 'CONVENTION', 'MULTIPLEX',
                                 'FOOD COURT', 'AMENITY', 'STORE ROOM']):
        return None

    # Return cleaned raw value for everything else
    return s or None


def extract_configurations(uploaded_docs, project_type: str = ''):
    """Return canonical configuration labels for display on property cards."""
    if not uploaded_docs:
        if project_type and 'plot' in project_type.lower():
            return ['Plots']
        return []

    rows_by_id = defaultdict(dict)
    for item in uploaded_docs:
        header = item.get('financial_year', '').strip()
        row_id = item.get('document_type', '')
        val = item.get('file', '').strip()
        rows_by_id[row_id][header] = val

    configs = set()
    for row_id, rdata in rows_by_id.items():
        for key in _CONFIG_KEYS:
            raw = rdata.get(key, '').strip()
            if not raw:
                continue
            upper = raw.upper()
            if upper in _SKIP_VALS:
                continue
            try:
                float(upper)
                continue  # pure number — skip
            except ValueError:
                pass
            is_plot = key in _PLOT_KEYS
            label = _normalize_config_label(raw, is_plot_key=is_plot)
            if label:
                configs.add(label)
            break  # found config key for this row — move on

    if not configs and project_type and 'plot' in project_type.lower():
        configs.add('Plots')

    # Canonical sort order
    order = {'1 BHK': 0, '2 BHK': 1, '3 BHK': 2, '4 BHK+': 3, 'Plots': 4, 'Villa': 5}
    return sorted(list(configs), key=lambda x: order.get(x, 99))


@app.get("/api/v1/projects", summary="List and filter RERA projects")
def list_projects(
    district: Optional[str] = Query(None, description="Filter by district (e.g. Bengaluru Urban)"),
    status: Optional[str] = Query(None, description="Filter by project status (e.g. APPROVED)"),
    promoter: Optional[str] = Query(None, description="Filter by promoter / builder name"),
    project_type: Optional[str] = Query(None, description="Filter by project / property type (e.g. Residential/Group Housing)"),
    approval_year: Optional[str] = Query(None, description="Filter by RERA approval year (e.g. 2024)"),
    completion_year: Optional[str] = Query(None, description="Filter by proposed completion year (e.g. 2028)"),
    property_config: Optional[str] = Query(None, description="Filter by property configuration (e.g. 3 BHK, Plots)"),
    q: Optional[str] = Query(None, description="Search term matching project name, promoter, or reg no"),
    min_quality_score: Optional[int] = Query(None, ge=0, le=100, description="Filter by minimum data quality score (0-100)"),
    limit: int = Query(20, ge=1, le=100, description="Page limit (default: 20, max: 100)"),
    offset: int = Query(0, ge=0, description="Offset index for pagination"),
):
    """Fetch paginated RERA projects with search and quality filters."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            where_clauses = ["project_name IS NOT NULL", "project_name != ''"]
            params = []

            if district:
                where_clauses.append("district LIKE %s")
                params.append(f"%{district.strip().lower()}%")

            if status:
                where_clauses.append("status = %s")
                params.append(status.strip().upper())

            if project_type:
                where_clauses.append("LOWER(project_type) LIKE %s")
                params.append(f"%{project_type.strip().lower()}%")

            if approval_year:
                where_clauses.append("approval_year = %s")
                params.append(approval_year.strip())

            if completion_year:
                where_clauses.append("completion_year = %s")
                params.append(completion_year.strip())

            if property_config:
                # Use the pre-computed GIN-indexed property_configs[] column for fast lookups.
                # Normalise the incoming value to match stored canonical labels.
                import re as _re
                pval = property_config.strip()
                pupper = pval.upper()
                # Map '4 BHK' / '4 BHK+' both to canonical '4 BHK+'
                m_bhk = _re.match(r'^(\d+)\s*BHK', pupper)
                if m_bhk:
                    n = int(m_bhk.group(1))
                    canonical = '4 BHK+' if n >= 4 else f'{n} BHK'
                elif pupper in ('PLOTS', 'PLOT'):
                    canonical = 'Plots'
                elif pupper in ('VILLA', 'VILLAS'):
                    canonical = 'Villa'
                else:
                    canonical = pval  # pass-through for any other value

                if canonical == 'Plots':
                    where_clauses.append(
                        "(%s = ANY(property_configs) OR LOWER(project_type) LIKE '%%plotted%%')"
                    )
                    params.append(canonical)
                else:
                    where_clauses.append("%s = ANY(property_configs)")
                    params.append(canonical)

            if promoter:
                where_clauses.append("LOWER(firm_name) LIKE %s")
                params.append(f"%{promoter.lower()}%")

            if q:
                where_clauses.append("search_text LIKE %s")
                params.append(f"%{q.strip().lower()}%")

            if min_quality_score is not None:
                where_clauses.append("(data->'data_quality'->>'quality_score')::int >= %s")
                params.append(min_quality_score)

            where_str = " AND ".join(where_clauses)

            data_query = f"""
                SELECT 
                    registration_no,
                    acknowledgement_no,
                    project_name,
                    firm_name as promoter_name,
                    crawled_at,
                    COALESCE(status, 'APPROVED') as status,
                    COALESCE(project_type, 'Residential/Group Housing') as project_type,
                    COALESCE(data->>'approved_on', approval_year) as approved_on,
                    COALESCE(data->'project_overview_details'->>'Project Start Date', data->>'project_start_date', data->>'start_date', data->>'commencement_date') as project_start_date,
                    COALESCE(data->>'proposed_completion_date', data->>'proposed_completion_date_at_registration', completion_year) as proposed_completion_date,
                    COALESCE(
                        (data->'completion_details' IS NOT NULL AND data->'completion_details' != '{{}}'::jsonb),
                        (data->>'completion_certificate_url' IS NOT NULL),
                        false
                    ) as completion_certificate_available,
                    COALESCE(district, 'Karnataka') as district,
                    COALESCE(data->'project_location'->>'taluk', data->>'taluk', '') as taluk,
                    latitude as latitude,
                    longitude as longitude,
                    CASE 
                        WHEN (coordinate_source IN ('rera_html', 'rera_website')) OR (latitude IS NOT NULL AND latitude != 0) THEN 'rera_website'
                        ELSE COALESCE(coordinate_source, 'district_centroid')
                    END as coordinate_source,
                    COALESCE((data->'data_quality'->>'quality_score')::int, 85) as quality_score,
                    COALESCE((data->'data_quality'->>'is_complete')::boolean, true) as is_complete,
                    COALESCE(property_configs, '{{}}') as property_configurations
                FROM rera_projects
                WHERE {where_str}
                ORDER BY crawled_at DESC LIMIT %s OFFSET %s;
            """
            cur.execute(data_query, params + [limit, offset])
            rows = cur.fetchall()

            for r in rows:
                r.pop('property_configs', None)

            count_query = f"SELECT COUNT(*) as total FROM rera_projects WHERE {where_str};"
            cur.execute(count_query, params)
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


@app.get("/api/v1/districts", summary="Get all distinct districts in the database")
def get_districts():
    """Returns a list of all unique districts found in registered projects."""
    conn = get_db_connection()
    if not conn:
        return {"districts": []}
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT INITCAP(district) as district
                FROM rera_projects
                WHERE district IS NOT NULL AND district != ''
                ORDER BY district;
            """)
            rows = cur.fetchall()
            districts = [r["district"] for r in rows if r.get("district")]
            return {"districts": districts}
    finally:
        conn.close()


@app.get("/api/v1/property-types", summary="Get all distinct property / project types")
def get_property_types():
    """Returns a list of all unique project/property types found in registered projects."""
    conn = get_db_connection()
    if not conn:
        return {"property_types": []}
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT project_type as p_type
                FROM rera_projects
                WHERE project_type IS NOT NULL AND project_type != ''
                ORDER BY p_type;
            """)
            rows = cur.fetchall()
            types = [r["p_type"] for r in rows if r.get("p_type")]
            return {"property_types": types}
    finally:
        conn.close()


@app.get("/api/v1/projects/analyze", summary="Analyze project information, positives and negatives")
def analyze_project(
    q: Optional[str] = Query(None, description="Search term for project name, promoter name, or registration number"),
    name: Optional[str] = Query(None, description="Project name"),
    persona: Optional[str] = Query("homebuyer", description="Evaluation persona ('homebuyer', 'lender', or 'investor')")
):
    """
    Given a project name or registration number, fetches all project details,
    analyzes regulatory approvals, filing completeness, timeline delays, complaints,
    and produces a comprehensive Positives (+) vs Negatives (-) Risk Evaluation Report
    tailored to the selected persona (Homebuyer, Lender, or Investor).
    """
    query_str = name or q
    if not query_str:
        raise HTTPException(status_code=400, detail="Must provide 'q' or 'name' parameter to analyze a project.")
    
    result = analyze_by_name_or_reg(query_str, persona=persona)
    if result.get("error"):
        raise HTTPException(status_code=404, detail=result.get("message"))
    result["property_configurations"] = extract_configurations(
        result.get("raw_data", {}).get("uploaded_documents", []),
        project_type=result.get("raw_data", {}).get("project_type", "")
    )
    return result


@app.get("/api/v1/projects/analyze/{query:path}", summary="Analyze project by path query")
def analyze_project_by_path(
    query: str,
    persona: Optional[str] = Query("homebuyer", description="Evaluation persona ('homebuyer', 'lender', or 'investor')")
):
    """
    Given a project name or registration number in path, fetches all project details and returns persona-tailored evaluation report.
    """
    decoded_q = urllib.parse.unquote(query)
    result = analyze_by_name_or_reg(decoded_q, persona=persona)
    if result.get("error"):
        raise HTTPException(status_code=404, detail=result.get("message"))
    result["property_configurations"] = extract_configurations(
        result.get("raw_data", {}).get("uploaded_documents", []),
        project_type=result.get("raw_data", {}).get("project_type", "")
    )
    return result


@app.get("/api/v1/promoters/{promoter_name:path}/track-record", summary="Get developer portfolio track record")
def get_promoter_track_record(promoter_name: str):
    """
    Returns historical portfolio delivery track record, delay rates, and complaint density for a developer.
    """
    decoded_name = urllib.parse.unquote(promoter_name)
    from project_analyzer import analyze_promoter_portfolio_track_record
    return analyze_promoter_portfolio_track_record(decoded_name)


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
                    COALESCE(status, 'APPROVED') as status,
                    COALESCE(district, 'Karnataka') as district,
                    latitude,
                    longitude,
                    COALESCE(coordinate_source, 'rera_website') as coordinate_source,
                    data->'project_location'->'geometry'->'bbox_polygon' as bbox_polygon,
                    COALESCE((data->'data_quality'->>'quality_score')::int, 85) as quality_score
                FROM rera_projects
                WHERE latitude IS NOT NULL AND longitude IS NOT NULL
            """
            params = []
            if district:
                query += " AND LOWER(district) LIKE %s"
                params.append(f"%{district.lower()}%")
            if coordinate_source:
                query += " AND coordinate_source = %s"
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



@app.get("/api/v1/documents/download", summary="Proxy and download PDF document attachment")
def download_document_proxy(
    url: str = Query(..., description="Target document download URL"),
    filename: Optional[str] = Query(None, description="Custom file name for the downloaded PDF")
):
    """
    Proxies and streams document downloads directly from RERA servers.
    Forces correct RFC-6266 / RFC-5987 'Content-Disposition: attachment; filename=...; filename*=UTF-8''...' headers
    to guarantee clean .pdf extension file downloads on Linux, Mac, and Windows desktop browsers.
    """
    clean_url = url
    if not clean_url.startswith("http://") and not clean_url.startswith("https://"):
        raise HTTPException(status_code=400, detail="Invalid URL scheme. Must start with http or https.")

    try:
        req = urllib.request.Request(
            clean_url,
            headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "application/pdf,application/octet-stream,*/*",
            }
        )
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        remote_res = urllib.request.urlopen(req, context=ctx, timeout=30)
        
        raw_name = filename or "RERA_Document"
        ascii_name = re.sub(r'[^a-zA-Z0-9_\-\.]', '_', raw_name)
        ascii_name = re.sub(r'_+', '_', ascii_name).strip('_')
        if not ascii_name.lower().endswith('.pdf'):
            ascii_name += '.pdf'

        utf8_encoded = urllib.parse.quote(raw_name)
        if not utf8_encoded.lower().endswith('.pdf'):
            utf8_encoded += '.pdf'

        disposition_val = f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{utf8_encoded}'

        def iterfile():
            try:
                while True:
                    chunk = remote_res.read(64 * 1024)
                    if not chunk:
                        break
                    yield chunk
            finally:
                try:
                    remote_res.close()
                except Exception:
                    pass

        headers = {
            "Content-Disposition": disposition_val,
            "Access-Control-Allow-Origin": "*",
            "Content-Type": "application/pdf"
        }
        
        content_length = remote_res.headers.get("Content-Length")
        if content_length:
            headers["Content-Length"] = content_length

        return StreamingResponse(iterfile(), media_type="application/pdf", headers=headers)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to stream document from RERA server: {str(exc)}")


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

