-- Migration 009: Spatial Hotpath Composite Indexes
-- Ensures high-speed composite index lookups for API endpoints and static map rendering hotpaths.

CREATE INDEX IF NOT EXISTS idx_project_pois_proj_dist 
ON project_pois (project_id, distance_km);

CREATE INDEX IF NOT EXISTS idx_project_usps_proj_rank 
ON project_usps (project_id, model_version, rank);
