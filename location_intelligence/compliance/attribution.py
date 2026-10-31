"""Stage 9 — Property page rendering (for attribution compliance testing).

Implements: COMP-01, COMP-02.

Deliberately thin: reuses Stage 8's get_location() directly (it's a
plain callable — the @app.get decorator doesn't remove that) rather
than re-querying the database, so this exercises the real API code
path instead of a parallel mock. OSM attribution comes from Stage 7's
single OSM_ATTRIBUTION constant and is always appended — there is no
parameter, flag, or condition anywhere in this function that could
omit it. That's what test_osm_attribution_always_rendered checks.
"""
from api.location import get_location
from maps.static_map import OSM_ATTRIBUTION


def render_property_page(dsn: str, project_id: int, model_version: str = "location-v1") -> str:
    """A minimal HTML property page. Real frontend rendering happens
    client-side in production — this exists so attribution compliance
    can be tested against actual rendered output, not asserted in the
    abstract.
    """
    data = get_location(project_id, model_version=model_version)

    usp_html = "".join(f"<li>{usp['headline_text']}</li>" for usp in data["top_usps"])

    return f"""<!DOCTYPE html>
<html>
<head><title>Project {data['project_id']}</title></head>
<body>
  <h1>Project {data['project_id']}</h1>
  <p>Location Score: {data['location_score']}</p>
  <ul>{usp_html}</ul>
  <footer>{OSM_ATTRIBUTION}</footer>
</body>
</html>"""
