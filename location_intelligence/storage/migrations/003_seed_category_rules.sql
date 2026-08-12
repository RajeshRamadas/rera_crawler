-- Seed category_rules with the taxonomy from requirements doc §5.
-- Covers the 9 required categories: transport, healthcare, education, employment,
-- shopping, lifestyle, civic, hospitality, future.

INSERT INTO category_rules (osm_tag, category, subcategory) VALUES
    -- Transport
    ('railway=station', 'transport', 'railway'),
    ('station=subway', 'transport', 'metro'),
    ('aeroway=aerodrome', 'transport', 'airport'),
    ('amenity=bus_station', 'transport', 'bus_terminal'),
    ('highway=motorway', 'transport', 'expressway'),
    ('highway=trunk', 'transport', 'highway'),

    -- Healthcare
    ('amenity=hospital', 'healthcare', 'multispeciality_hospital'),
    ('amenity=clinic', 'healthcare', 'clinic'),
    ('amenity=doctors', 'healthcare', 'clinic'),
    ('amenity=pharmacy', 'healthcare', 'diagnostics'),
    ('healthcare=hospital', 'healthcare', 'multispeciality_hospital'),

    -- Education
    ('amenity=school', 'education', 'school'),
    ('amenity=university', 'education', 'university'),
    ('amenity=college', 'education', 'college'),

    -- Employment
    ('landuse=industrial', 'employment', 'industrial_area'),
    ('office=it', 'employment', 'it_park'),

    -- Shopping
    ('shop=mall', 'shopping', 'mall'),
    ('shop=supermarket', 'shopping', 'supermarket'),
    ('shop=department_store', 'shopping', 'hypermarket'),

    -- Lifestyle
    ('leisure=park', 'lifestyle', 'park'),
    ('natural=water', 'lifestyle', 'lake'),
    ('leisure=sports_centre', 'lifestyle', 'sports_complex'),
    ('leisure=golf_course', 'lifestyle', 'golf_course'),
    ('amenity=cinema', 'lifestyle', 'cinema'),

    -- Civic
    ('office=government', 'civic', 'government_office'),
    ('amenity=police', 'civic', 'police'),
    ('amenity=fire_station', 'civic', 'fire'),
    ('amenity=courthouse', 'civic', 'court'),

    -- Hospitality
    ('tourism=hotel', 'hospitality', 'hotel'),

    -- Future (proposed / under-construction infrastructure)
    ('railway=construction', 'future', 'metro_under_construction'),
    ('highway=construction', 'future', 'highway_under_construction')
ON CONFLICT (osm_tag) DO NOTHING;
