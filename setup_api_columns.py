#!/usr/bin/env python3
"""
Populate and index database columns required by api_server.py and frontend web app.
"""
import psycopg2
import psycopg2.extras
import time
import re
from collections import defaultdict

DB_URI = "postgresql://rera_user:rera_password@localhost:5432/rera_db"

_PLOT_KEYS = {'Plot Type (Site Dimension in Mtr)', 'Plot Size', 'Plot Type'}
_CONFIG_KEYS = ['Unit Type', 'Type of Inventory', 'Plot Type (Site Dimension in Mtr)', 'Plot Size', 'Plot Type']
_SKIP_VALS = {'TOTAL', 'RESIDENTIAL', 'ODD', 'EVEN', 'ODD SITE', 'ODD SITES',
              'OTHER-ROOMS', 'OTHERS-ROOMS', 'EWS', '0.00', 'NA', 'N/A', ''}

def _normalize_config_label(raw: str, is_plot_key: bool = False):
    s = raw.strip()
    upper = s.upper()
    if is_plot_key or any(k in upper for k in ['PLOT SIZE', 'SITE DIMENSION', 'PLOTTED']):
        return 'Plots'
    if 'VILLA' in upper:
        return 'Villa'
    m = re.search(r'(\d+(?:\.\d+)?)\s*(?:BHK|BED(?:ROOM)?)', upper)
    if m:
        try:
            n = float(m.group(1))
        except ValueError:
            n = 0
        if n < 1:
            return None
        n_int = int(n)
        if n_int >= 4:
            return '4 BHK+'
        if n_int == 0:
            return None
        return f'{n_int} BHK'
    if any(k in upper for k in ['SHOP', 'COMMERCIAL', 'OFFICE', 'CONVENTION', 'MULTIPLEX',
                                  'FOOD COURT', 'AMENITY', 'STORE ROOM']):
        return None
    return s or None

def extract_configs(uploaded_docs, project_type: str = ''):
    if not uploaded_docs:
        return ['Plots'] if project_type and 'plot' in project_type.lower() else []
    rows_by_id = defaultdict(dict)
    for item in uploaded_docs or []:
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
                continue
            except ValueError:
                pass
            label = _normalize_config_label(raw, is_plot_key=(key in _PLOT_KEYS))
            if label:
                configs.add(label)
            break
    if not configs and project_type and 'plot' in project_type.lower():
        configs.add('Plots')
    order = {'1 BHK': 0, '2 BHK': 1, '3 BHK': 2, '4 BHK+': 3, 'Plots': 4, 'Villa': 5}
    return sorted(list(configs), key=lambda x: order.get(x, 99))

def main():
    print("=== Step 1: Connecting to local PostgreSQL database... ===")
    conn = psycopg2.connect(DB_URI, cursor_factory=psycopg2.extras.RealDictCursor)
    conn.autocommit = True

    with conn.cursor() as cur:
        print("=== Step 2: Ensuring all API table columns exist... ===")
        columns = [
            ("status", "TEXT"),
            ("project_type", "TEXT"),
            ("approval_year", "TEXT"),
            ("completion_year", "TEXT"),
            ("district", "TEXT"),
            ("latitude", "NUMERIC"),
            ("longitude", "NUMERIC"),
            ("coordinate_source", "TEXT"),
            ("search_text", "TEXT"),
            ("property_configs", "text[]"),
        ]
        for col_name, col_type in columns:
            cur.execute(f"ALTER TABLE rera_projects ADD COLUMN IF NOT EXISTS {col_name} {col_type};")

        print("=== Step 3: Populating indexed columns from JSONB data... ===")
        t0 = time.time()
        cur.execute("""
            UPDATE rera_projects
            SET 
                status = COALESCE(data->>'status', data->'project_overview_details'->>'Project Status', 'APPROVED'),
                project_type = COALESCE(data->'project_overview_details'->>'Project Sub Type', data->>'project_type', 'Residential/Group Housing'),
                approval_year = SUBSTRING(COALESCE(data->>'approved_on', data->'project_overview_details'->>'Approved On', '') FROM '\d{4}'),
                completion_year = SUBSTRING(COALESCE(data->>'proposed_completion_date', data->'project_overview_details'->>'Proposed Completion Date', '') FROM '\d{4}'),
                district = COALESCE(data->'project_location'->>'district', data->>'district', ''),
                latitude = CASE WHEN data->'project_location'->>'latitude' ~ '^-?[0-9]+(\.[0-9]+)?$' THEN (data->'project_location'->>'latitude')::numeric ELSE NULL END,
                longitude = CASE WHEN data->'project_location'->>'longitude' ~ '^-?[0-9]+(\.[0-9]+)?$' THEN (data->'project_location'->>'longitude')::numeric ELSE NULL END,
                coordinate_source = COALESCE(data->'project_location'->>'coordinate_source', 'district_centroid'),
                search_text = LOWER(CONCAT_WS(' ', project_name, firm_name, registration_no, acknowledgement_no, data->'project_location'->>'district', data->'project_location'->>'taluk'));
        """)
        print(f"  ✓ Columns populated in {time.time() - t0:.2f}s")

        print("=== Step 4: Extracting and migrating property_configs... ===")
        t0 = time.time()
        cur.execute("SELECT registration_no, data->'uploaded_documents' as docs, project_type FROM rera_projects;")
        rows = cur.fetchall()
        updates = []
        for r in rows:
            cfgs = extract_configs(r['docs'], r['project_type'] or '')
            updates.append((cfgs or None, r['registration_no']))

        psycopg2.extras.execute_batch(
            cur,
            "UPDATE rera_projects SET property_configs = %s WHERE registration_no = %s;",
            updates,
            page_size=1000
        )
        print(f"  ✓ Configs updated for {len(updates)} records in {time.time() - t0:.2f}s")

        print("=== Step 5: Creating GIN and B-Tree indexes for fast web search... ===")
        indexes = [
            ("idx_rera_status", "status"),
            ("idx_rera_district", "district"),
            ("idx_rera_project_type", "project_type"),
            ("idx_rera_search_text", "search_text"),
        ]
        for idx_name, col_name in indexes:
            cur.execute(f"CREATE INDEX IF NOT EXISTS {idx_name} ON rera_projects ({col_name});")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_rera_property_configs ON rera_projects USING gin (property_configs);")
        print("  ✓ All indexes ready.")

    conn.close()
    print("\n✅ API Columns Setup & Data Indexing Complete!")

if __name__ == "__main__":
    main()
