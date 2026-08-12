-- Stage 2: POI Data Model & Taxonomy
-- Implements: POI-01, POI-02, DB-01 (design doc §4 DDL)

-- poi_staging: raw filtered OSM records, truncated/replaced per ingestion run (ING-03)
CREATE TABLE IF NOT EXISTS poi_staging (
    id              BIGSERIAL PRIMARY KEY,
    osm_type        TEXT NOT NULL,          -- 'node' | 'way' | 'relation'
    osm_id          BIGINT NOT NULL,
    raw_tags        JSONB NOT NULL,
    geom            GEOMETRY(Point, 4326) NOT NULL,
    extract_date    DATE NOT NULL,
    UNIQUE (osm_type, osm_id, extract_date)
);

CREATE INDEX IF NOT EXISTS idx_poi_staging_extract_date ON poi_staging (extract_date);
CREATE INDEX IF NOT EXISTS idx_poi_staging_geom ON poi_staging USING GIST (geom);

-- pois: the normalized master table. Raw OSM tags never reach this table (QUAL-01) —
-- only category/subcategory classified via category_rules.
CREATE TABLE IF NOT EXISTS pois (
    id                  BIGSERIAL PRIMARY KEY,
    name                TEXT,
    category            TEXT NOT NULL,
    subcategory         TEXT,
    geom                GEOMETRY(Point, 4326) NOT NULL,
    confidence          NUMERIC(3, 2) NOT NULL DEFAULT 1.0,   -- QUAL-03
    merged_from_osm_ids BIGINT[] NOT NULL DEFAULT '{}',       -- QUAL-02 lineage
    is_future           BOOLEAN NOT NULL DEFAULT false,       -- "Future" taxonomy category
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_pois_geom ON pois USING GIST (geom);
CREATE INDEX IF NOT EXISTS idx_pois_category ON pois (category);

-- poi_rejects: quarantine, never silently dropped (QUAL-04)
CREATE TABLE IF NOT EXISTS poi_rejects (
    id              BIGSERIAL PRIMARY KEY,
    osm_type        TEXT NOT NULL,
    osm_id          BIGINT NOT NULL,
    reason_code     TEXT NOT NULL,
    raw_tags        JSONB,
    rejected_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- poi_overrides: manual correction/verification; resolver prefers this over ingested data (QUAL-05)
CREATE TABLE IF NOT EXISTS poi_overrides (
    id              BIGSERIAL PRIMARY KEY,
    poi_id          BIGINT NOT NULL REFERENCES pois(id) ON DELETE CASCADE,
    field           TEXT NOT NULL,
    override_value  TEXT NOT NULL,
    verified_by     TEXT NOT NULL,
    verified_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- poi_source_metadata: append-only licence/provenance audit trail (COMP-01-03)
-- Enforced append-only at the application layer in Stage 9; DB-level trigger below
-- gives a defense-in-depth guarantee against hard deletes.
CREATE TABLE IF NOT EXISTS poi_source_metadata (
    id              BIGSERIAL PRIMARY KEY,
    poi_id          BIGINT NOT NULL REFERENCES pois(id) ON DELETE CASCADE,
    provider        TEXT NOT NULL,
    licence         TEXT NOT NULL,
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION prevent_poi_source_metadata_delete()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'poi_source_metadata is append-only: hard delete is not permitted (COMP-03)';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_poi_source_metadata_no_delete ON poi_source_metadata;
CREATE TRIGGER trg_poi_source_metadata_no_delete
    BEFORE DELETE ON poi_source_metadata
    FOR EACH ROW EXECUTE FUNCTION prevent_poi_source_metadata_delete();

-- category_rules: data-driven taxonomy — adding a category is a data change, not a code change (POI-02)
CREATE TABLE IF NOT EXISTS category_rules (
    id              BIGSERIAL PRIMARY KEY,
    osm_tag         TEXT NOT NULL UNIQUE,   -- e.g. 'amenity=hospital'
    category        TEXT NOT NULL,
    subcategory     TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
