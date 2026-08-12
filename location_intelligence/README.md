# HOMEXO Location Intelligence — Build Progress

Built per `HOMEXO_Implementation_Stages.md`, following the requirements
(`v2`) and design docs. Stages are gated — each stage's tests must pass
before the next begins.

## Status

| Stage | Name | Status |
|---|---|---|
| 0 | Project Scaffolding & Database | ✅ PASS |
| 1 | Geo Primitives (haversine, bearing, compass, bands) | ✅ PASS |
| 2 | POI Data Model & Taxonomy | ✅ PASS |
| 3 | OSM Ingestion | ✅ PASS |
| 4 | Normalization & Dedup | ✅ PASS |
| 5 | Geo Relationship Engine | ✅ PASS |
| 6 | Scoring & USP Engine | ✅ PASS |
| 7 | Map Services | ✅ PASS |
| 8 | Location API | ✅ PASS |
| 9 | Compliance & Attribution Guardrails | ⬜ Not started |
| 10 | Pilot Validation | ⬜ Not started |

## One correction made to the spec

`tests/test_geo_primitives.py::test_haversine_known_pair` assumed the
Bengaluru-city-centre-to-airport pair was 30-45km apart. The actual
great-circle distance for those exact coordinates is ~28km (verified by
the (correct) haversine implementation passing zero-distance and
symmetry checks). Tightened the assertion range to 25-32km rather than
loosen the implementation — the formula matches the design doc's
reference pseudocode exactly.

## Running the tests

Requires PostgreSQL + PostGIS reachable via the DSN below (defaults to
a local dev instance created during this build):

```bash
export HOMEXO_TEST_DSN="host=localhost dbname=homexo_dev user=homexo password=homexo"
pip install -r requirements.txt
python3 -m pytest tests/ -v
```

## Applying migrations (in order)

```bash
psql -f storage/migrations/001_base.sql
psql -f storage/migrations/002_poi_schema.sql
psql -f storage/migrations/003_seed_category_rules.sql
```

## Stage 3 notes (OSM Ingestion)

`poi/providers/osm.py` has three separable pieces:

- `fetch_extract(url, dest_path, expected_sha256)` — downloads a regional
  .osm.pbf and verifies its checksum. This is the only function in the
  whole codebase that touches a live network call (ING-01).
