# HOMEXO Location Intelligence Engine

A location-intelligence pipeline for HOMEXO's real estate platform.
For every listed project, it finds nearby points of interest, scores
the surrounding infrastructure, and generates marketing-ready selling
points ("Prime Connectivity", "Excellent Schooling Options") — entirely
from open geographic data, computed and owned in-house.

## What it does

1. **Ingests** OpenStreetMap data for a region and filters it down to a
   defined points-of-interest taxonomy (transport, healthcare,
   education, employment, shopping, lifestyle, civic, hospitality,
   future infrastructure).
2. **Normalizes and deduplicates** raw OSM records into a clean POI
   catalog, with confidence scoring and manual-override support.
3. **Computes relationships** between every RERA project and nearby
   POIs — exact distance, bearing, compass direction, and distance band
   — using geo math implemented in-house, never sourced from a
   third-party API.
4. **Scores** each project's location quality per category and overall,
   with a diversity bonus that rewards well-rounded infrastructure over
   one dominant amenity.
5. **Selects and writes marketing copy** for the top 4–6 unique selling
   points per project.
6. **Renders maps** — a cached static image (real OSM basemap, distance
   rings, POI markers, USP callouts) and a GeoJSON feed for an
   interactive client-side map.
7. **Serves it all** through a read-only REST API with a hard
   architectural guarantee: no scoring or geo computation ever runs on
   the request path — only precomputed data is read.
8. **Enforces compliance automatically** — data provenance is
   append-only at the database layer, OSM attribution is
   unconditionally rendered, and the interactive map can never mix in a
   second basemap provider.

## Architectural principles

These are enforced by tests, not just documentation:

- **RERA is the source of truth** for project facts and coordinates.
  **OpenStreetMap is the only persistent source of POIs.** Google Maps
  is permitted solely as a client-side rendering layer for the
  interactive map — it can never seed the permanent dataset, and a
  non-Google basemap can never share a view with it.
- **All derived intelligence — distance, bearing, scores, USPs — is
  computed by this codebase**, never delegated to or sourced from a
  third-party API.
- **Nothing is silently dropped.** Invalid coordinates and
  unclassifiable tags are quarantined to a rejects table, not deleted.
- **Provenance is append-only**, enforced at the database layer against
  both `DELETE` and `TRUNCATE`.
- **The API is a pure read layer.** A dedicated test proves the
  `/location` endpoint returns correctly even if the scoring engine is
  made to throw on any call — the endpoint genuinely never reaches it.

## Project status

All stages are gated: each stage's automated tests must pass in full,
alongside every prior stage's tests, before the next stage begins.

| Stage | Name | Status |
|---|---|---|
| 0 | Project Scaffolding & Database | ✅ Complete |
| 1 | Geo Primitives | ✅ Complete |
| 2 | POI Data Model & Taxonomy | ✅ Complete |
| 3 | OSM Ingestion | ✅ Complete |
| 4 | Normalization & Deduplication | ✅ Complete |
| 5 | Geo Relationship Engine | ✅ Complete |
| 6 | Scoring & USP Engine | ✅ Complete |
| 7 | Map Services | ✅ Complete |
| 8 | Location API | ✅ Complete |
| 9 | Compliance & Attribution Guardrails | ✅ Complete |
| 10 | Pilot Validation (100 real projects) | ⬜ Pending real RERA data source |

**50/50 automated tests passing** across Stages 0–9, verified against
both synthetic fixtures and a real ~100km-radius Bangalore OpenStreetMap
extract (7,944 deduplicated real-world POIs).

See [docs/STAGE_NOTES.md](docs/STAGE_NOTES.md) for full per-stage
implementation detail and requirement-ID traceability, and
[CHANGELOG.md](CHANGELOG.md) for real bugs found and fixed during the
build — including a genuine compliance gap (a `TRUNCATE` bypass on the
audit-trail table) caught and closed before it ever mattered.

## Project layout

```
geo/            Pure geo math — haversine distance, bearing, compass direction, distance bands
poi/            POI taxonomy, classification, and OSM ingestion (poi/providers/osm.py)
intelligence/   Geo relationship engine, scoring, and USP selection
maps/           GeoJSON export, static map rendering, interactive map payload assembly
api/            Read-only FastAPI location API
compliance/     Automated compliance and attribution guardrails
storage/        Database migrations (run in numeric order)
tests/          One test file per stage, plus fixtures
docs/           Detailed engineering notes
```

## Setup

Requires PostgreSQL with PostGIS, and Python 3.12+.

```bash
# 1. Install dependencies
pip install -r requirements.txt --break-system-packages

# 2. Apply migrations, in order
for f in storage/migrations/*.sql; do
  PGPASSWORD=<password> psql -h <host> -p <port> -U <user> -d <db> -f "$f"
done

# 3. Point tests and the API at your database
export HOMEXO_TEST_DSN="host=<host> port=<port> dbname=<db> user=<user> password=<password>"
export HOMEXO_DSN="$HOMEXO_TEST_DSN"
export HOMEXO_TILE_SERVER_URL="http://localhost:8080"   # your local OSM tile server, if running

# 4. Run the full test suite
python3 -m pytest tests/ -v
```

## Running the API

```bash
uvicorn api.location:app --reload --port 8001
```

```bash
curl http://localhost:8001/api/projects/1/location
curl "http://localhost:8001/api/projects/1/location/pois?category=healthcare"
curl http://localhost:8001/api/projects/1/location/geojson
curl http://localhost:8001/api/projects/1/location/map.png --output map.png
```

## Ingesting real OpenStreetMap data

```python
from datetime import date
from poi.providers.osm import run_ingestion
from poi.normalizer import run_normalization
from poi.dedupe import find_and_merge_duplicates
from intelligence.relationships import build_relationships_for_all_projects
from intelligence.location_score import compute_scores
from intelligence.usp_engine import select_usps, persist_usps

dsn = "host=<host> port=<port> dbname=<db> user=<user> password=<password>"

# 1. Ingest a regional .osm.pbf extract (auto-filters to the POI taxonomy)
run_ingestion(dsn, "/path/to/region.osm.pbf", extract_date=date.today())

# 2. Normalize, validate, and deduplicate
run_normalization(dsn, extract_date=date.today())
find_and_merge_duplicates(dsn)

# 3. Compute relationships and scores for every active project
build_relationships_for_all_projects(dsn)
for project_id in [...]:
    compute_scores(dsn, project_id)
    usps = select_usps(dsn, project_id)
    persist_usps(dsn, project_id, usps)
```

## License and attribution

POI data is sourced from [OpenStreetMap](https://www.openstreetmap.org/copyright),
© OpenStreetMap contributors, available under the Open Database License
(ODbL). Attribution is rendered unconditionally wherever OSM-derived
data is displayed, enforced by an automated compliance test
(Stage 9).
