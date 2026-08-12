-- Stage 5: Geo Relationship Engine
-- Implements: GEO-01-GEO-04, per design doc §4 DDL

CREATE TABLE IF NOT EXISTS project_pois (
    id              BIGSERIAL PRIMARY KEY,
    project_id      BIGINT NOT NULL REFERENCES rera_projects(id) ON DELETE CASCADE,
    poi_id          BIGINT NOT NULL REFERENCES pois(id) ON DELETE CASCADE,
    distance_km     NUMERIC(6, 3) NOT NULL,
    bearing_deg     NUMERIC(5, 2) NOT NULL,
    direction       TEXT NOT NULL,
    band            TEXT NOT NULL,
    score           NUMERIC(6, 3),          -- filled in by Stage 6 (Scoring Engine)
    method          TEXT NOT NULL DEFAULT 'haversine',
    computed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (project_id, poi_id)
);

CREATE INDEX IF NOT EXISTS idx_project_pois_project_band ON project_pois (project_id, band);
