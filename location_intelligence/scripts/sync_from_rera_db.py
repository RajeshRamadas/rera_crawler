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

SRC_DSN = os.getenv("RERA_DB_URL", "postgresql://rera_user:rera_password@localhost:5432/rera_db")
DST_DSN = os.getenv("HOMEXO_DSN", os.getenv("HOMEXO_TEST_DSN", "postgresql://rera_user:rera_password@localhost:5432/homexo_location_test"))


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
    query = """
        SELECT 
            registration_no,
            COALESCE(project_name, 'RERA Property') AS name,
            (data->'project_location'->>'longitude')::float AS lon,
            (data->'project_location'->>'latitude')::float AS lat,
            data->'project_location'->>'district' AS city
        FROM rera_projects
        WHERE data->'project_location'->>'latitude' IS NOT NULL 
          AND data->'project_location'->>'latitude' != ''
          AND (data->'project_location'->>'latitude')::float BETWEEN 11.0 AND 19.0
          AND (data->'project_location'->>'longitude')::float BETWEEN 74.0 AND 79.5;
    """
    cur_src.execute(query)
    rows = cur_src.fetchall()
    print(f"[*] Discovered {len(rows)} valid geocoded properties.")

    upsert_query = """
        INSERT INTO rera_projects (rera_id, name, geom, status, city)
        VALUES (%s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326), 'active', %s)
        ON CONFLICT (rera_id) DO UPDATE SET
            name = EXCLUDED.name,
            geom = EXCLUDED.geom,
            city = EXCLUDED.city,
            updated_at = now();
    """

    synced_count = 0
    for reg_no, name, lon, lat, city in rows:
        cur_dst.execute(upsert_query, (reg_no, name, lon, lat, city))
        synced_count += 1

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
