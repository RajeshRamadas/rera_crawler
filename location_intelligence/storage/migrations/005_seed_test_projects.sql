-- Synthetic rera_projects for building/testing Stage 5+ before real RERA
-- data is wired in. Swap or delete these later — Stage 5's code doesn't
-- care where rera_projects rows come from.

INSERT INTO rera_projects (rera_id, name, geom, status, city) VALUES
    ('TEST-HSR-001', 'Test Project — HSR Layout',      ST_SetSRID(ST_MakePoint(77.6446, 12.9121), 4326), 'active', 'Bengaluru'),
    ('TEST-WF-001',  'Test Project — Whitefield',       ST_SetSRID(ST_MakePoint(77.7500, 12.9698), 4326), 'active', 'Bengaluru'),
    ('TEST-EC-001',  'Test Project — Electronic City',  ST_SetSRID(ST_MakePoint(77.6701, 12.8452), 4326), 'active', 'Bengaluru'),
    ('TEST-HEB-001', 'Test Project — Hebbal',           ST_SetSRID(ST_MakePoint(77.5970, 13.0355), 4326), 'active', 'Bengaluru'),
    ('TEST-JPN-001', 'Test Project — JP Nagar',         ST_SetSRID(ST_MakePoint(77.5850, 12.9077), 4326), 'active', 'Bengaluru')
ON CONFLICT (rera_id) DO NOTHING;
