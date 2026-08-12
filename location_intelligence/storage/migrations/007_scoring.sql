-- Stage 6: Scoring & USP Engine
-- Implements: SCORE-01-SCORE-04, per design doc §4 DDL

CREATE TABLE IF NOT EXISTS location_scores (
    id              BIGSERIAL PRIMARY KEY,
    project_id      BIGINT NOT NULL REFERENCES rera_projects(id) ON DELETE CASCADE,
    category        TEXT NOT NULL,        -- one of the 9 taxonomy categories, or 'overall'
    score           NUMERIC(5, 2) NOT NULL,
    model_version   TEXT NOT NULL,        -- SCORE-03: every write is stamped
    computed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (project_id, category, model_version)
);

CREATE INDEX IF NOT EXISTS idx_location_scores_project ON location_scores (project_id, model_version);

CREATE TABLE IF NOT EXISTS project_usps (
    id              BIGSERIAL PRIMARY KEY,
    project_id      BIGINT NOT NULL REFERENCES rera_projects(id) ON DELETE CASCADE,
    poi_id          BIGINT NOT NULL REFERENCES pois(id) ON DELETE CASCADE,
    rank            SMALLINT NOT NULL,
    headline_text   TEXT NOT NULL,
    model_version   TEXT NOT NULL,
    computed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (project_id, poi_id, model_version)
);

CREATE INDEX IF NOT EXISTS idx_project_usps_project ON project_usps (project_id, model_version, rank);
