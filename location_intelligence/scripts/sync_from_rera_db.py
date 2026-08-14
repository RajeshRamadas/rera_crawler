"""
RERA Database to Location Intelligence Sync Script
===================================================
Extracts geocoded properties (with valid lat/long coordinates) from the main
RERA crawler database (rera_db) and upserts them into the Location Intelligence
PostGIS engine (homexo_location_test / HOMEXO_DSN).

Usage:
  python3 scripts/sync_from_rera_db.py
"""

import os
import sys
import psycopg2
from psycopg2.extras import execute_values
from config import settings

SRC_DSN = settings.rera_db_url
DST_DSN = settings.dsn



def sync_properties(src_dsn: str, dst_dsn: str):
    print(f"[*] Connecting to source database: {src_dsn}")
    try:
        conn_src = psycopg2.connect(src_dsn)
    except Exception as exc:
        print(f"[!] Error connecting to source database: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"[*] Connecting to target database: {dst_dsn}")
    try:
        conn_dst = psycopg2.connect(dst_dsn)
    except Exception as exc:
        print(f"[!] Error connecting to target database: {exc}", file=sys.stderr)
        sys.exit(1)

    cur_src = conn_src.cursor()
    cur_dst = conn_dst.cursor()

    print("[*] Fetching geocoded RERA properties from rera_db...")
    cur_src.execute("SELECT column_name FROM information_schema.columns WHERE table_name='rera_projects';")
    cols = {r[0] for r in cur_src.fetchall()}

    reg_col = "registration_no" if "registration_no" in cols else ("rera_id" if "rera_id" in cols else "id::text")
    name_col = "project_name" if "project_name" in cols else "name"

    firm_col = "firm_name" if "firm_name" in cols else "NULL"

    if "data" in cols:
        query = f"""
            SELECT 
                {reg_col},
                COALESCE({name_col}, 'RERA Property') AS name,
                (data->'project_location'->>'longitude')::float AS lon,
                (data->'project_location'->>'latitude')::float AS lat,
                data->'project_location'->>'district' AS city,
                COALESCE({firm_col}, data->'promoter_details'->'Promoter Details'->>'Promoter Name', 'RERA Developer') AS developer,
                data::text AS rera_data
            FROM rera_projects
            WHERE data->'project_location'->>'latitude' IS NOT NULL 
              AND data->'project_location'->>'latitude' != '';
        """
    else:
        query = f"""
            SELECT 
                {reg_col},
                COALESCE({name_col}, 'RERA Property') AS name,
                ST_X(geom) AS lon,
                ST_Y(geom) AS lat,
                city,
                'RERA Developer' AS developer,
                NULL AS rera_data
            FROM rera_projects
            WHERE geom IS NOT NULL;
        """

    cur_src.execute(query)
    rows = cur_src.fetchall()
    print(f"[*] Discovered {len(rows)} valid geocoded properties.")

    upsert_query = """
        INSERT INTO rera_projects (rera_id, name, geom, status, city, developer, rera_data)
        VALUES %s
        ON CONFLICT (rera_id) DO UPDATE SET
            name = EXCLUDED.name,
            geom = EXCLUDED.geom,
            city = EXCLUDED.city,
            developer = EXCLUDED.developer,
            rera_data = EXCLUDED.rera_data,
            updated_at = now();
    """

    template = "(%s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326), 'active', %s, %s, %s::jsonb)"

    if rows:
        execute_values(cur_dst, upsert_query, rows, template=template, page_size=1000)
        synced_count = len(rows)
    else:
        synced_count = 0

    conn_dst.commit()



    # Get final total count in destination
    cur_dst.execute("SELECT COUNT(*) FROM rera_projects WHERE status = 'active';")
    total_in_dst = cur_dst.fetchone()[0]

    conn_src.close()
    conn_dst.close()

    print(f"[+] Successfully synced {synced_count} geocoded properties.")
    print(f"[+] Total active properties in Location Intelligence engine: {total_in_dst}")


if __name__ == "__main__":
    sync_properties(SRC_DSN, DST_DSN)