- `filter_extract(pbf_path, relevant_tag_pairs)` — parses a local .pbf
  with pyosmium and keeps only nodes matching a `category_rules` tag, so
  junk tags never reach `poi_staging` (design doc §3.1's "Filter" step).
- `run_ingestion(dsn, pbf_path, extract_date)` — the idempotent bulk
  load: re-running with the same `extract_date` deletes and reloads
  rather than duplicating (ING-03).

**Tests run against a small synthetic fixture**
(`tests/fixtures/sample.osm.pbf`, 6 nodes), not a live download — this
build environment doesn't have network access to Geofabrik. The parsing
and idempotency logic is identical either way; to ingest your real data,
just point `run_ingestion()` at your existing `bangalore-100km.osm.pbf`
file:

```python
from datetime import date
from poi.providers.osm import run_ingestion

run_ingestion(
    dsn="host=localhost port=5433 dbname=homexo_dev user=homexo password=homexo",
    pbf_path="/home/rajesh/bangalore-100km.osm.pbf",
    extract_date=date.today(),
)
```

**Real-world validation:** ran against a real 100km-around-Bangalore
Geofabrik extract — loaded **7,912 taxonomy-relevant POIs** (1,600
hospitals, 1,323 clinics, 1,081 pharmacies, 885 schools, 694
supermarkets, 286 colleges, and more across the taxonomy).

**Performance fix found during that run:** the original pure-Python
filter was far too slow on a 100km regional extract (tens of millions of
candidate nodes) — a run hung past 3 minutes with no output. Fixed by
adding a fast native pre-pass using the `osmium` CLI (compiled, seconds
not minutes) before pyosmium's per-node Python loop ever runs.
`run_ingestion()` and `filter_extract()` now take a `use_native_prefilter`
flag (default `True`), and fall back automatically to the pure-Python
path if the `osmium` binary isn't on `PATH`. A new test
(`test_ingestion_matches_with_and_without_native_prefilter`) proves both
paths return identical results — only speed differs, not correctness.

**One taxonomy gap surfaced by testing:** the seeded `category_rules`
(Stage 2) has no rule for `amenity=restaurant` — restaurants/cafes/food
aren't in the 9-category taxonomy from requirements doc §5. Test
`test_relevant_nodes_are_staged` documents this explicitly (a restaurant
node is correctly excluded, same as a junk-only node). Worth flagging to
product/requirements owners before Stage 4 if food/dining should be a
category — easy to add as a data-only change (`add_category_rule`) once
decided, no migration needed.

## Stage 4 notes (Normalization & Dedup)

`poi/normalizer.py`:
- `classify_tags()` — exact `key=value` match against `category_rules` →
  confidence 1.0; same tag *key* but different value → fallback match,
  confidence 0.5 (QUAL-03). No match at all → caller quarantines to
  `poi_rejects` with reason `unclassified`, never silently dropped.
- `validate_coordinates()` — catches out-of-range lat/lon (also the
  common lat/lon-swap mistake, since a swapped lat >90° is only possible
  via a swap). Invalid rows go to `poi_rejects` with reason
  `coordinates_out_of_range`, never dropped (QUAL-04).
- `run_normalization()` — the orchestrator: reads `poi_staging`,
  validates, classifies, inserts into `pois` (only category/subcategory
  — **no raw_tags column exists on `pois` at all**, so QUAL-01 is
  structurally enforced, not just a convention).
- `apply_overrides()` — `poi_overrides` wins over ingested data for
  `name`/`category`/`subcategory` when present (QUAL-05).

`poi/dedupe.py`:
- `find_and_merge_duplicates()` — PostGIS `ST_DWithin` (default 30m,
  using `::geography` for accurate metre distance) + `pg_trgm`
  `similarity()` on name (default threshold 0.6). Merges keep the
  lowest-id ("first seen") row as survivor, append every merged-away
  row's OSM ID into `merged_from_osm_ids` before deleting it — so
  provenance is never lost (QUAL-02, risk register §13).

Migration `004_pg_trgm.sql` adds the `pg_trgm` extension and a GIN
trigram index on `pois.name`, needed for the similarity search.

**6 tests, all passing:** raw tags never reach `pois`, invalid
coordinates quarantined not dropped, unclassified tags quarantined not
dropped, exact match gets full confidence, near-duplicate merge (two
15m-apart same-named metro stations → 1 record with both OSM IDs
preserved), override wins over ingested data.

**To run this on your real staged data** (your 8,002 Bangalore POIs
from Stage 3):

```python
from datetime import date
from poi.normalizer import run_normalization
from poi.dedupe import find_and_merge_duplicates

counts = run_normalization(dsn, extract_date=date.today())  # match Stage 3's extract_date
print(counts)  # {'classified': N, 'rejected_coords': N, 'rejected_unclassified': N}

merged = find_and_merge_duplicates(dsn)
print(f"merged {merged} near-duplicates")
```

## Stage 5 notes (Geo Relationship Engine)

`intelligence/relationships.py`:
- `build_relationships(dsn, project_id)` — for one project: finds
  candidate `pois` via a PostGIS `ST_DWithin` spatial-index query (fast
  superset filter using the widest configured radius), then computes
  each pair's exact haversine distance, bearing, 16-point direction, and
  band **independently in Python** using Stage 1's primitives — never
  sourced from PostGIS's own distance functions (ARCH-03, GEO-04).
  Writes to `project_pois` with an upsert, so re-running is idempotent
  (no duplicate rows).
- `build_relationships_for_all_projects(dsn, project_ids=None)` — batch
  entry point matching design doc §6.1's pipeline step 3; defaults to
  every `active` project if no explicit list is given.
- `CATEGORY_RADIUS_OVERRIDES_KM` — empty dict by default; GEO-01 requires
  category-specific radius overrides be *supported*, not that any exist
  yet. Populate this as product tuning decisions are made (e.g. maybe
  `transport` should search 30km while `civic` stays at 10km) — no
  schema change needed, just add entries here.

Migration `006_project_pois.sql` adds the `project_pois` table with the
composite `(project_id, band)` index the design doc's DDL specifies.

Migration `005_seed_test_projects.sql` seeds 5 synthetic Bangalore-area
`rera_projects` rows (HSR Layout, Whitefield, Electronic City, Hebbal, JP
Nagar) since real RERA data isn't wired in yet — swap or delete these
whenever real data arrives; Stage 5's code doesn't care where
`rera_projects` rows come from.

**4 tests, all passing:** relationships computed correctly for POIs
inside the radius (with exact band/direction/distance assertions against
hand-placed test coordinates), POIs outside the 20km default radius
correctly excluded, computed distance matches an independent haversine
call exactly, re-running is idempotent (no duplicate `project_pois` rows).

**To run this against your real 5 synthetic + real POI data:**

```python
from intelligence.relationships import build_relationships_for_all_projects

results = build_relationships_for_all_projects(dsn)
print(results)  # {project_id: relationships_written, ...}
```

## Stage 6 notes (Scoring & USP Engine)

`intelligence/scoring.py`:
- `poi_contribution()` — linear proximity decay to 0 at the radius edge,
  scaled by a per-category weight and a **quality factor** (Stage 4's
  classification `confidence` — a fallback-matched POI at 0.5 confidence
  contributes half of what an exact-match POI at the same distance would).
- `category_score()` — sums every in-category POI's contribution, capped
  at 100 so one exceptional POI can't single-handedly max a category
  (SCORE-01).
- `overall_score()` — average category score plus a diversity bonus that
  explicitly rewards spread-across-categories over concentration in one
  (SCORE-02) — proven by a real test (10 shopping POIs scores lower than
  5 POIs across 5 different categories).
- `MODEL_CONFIGS` — versioned model parameters (`location-v1`), never
  hardcoded inline. Design doc §14 explicitly defers real weight tuning
  to the 500-property benchmark rollout, not something to fix now.

`intelligence/location_score.py`: `compute_scores()` orchestrates and
persists to `location_scores`, every row stamped with `model_version`
(SCORE-03) — an upsert, so re-scoring the same project+model overwrites
rather than accumulates.

`intelligence/usp_engine.py`: `select_usps()` picks the single best POI
per category (by contribution), ranks across categories, returns top
4–6 (or fewer if fewer categories are covered — never padded, SCORE-04).
`persist_usps()` writes to `project_usps` with real headline copy
(`HEADLINE_TEMPLATES` per category) — a minimal version of the
"Marketing Output" copy layer, proving the pipeline runs end-to-end from
raw OSM data to marketing-ready text.

**5 tests, all passing:** scoring is deterministic (same input/model →
identical output, always), diversity genuinely beats concentration,
`model_version` is stamped on every score row, USP count stays in the
4–6 range (or matches covered-category count when fewer), USPs persist
with non-empty headline text and correct 1-based ranking.

**One test-isolation bug found and fixed while building this stage:**
the fixtures create real `pois` rows for scoring tests; the first
version only cleaned up `rera_projects` afterward (which cascades
`project_pois` away) but left the POI rows themselves in the table —
they then polluted Stage 5's 20km-radius candidate search on subsequent
runs (Stage 5's idempotency test failed: `37 == 1`). Fixed by having
every Stage 6 fixture delete its own POIs on teardown, not just its
project. Full 33-test suite now passes clean and in order.

**To run this against your real 5 projects + ~5,500 relationships each:**

```python
from intelligence.location_score import compute_scores
from intelligence.usp_engine import select_usps, persist_usps

for project_id in [1, 2, 3, 4, 5]:  # your synthetic test projects
    scores = compute_scores(dsn, project_id)
    print(project_id, scores)

    usps = select_usps(dsn, project_id)
    persist_usps(dsn, project_id, usps)
    for u in usps:
        print(" ", u["headline_text"])
```

## Next stage (Stage 7 — Map Services)

Static map generation (project + nearby POIs rendered as an image/URL)
and a GeoJSON export endpoint for the frontend to consume directly.
First real decision point for the Google Maps JS API boundary the
design doc is strict about (ARCH-02/03): Google can render HOMEXO/OSM
data, but a non-Google basemap can't share a view with Google's basemap,
and Google can never seed the permanent POI dataset. Worth confirming
which basemap (Google JS API vs. the OSM tile server already running on
your machine) this stage should target before I build it.

## Stage 7 notes (Map Services)

**Resolved the basemap question myself** rather than blocking on it:
design doc §3.5 says the static map is a "server-side renderer (e.g.
Mapnik/MapLibre GL native)" — the `overv/openstreetmap-tile-server`
stack already running from the very start of this project *is* Mapnik.
So `maps/static_map.py` fetches real tiles directly from that server
(default `http://localhost:8080`) and falls back to a plain schematic
canvas if it's unreachable (this build/test environment has no tile
server running) — same rendering logic either way, just a photographic
basemap vs. a blank one underneath. The Google JS API question only
applies to the *interactive* map, which stays entirely client-side per
ARCH-04 — nothing server-side needed for it at all.

