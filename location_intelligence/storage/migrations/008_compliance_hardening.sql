-- Stage 9: close a real gap found while testing COMP-03's append-only
-- guarantee. The Stage 2 trigger (prevent_poi_source_metadata_delete)
-- only fires on row-level DELETE — but PostgreSQL's TRUNCATE does NOT
-- invoke FOR EACH ROW triggers at all, so anyone with TRUNCATE privilege
-- could previously wipe the entire audit trail undetected. This adds a
-- statement-level trigger closing that hole, reusing the same function.

DROP TRIGGER IF EXISTS trg_poi_source_metadata_no_truncate ON poi_source_metadata;
CREATE TRIGGER trg_poi_source_metadata_no_truncate
    BEFORE TRUNCATE ON poi_source_metadata
    FOR EACH STATEMENT EXECUTE FUNCTION prevent_poi_source_metadata_delete();
