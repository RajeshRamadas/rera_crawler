import psycopg2
import json

DB_URI = "postgres://rajesh:password@localhost:5432/rera_db"  # Or whatever it is

try:
    conn = psycopg2.connect("dbname=rera")
    cur = conn.cursor()
    cur.execute("SELECT raw_data FROM rera_projects WHERE registration_no = 'PRM/KA/RERA/1251/308/PR/161122/005459'")
    row = cur.fetchone()
    if row:
        raw = row[0]
        with open("test_out.json", "w") as f:
            json.dump(raw, f, indent=2)
        print(f"Total uploaded_documents: {len(raw.get('uploaded_documents', []))}")
except Exception as e:
    print(f"Error: {e}")
