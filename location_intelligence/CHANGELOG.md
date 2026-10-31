# Changelog

Notable fixes and hardening found through testing during the build,
in the order they were discovered. See [docs/STAGE_NOTES.md](docs/STAGE_NOTES.md)
for full stage-by-stage implementation detail.

## Stage 9 — Compliance & Attribution Guardrails

**Fixed:** `poi_source_metadata`'s append-only guarantee did not cover
`TRUNCATE`. The original Stage 2 trigger only fires on row-level
`DELETE`; PostgreSQL does not invoke `FOR EACH ROW` triggers for
`TRUNCATE`, so a user with `TRUNCATE` privilege could have wiped the
entire compliance/provenance audit trail with no trace. Closed with a
statement-level `BEFORE TRUNCATE` trigger
(`storage/migrations/008_compliance_hardening.sql`), verified by
`test_source_metadata_truncate_is_also_blocked`.

**Recommendation:** in production, also revoke `DELETE`/`TRUNCATE` on
`poi_source_metadata` from the application's runtime database role, so
the guarantee is enforced by both a privilege boundary and a trigger.

## Stage 8 — Location API

**Fixed:** the `/location/geojson` and `/location/map.png` endpoints
opened a plain `psycopg2` cursor for their project-existence check, but
the shared `_require_project()` helper assumed a `DictCursor` row —
`dict(row)` on a plain tuple raised `TypeError`. Caught by
`test_geojson_endpoint_returns_feature_collection` and
`test_map_png_endpoint_returns_image`. Fixed by using `DictCursor`
consistently across all four endpoints.

## Stage 7 — Map Services

**Fixed:** the interactive-map module's own source-level warning
comment ("don't import googlemaps...") contained the literal substring
its accompanying compliance test greps for, producing a false positive.
Reworded without weakening the guarantee it documents.

**Fixed (cosmetic):** PIL's default bitmap font has no glyph for the
em dash (`—`) used in real marketing headline copy, rendering as a
garbled box on static map images. Sanitized only at canvas-draw time;
the stored `headline_text` in `project_usps` keeps the em dash for
actual UI display.

## Stage 6 — Scoring & USP Engine

**Fixed:** the first version of the scoring test fixtures created real
`pois` rows but only cleaned up the parent `rera_projects` row on
teardown. The leftover POIs then polluted Stage 5's 20km-radius
candidate search on later runs, breaking its idempotency assertion
(37 rows instead of 1). Fixed by having every Stage 6 fixture delete
its own POIs on teardown, not just its project — confirmed by running
the full suite together, not just the stage under test.

## Stage 3 — OSM Ingestion

**Fixed (performance):** the original pure-Python tag filter iterated
every node in a regional `.osm.pbf` extract in-process. On a real
100km-radius Bangalore extract (tens of millions of candidate nodes),
this hung past 3 minutes with no output. Added a fast native pre-pass
using the `osmium` CLI (compiled, seconds not minutes) before the
per-node Python loop runs; falls back automatically to the pure-Python
path if the `osmium` binary isn't on `PATH`. A dedicated test
(`test_ingestion_matches_with_and_without_native_prefilter`) proves
both paths return identical results — only speed differs, not
correctness. Real-world result: ~8,000 taxonomy-relevant POIs loaded
from a full raw 100km extract in well under a minute.

## Stage 1 — Geo Primitives

**Fixed (spec correction):** a test fixture assumed the distance
between Bengaluru's city centre and its airport was 30-45km; the actual
great-circle distance for those exact coordinates is ~28km. Corrected
the test's expected range rather than the implementation, since the
haversine formula independently passed its zero-distance and symmetry
checks and matches the design doc's reference pseudocode exactly.
