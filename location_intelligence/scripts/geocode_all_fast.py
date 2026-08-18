#!/usr/bin/env python3
"""
Fast OpenStreetMap Bulk Geocoder for RERA Projects.
Geocodes distinct PIN codes and Taluks across Karnataka, then updates all 4,900+ missing
projects in PostgreSQL in a single fast execution pass.
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
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)

def geocode_query(q):
    headers = {"User-Agent": USER_AGENT}
    url = f"{NOMINATIM_URL}?q={urllib.parse.quote(q)}&format=json&limit=1"
    try:
        res = requests.get(url, headers=headers, timeout=5)
        if res.status_code == 200:
            data = res.json()
            if data and len(data) > 0:
                lat = float(data[0]["lat"])
                lng = float(data[0]["lon"])
                if 11.5 <= lat <= 18.5 and 74.0 <= lng <= 78.6:
                    return lat, lng
    except Exception:
        pass
    return None, None

def run_fast_bulk_geocoding():
    conn = get_db_connection()
    conn.autocommit = True

    print("=== STARTING FAST BULK OPENSTREETMAP GEOCODING ===")
    
    # Step 1: Geocode by PIN Codes (highest specificity for unmapped projects)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT data->'project_location'->>'pin_code' as pin_code
            FROM rera_projects
            WHERE latitude IS NULL AND data->'project_location'->>'pin_code' IS NOT NULL
            AND LENGTH(TRIM(data->'project_location'->>'pin_code')) = 6;
        """)
        pincodes = [r["pin_code"].strip() for r in cur.fetchall() if r["pin_code"]]

    print(f"Found {len(pincodes)} unique PIN codes to geocode...")
    pin_success = 0

    for idx, pin in enumerate(pincodes, 1):
        q = f"{pin}, Karnataka, India"
        lat, lng = geocode_query(q)
        time.sleep(1.05) # Rate limit requirement

        if lat and lng:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE rera_projects
                    SET latitude = %s,
                        longitude = %s,
                        coordinate_source = 'osm_nominatim'
                    WHERE latitude IS NULL
                    AND TRIM(data->'project_location'->>'pin_code') = %s;
                """, (lat, lng, pin))
                updated = cur.rowcount
                pin_success += updated
                print(f"[{idx}/{len(pincodes)}] PIN {pin} -> ({lat:.4f}, {lng:.4f}) | Updated {updated} projects")

    print(f"\nPIN Code Phase Complete! Updated {pin_success} projects.")

    # Step 2: Geocode remaining missing projects by Taluk + District
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT 
                COALESCE(data->'project_location'->>'taluk', data->>'taluk') as taluk,
                COALESCE(district, 'Karnataka') as district
            FROM rera_projects
            WHERE latitude IS NULL
            AND (data->'project_location'->>'taluk' IS NOT NULL OR district IS NOT NULL);
        """)
        taluk_pairs = cur.fetchall()

    print(f"\nFound {len(taluk_pairs)} unique Taluk/District pairs to geocode...")
    taluk_success = 0

    for idx, pair in enumerate(taluk_pairs, 1):
        taluk = pair["taluk"] or ""
        district = pair["district"] or ""
        q = f"{taluk}, {district}, Karnataka, India" if taluk else f"{district}, Karnataka, India"
        
        lat, lng = geocode_query(q)
        time.sleep(1.05)

        if lat and lng:
            with conn.cursor() as cur:
                if taluk:
                    cur.execute("""
                        UPDATE rera_projects
                        SET latitude = %s,
                            longitude = %s,
                            coordinate_source = 'osm_nominatim'
                        WHERE latitude IS NULL
                        AND (data->'project_location'->>'taluk' = %s OR data->>'taluk' = %s)
                        AND COALESCE(district, 'Karnataka') = %s;
                    """, (lat, lng, taluk, taluk, district))
                else:
                    cur.execute("""
                        UPDATE rera_projects
                        SET latitude = %s,
                            longitude = %s,
                            coordinate_source = 'osm_nominatim'
                        WHERE latitude IS NULL
                        AND COALESCE(district, 'Karnataka') = %s;
                    """, (lat, lng, district))

                updated = cur.rowcount
                taluk_success += updated
                print(f"[{idx}/{len(taluk_pairs)}] Taluk '{taluk}', '{district}' -> ({lat:.4f}, {lng:.4f}) | Updated {updated} projects")

    print("\n=== FINAL SUMMARY ===")
    with conn.cursor() as cur:
        cur.execute("SELECT COALESCE(coordinate_source, 'district_centroid_fallback'), COUNT(*) FROM rera_projects GROUP BY coordinate_source;")
        for r in cur.fetchall():
            print(f"  {r[0]:30}: {r[1]:,} projects")

    conn.close()

if __name__ == "__main__":
    run_fast_bulk_geocoding()
