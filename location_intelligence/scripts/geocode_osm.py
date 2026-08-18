#!/usr/bin/env python3
"""
OpenStreetMap (Nominatim) Batch Geocoder for RERA Projects.
Geocodes RERA projects missing location coordinates, updating PostgreSQL database
with latitude, longitude, and coordinate_source = 'osm_nominatim'.
"""

import os
import sys
import time
import urllib.parse
import psycopg2
import psycopg2.extras
import requests

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://rera_user:rera_password@localhost:5432/rera_db")
NOMINATIM_URL = os.getenv("NOMINATIM_URL", "https://nominatim.openstreetmap.org/search")
USER_AGENT = "RERA-Location-Intelligence/1.0 (contact@rera-audit.local)"

def get_db_connection():
    try:
        return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    except Exception as e:
        print(f"Database connection error: {e}")
        return None

def geocode_osm_single(project_name, taluk, district):
    """
    Queries Nominatim API using fallback search variations.
    Returns (lat, lng, display_name) or (None, None, None).
    """
    headers = {"User-Agent": USER_AGENT}
    
    variations = [
        f"{project_name}, {taluk or ''}, {district or ''}, Karnataka, India",
        f"{project_name}, {district or ''}, Karnataka, India",
        f"{taluk or ''}, {district or ''}, Karnataka, India" if taluk else None
    ]

    for query in filter(None, variations):
        clean_q = ", ".join([p.strip() for p in query.split(",") if p.strip()])
        params = {
            "q": clean_q,
            "format": "json",
            "limit": 1
        }

        try:
            res = requests.get(NOMINATIM_URL, params=params, headers=headers, timeout=5)
            if res.status_code == 200:
                data = res.json()
                if data and len(data) > 0:
                    lat = float(data[0]["lat"])
                    lng = float(data[0]["lon"])
                    display_name = data[0].get("display_name", "")
                    # Verify lat/lng within Karnataka bounding box
                    if 11.5 <= lat <= 18.5 and 74.0 <= lng <= 78.6:
                        return lat, lng, display_name
        except Exception as e:
            pass

        time.sleep(1.1) # Respect 1 req/sec rate limit

    return None, None, None

def run_osm_geocoder(limit=100):
    conn = get_db_connection()
    if not conn:
        print("Failed to connect to DB.")
        return

    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT registration_no, project_name, firm_name, district, data->'project_location'->>'taluk' as taluk
                FROM rera_projects
                WHERE latitude IS NULL
                ORDER BY registration_no
                LIMIT %s;
            """, (limit,))
            rows = cur.fetchall()

        print(f"Found {len(rows)} projects missing location data. Processing via OpenStreetMap Nominatim...")

        success_count = 0
        failed_count = 0

        for idx, r in enumerate(rows, 1):
            reg_no = r["registration_no"]
            pname = r["project_name"] or ""
            district = r["district"] or ""
            taluk = r["taluk"] or ""

            lat, lng, display_name = geocode_osm_single(pname, taluk, district)

            if lat and lng:
                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE rera_projects
                        SET latitude = %s,
                            longitude = %s,
                            coordinate_source = 'osm_nominatim'
                        WHERE registration_no = %s;
                    """, (lat, lng, reg_no))
                success_count += 1
                print(f"[{idx}/{len(rows)}] [SUCCESS: OSM Nominatim] {reg_no} -> ({lat:.4f}, {lng:.4f})")
            else:
                failed_count += 1
                print(f"[{idx}/{len(rows)}] [NO MATCH] {reg_no} -> {pname[:30]}")

        print(f"\nBatch processing complete! OSM Geocoded: {success_count}, Failed/Unresolved: {failed_count}")

    finally:
        conn.close()

if __name__ == "__main__":
    limit_arg = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    run_osm_geocoder(limit_arg)
