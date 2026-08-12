-- Stage 4: enables trigram name-similarity matching for dedup (QUAL-02)
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE INDEX IF NOT EXISTS idx_pois_name_trgm ON pois USING GIN (name gin_trgm_ops);