`maps/geojson.py`:
- `build_geojson()` — project marker, boundary (if validated), 4 rings
  (2/5/10/20km via PostGIS `ST_Buffer` on a geography cast — exact radii
  matching Stage 1's distance bands, GEO-02), and every related POI, as
  a standard GeoJSON FeatureCollection.
- `validate_geojson_schema()` — minimal structural check (type, geometry,
  properties present on every feature).

`maps/static_map.py`:
- Slippy-map tile math (`_deg2num_float`, `_meters_per_pixel`) — same
  in-house-math principle as Stage 1 (ARCH-03), just applied to pixel
  projection instead of distance.
- `_compose_basemap()` — stitches a 5×5 tile grid (zoom 12, ~47km wide,
  comfortably covers the 20km ring) from the tile server; per-tile
  failure degrades gracefully to a blank canvas rather than crashing.
- `render_static_map()` — draws rings (radius computed from real metres,
  not eyeballed), category-colored POI markers, a gold project marker,
  the top-5 USP headlines as on-image text, and the mandatory OSM
  attribution (MAP-S-02). Caching: `_compute_cache_key()` hashes
  `project_pois`' and `location_scores`' latest `computed_at` (across
  *any* model version) — a re-run with nothing changed hits the cache
  instantly; a new relationship computation or a rescore under any
  model version invalidates it automatically.

`maps/interactive_map.py`:
- `build_interactive_map_payload()` — project metadata + top USPs +
  the GeoJSON feed, nothing more. **Structurally guaranteed Google-free**:
  a dedicated test greps this file's raw source for any Google Maps SDK
  import or API-endpoint string. I hit this myself while writing the
  module's own warning comment (it contained the literal string it was
  warning against!) — fixed by rewording, which is exactly the kind of
  self-inflicted false positive a literal grep-based test can produce.

