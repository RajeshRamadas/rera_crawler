#!/usr/bin/env python3
"""
High-Precision Exact-Name & Village OpenStreetMap Geocoder for RERA Projects.
Applies strict OSM feature class filtering (building/apartments/residential) for exact project names.
Falls back to Village/Hobli extraction from address fields when exact project name is unmapped.
"""

import os
import re
import sys
import time
import urllib.parse
import psycopg2
import psycopg2.extras
import requests

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://rera_user:rera_password@localhost:5432/rera_db")
NOMINATIM_URL = os.getenv("NOMINATIM_URL", "https://nominatim.openstreetmap.org/search")
USER_AGENT = "RERA-Location-Intelligence/1.0 (contact@rera-audit.local)"

PROPERTY_TYPES = {
    "building", "apartments", "residential", "house", "gated_community", 
    "estate", "housing", "construction", "commercial", "office"
}

def get_db_connection():
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)

def clean_project_name(name):
    """Strip phase, wing, block, tower, and stage numbers from project title."""
    if not name:
        return ""
    # Remove Phase/Wing/Block/Tower/Stage patterns
    cleaned = re.sub(r'(?i)\b(phase|wing|block|tower|stage|sec|sector|part|flat|unit|plot)\b.*$', '', name)
    cleaned = re.sub(r'\(.*?\)', '', cleaned)
    cleaned = re.sub(r'-.*$', '', cleaned)
    return cleaned.strip()

def extract_village_or_hobli(address):
    """Extract village, hobli, or locality from address text."""
    if not address:
        return None
    # Look for Village/Hobli keywords
    match = re.search(r'([A-Za-z\s]+?)\s+(Village|Hobli|Layout|Nagar|Colony)', address, re.IGNORECASE)
    if match:
        return match.group(0).strip()
    return None

def query_nominatim(q):
    headers = {"User-Agent": USER_AGENT}
    url = f"{NOMINATIM_URL}?q={urllib.parse.quote(q)}&format=json&limit=1"
    try:
        res = requests.get(url, headers=headers, timeout=5)
        if res.status_code == 200:
            data = res.json()
            if data and len(data) > 0:
                return data[0]
    except Exception:
        pass
    return None

def run_exact_and_village_geocoder(limit=100):
    conn = get_db_connection()
    conn.autocommit = True

    with conn.cursor() as cur:
        cur.execute("""
            SELECT 
                registration_no, 
                project_name, 
                district, 
                data->'project_location'->>'taluk' as taluk,
                data->'project_location'->>'address' as address
            FROM rera_projects
            WHERE latitude IS NULL
            ORDER BY registration_no
            LIMIT %s;
        """, (limit,))
        projects = cur.fetchall()

    print(f"Loaded {len(projects)} unmapped projects. Starting Step 1 (Exact-Name OSM) & Step 2 (Village Fallback)...")

    exact_matches = 0
    village_matches = 0
    unresolved = 0

    for idx, p in enumerate(projects, 1):
        reg_no = p["registration_no"]
        raw_name = p["project_name"] or ""
        clean_name = clean_project_name(raw_name)
        district = p["district"] or "Karnataka"
        taluk = p["taluk"] or ""
        address = p["address"] or ""

        time.sleep(1.05) # Nominatim 1 req/sec rate limit

        # Step 1: Query Exact Cleaned Project Name
        q_exact = f"{clean_name}, {district}, Karnataka, India"
        result = query_nominatim(q_exact)

        is_exact_match = False
        lat, lng, display_name = None, None, ""

        if result:
            res_class = result.get("class", "").lower()
            res_type = result.get("type", "").lower()
            disp_name = result.get("display_name", "").lower()
            
            # Check if match is a building/residential property (not a city/pincode centroid)
            if res_class in PROPERTY_TYPES or res_type in PROPERTY_TYPES or any(k in disp_name for k in ["apartment", "residency", "villas", "heights", "enclave", "gardens", "acres"]):
                lat = float(result["lat"])
                lng = float(result["lon"])
                display_name = result.get("display_name", "")
                if 11.5 <= lat <= 18.5 and 74.0 <= lng <= 78.6:
                    is_exact_match = True

        if is_exact_match:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE rera_projects
                    SET latitude = %s,
                        longitude = %s,
                        coordinate_source = 'osm_nominatim'
                    WHERE registration_no = %s;
                """, (lat, lng, reg_no))
            exact_matches += 1
            print(f"[{idx}/{len(projects)}] [STEP 1 EXACT MATCH] {reg_no} -> {clean_name[:25]} @ ({lat:.4f}, {lng:.4f})")
            continue

        # Step 2: Fallback to Village / Hobli / Locality extraction from Address
        loc_name = extract_village_or_hobli(address)
        if not loc_name and taluk:
            loc_name = f"{taluk} Taluk"

        if loc_name:
            q_village = f"{loc_name}, {district}, Karnataka, India"
            v_result = query_nominatim(q_village)
            if v_result:
                v_lat = float(v_result["lat"])
                v_lng = float(v_result["lon"])
                if 11.5 <= v_lat <= 18.5 and 74.0 <= v_lng <= 78.6:
                    with conn.cursor() as cur:
                        cur.execute("""
                            UPDATE rera_projects
                            SET latitude = %s,
                                longitude = %s,
                                coordinate_source = 'osm_village'
                            WHERE registration_no = %s;
                        """, (v_lat, v_lng, reg_no))
                    village_matches += 1
                    print(f"[{idx}/{len(projects)}] [STEP 2 VILLAGE MATCH] {reg_no} -> {loc_name} @ ({v_lat:.4f}, {v_lng:.4f})")
                    continue

        unresolved += 1
        print(f"[{idx}/{len(projects)}] [UNRESOLVED] {reg_no} -> {clean_name[:25]}")

    print(f"\nBatch Completed! Exact Project Matches: {exact_matches}, Village/Hobli Matches: {village_matches}, Unresolved: {unresolved}")
    conn.close()

if __name__ == "__main__":
    limit_arg = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    run_exact_and_village_geocoder(limit_arg)
