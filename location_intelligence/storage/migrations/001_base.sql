-- Stage 0: Project Scaffolding & Database
-- Implements: base extension + rera_projects + project_boundaries (design doc §4)

CREATE EXTENSION IF NOT EXISTS postgis;

-- rera_projects: RERA is the sole source of truth for project facts (DATA-01, ARCH-01)
CREATE TABLE IF NOT EXISTS rera_projects (
    id              BIGSERIAL PRIMARY KEY,
    rera_id         TEXT UNIQUE NOT NULL,
    name            TEXT NOT NULL,
    geom            GEOMETRY(Point, 4326) NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active',
    city            TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_rera_projects_geom ON rera_projects USING GIST (geom);
CREATE INDEX IF NOT EXISTS idx_rera_projects_status ON rera_projects (status);

-- project_boundaries: nullable until a validated boundary polygon exists
CREATE TABLE IF NOT EXISTS project_boundaries (
    id              BIGSERIAL PRIMARY KEY,
    project_id      BIGINT NOT NULL REFERENCES rera_projects(id) ON DELETE CASCADE,
    geom            GEOMETRY(Polygon, 4326),
    validated       BOOLEAN NOT NULL DEFAULT false,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (project_id)
);

CREATE INDEX IF NOT EXISTS idx_project_boundaries_geom ON project_boundaries USING GIST (geom);

-- pipeline_runs: observability signal (design doc §10) — logged from Stage 3 onward
CREATE TABLE IF NOT EXISTS pipeline_runs (
    id              BIGSERIAL PRIMARY KEY,
    stage           TEXT NOT NULL,
    status          TEXT NOT NULL,
    row_counts      JSONB,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ,
    error_detail    TEXT
);