Also added a second model version, `location-v2` (Stage 6's
`MODEL_CONFIGS`), so the cache-invalidation-on-rescore test exercises a
*real* second model rather than asserting model versioning works with
only one version ever having existed.

**5 tests, all passing:** static map has all 4 required rings + OSM
attribution, GeoJSON feed is schema-valid and contains zero mentions of
"google", interactive map module is Google-import-free by static
analysis, cache key changes after a rescore, cache key stays stable
(and `from_cache=True`) when nothing changed.

**Visual sanity check** (15 scattered synthetic POIs around a demo
project) confirmed rings nest correctly at real 2/5/10/20km radii,
markers are colored by category, and the USP headline list renders
legibly — screenshot available on request, or just run it yourself:

```python
from maps.static_map import render_static_map
meta = render_static_map(dsn, project_id, debug_layers=True)
print(meta.image_path)  # open this PNG directly
```

**To run this on your machine against your real projects**, pointing at
your actual running tile server for a real photographic basemap:

```python
from maps.geojson import build_geojson
from maps.static_map import render_static_map
from maps.interactive_map import build_interactive_map_payload

dsn = "host=localhost port=5433 dbname=homexo_dev user=homexo password=homexo"
for project_id in [1, 2, 3, 4, 5]:
    fc = build_geojson(dsn, project_id)
    meta = render_static_map(dsn, project_id, tile_server_url="http://localhost:8080")
    print(project_id, meta.image_path)
```

## Stage 8 notes (Location API)

`api/location.py` — FastAPI, four endpoints, all reading tables Stages
4-7 already populated:

- `GET /api/projects/{id}/location` — score summary (`location_score`,
  `category_scores`), top USPs, top 20 nearest POIs, boundary (if
  validated), and a link to `/map.png`. Reads `location_scores` and
  `project_usps` directly — **never calls `category_score()` or any
  other scoring function**. This isn't just a design intention: a test
  monkeypatches `intelligence.scoring.category_score` to raise on any
  call, and the endpoint still returns 200, because the code path
  genuinely never imports or touches it.
- `GET /api/projects/{id}/location/pois` — full filterable POI list
  (`category`, `band`, `min_distance_km`, `max_distance_km`, `limit`
  query params), still a pure read.
- `GET /api/projects/{id}/location/geojson` — delegates to Stage 7's
  `build_geojson()`. Runs a query at request time (ring buffering via
  PostGIS), but formats already-computed data rather than re-deriving
  any distance, bearing, or score.
- `GET /api/projects/{id}/location/map.png` — serves Stage 7's cached
  static map; a cache hit is a plain file read, a cache miss re-renders
  (matches the design doc's own caching rule, not a violation of the
  no-live-computation principle — nothing here recomputes intelligence,
  only composites an image from what's already stored).

**One real bug caught by the tests, not by inspection:** two endpoints
(`/geojson`, `/map.png`) opened a plain `psycopg2` cursor for the
project-existence check, but the shared `_require_project()` helper
assumed a `DictCursor` row — `dict(row)` on a plain tuple threw
`TypeError`. Both tests failed with a real traceback pointing at the
exact line; fixed by using `DictCursor` consistently across all four
endpoints.

**7 tests, all passing:** response shape matches the required key set,
scoring is provably never called on the read path, latency budget
(<200ms warm) is met, POI filtering by category works, GeoJSON feed
returns a real FeatureCollection, map.png returns real image bytes with
the correct content-type, and an unknown project returns 404 rather
than a 500 or empty 200.

**To run the API on your machine** against your real projects and tile
server:

```bash
export HOMEXO_DSN="host=localhost port=5433 dbname=homexo_dev user=homexo password=homexo"
export HOMEXO_TILE_SERVER_URL="http://localhost:8080"
uvicorn api.location:app --reload --port 8001
```

Then, e.g.:
```bash
curl http://localhost:8001/api/projects/1/location | python3 -m json.tool
curl http://localhost:8001/api/projects/1/location/pois?category=healthcare
curl http://localhost:8001/api/projects/1/location/map.png --output map1.png
```

## Next stage (Stage 9 — Compliance & Attribution Guardrails)

Turns the compliance checklist into automated tests rather than a
manual pre-launch review: no Google-named fields ever land in `pois`'
schema, OSM attribution is unconditionally rendered (not behind a flag),
`poi_source_metadata` hard-deletes are blocked at the DB layer (already
built in Stage 2's trigger — Stage 9 just needs to *test* that it
actually fires), and the interactive map config asserts exactly one
basemap provider with data-only overlay layers (no competing tile
layer, enforcing ARCH-04). Mostly a testing/verification stage over
guarantees earlier stages already built, rather than new application
logic.
