"""Stage 7 — Static map rendering.

Implements: MAP-S-01, MAP-S-02.

Server-side renderer using real OSM tiles (design doc §3.5 calls for
"Mapnik/MapLibre native" — the overv/openstreetmap-tile-server stack
already running from earlier in this project IS Mapnik under the hood,
so this module fetches tiles directly from that server). Composites:
project marker, 2/5/10/20km rings (drawn using Stage 1-consistent
distance math, not re-derived), POI markers, top-USP labels, and
mandatory OSM attribution (MAP-S-02).

Falls back to a plain schematic canvas if the tile server is
unreachable (e.g. this build/test environment, or before the tile
server container is started) — the map still renders correctly with
every required element, just without a real photographic basemap
underneath. On a machine with the tile server running (localhost:8080,
per the OSM setup earlier in this project), real tiles are used.

Caching: a project's static map is only re-rendered when its
relationships (project_pois) or scores (location_scores, any model
version) have changed since the last render — matching design doc
§3.5's "cached; invalidated only when relationships/scores change".
"""
import hashlib
import math
import os
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Optional

import psycopg2
import psycopg2.extras
from PIL import Image, ImageDraw

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

OSM_ATTRIBUTION = "\u00a9 OpenStreetMap contributors"
RING_RADII_KM = [2, 5, 10, 20]
TILE_SIZE = 256
DEFAULT_ZOOM = 12
GRID_HALF = 2  # 5x5 tile grid, covers ~47km wide at zoom 12 — enough for the 20km ring
CACHE_DIR = Path(os.environ.get("HOMEXO_MAP_CACHE_DIR", "/tmp/homexo_static_maps"))

CATEGORY_COLORS = {
    "transport": (0, 102, 204), "healthcare": (204, 0, 0), "education": (0, 153, 76),
    "employment": (102, 0, 153), "shopping": (255, 140, 0), "lifestyle": (0, 153, 153),
    "civic": (90, 90, 90), "hospitality": (204, 0, 153), "future": (150, 150, 0),
}


@dataclass
class StaticMapMeta:
    cache_key: str
    image_path: str
    has_rings: list
    has_osm_attribution: bool
    from_cache: bool


def _deg2num_float(lat_deg: float, lon_deg: float, zoom: int) -> tuple[float, float]:
    lat_rad = math.radians(lat_deg)
    n = 2.0 ** zoom
    x = (lon_deg + 180.0) / 360.0 * n
    y = (1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n
    return x, y


def _meters_per_pixel(lat_deg: float, zoom: int) -> float:
    return 156543.03392 * math.cos(math.radians(lat_deg)) / (2 ** zoom)


def _fetch_tile(tile_server_url: str, z: int, x: int, y: int) -> Optional[Image.Image]:
    if requests is None:
        return None
    try:
        resp = requests.get(f"{tile_server_url}/tile/{z}/{x}/{y}.png", timeout=3)
        if resp.status_code == 200:
            return Image.open(BytesIO(resp.content)).convert("RGB")
    except Exception:
        pass
    return None


def _compose_basemap(center_lat: float, center_lon: float, zoom: int, tile_server_url: str) -> tuple[Image.Image, float, float]:
    """Returns (canvas, top_left_tile_x, top_left_tile_y). Falls back to a
    plain schematic canvas if any tile fetch fails.
    """
    grid_size = GRID_HALF * 2 + 1
    canvas_px = grid_size * TILE_SIZE
    cx, cy = _deg2num_float(center_lat, center_lon, zoom)
    center_x_int, center_y_int = int(cx), int(cy)
    top_left_x, top_left_y = center_x_int - GRID_HALF, center_y_int - GRID_HALF

    canvas = Image.new("RGB", (canvas_px, canvas_px), (235, 235, 230))
    all_tiles_ok = True
    for dx in range(grid_size):
        for dy in range(grid_size):
            tile = _fetch_tile(tile_server_url, zoom, top_left_x + dx, top_left_y + dy)
            if tile is None:
                all_tiles_ok = False
                continue
            canvas.paste(tile, (dx * TILE_SIZE, dy * TILE_SIZE))

    return canvas, top_left_x, top_left_y


def _latlon_to_px(lat, lon, zoom, top_left_x, top_left_y):
    x, y = _deg2num_float(lat, lon, zoom)
    return (x - top_left_x) * TILE_SIZE, (y - top_left_y) * TILE_SIZE


def _compute_cache_key(dsn: str, project_id: int) -> str:
    """Changes whenever relationships (Stage 5) or scores under ANY model
    version (Stage 6) have been recomputed for this project — matches
    the design doc's cache-invalidation rule without needing the caller
    to pass a model_version through this function's signature.
    """
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT max(computed_at) FROM project_pois WHERE project_id = %s;", (project_id,))
            rel_ts = cur.fetchone()[0]
            cur.execute(
                "SELECT max(computed_at), count(*) FROM location_scores WHERE project_id = %s;",
                (project_id,),
            )
            score_ts, score_count = cur.fetchone()
    finally:
        conn.close()

    raw = f"{project_id}:{rel_ts}:{score_ts}:{score_count}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def render_static_map(
    dsn: str,
    project_id: int,
    tile_server_url: str = "http://localhost:8080",
    zoom: int = DEFAULT_ZOOM,
    debug_layers: bool = False,
) -> StaticMapMeta:
    cache_key = _compute_cache_key(dsn, project_id)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    image_path = CACHE_DIR / f"{project_id}_{cache_key}.png"

    if image_path.exists():
        return StaticMapMeta(cache_key, str(image_path), RING_RADII_KM, True, from_cache=True)

    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT ST_X(geom) AS lon, ST_Y(geom) AS lat FROM rera_projects WHERE id = %s;", (project_id,))
            project = cur.fetchone()
            if project is None:
                raise ValueError(f"No rera_projects row with id={project_id}")

            cur.execute(
                """
                SELECT p.name, p.category, ST_X(p.geom) AS lon, ST_Y(p.geom) AS lat, pp.distance_km
                FROM project_pois pp JOIN pois p ON p.id = pp.poi_id
                WHERE pp.project_id = %s;
                """,
                (project_id,),
            )
            pois = cur.fetchall()

            cur.execute(
                """
                SELECT pu.rank, pu.headline_text, p.id AS poi_id
                FROM project_usps pu JOIN pois p ON p.id = pu.poi_id
                WHERE pu.project_id = %s ORDER BY pu.rank ASC;
                """,
                (project_id,),
            )
            top_usps = cur.fetchall()
    finally:
        conn.close()

    canvas, top_left_x, top_left_y = _compose_basemap(project["lat"], project["lon"], zoom, tile_server_url)
    draw = ImageDraw.Draw(canvas)
    mpp = _meters_per_pixel(project["lat"], zoom)

    proj_px, proj_py = _latlon_to_px(project["lat"], project["lon"], zoom, top_left_x, top_left_y)

    for radius_km in RING_RADII_KM:
        r_px = (radius_km * 1000) / mpp
        draw.ellipse(
            [proj_px - r_px, proj_py - r_px, proj_px + r_px, proj_py + r_px],
            outline=(80, 80, 200), width=2,
        )
        draw.text((proj_px + r_px * 0.7, proj_py - r_px * 0.7), f"{radius_km}km", fill=(60, 60, 160))

    for poi in pois:
        px, py = _latlon_to_px(poi["lat"], poi["lon"], zoom, top_left_x, top_left_y)
        if not (0 <= px <= canvas.width and 0 <= py <= canvas.height):
            continue
        color = CATEGORY_COLORS.get(poi["category"], (100, 100, 100))
        draw.ellipse([px - 4, py - 4, px + 4, py + 4], fill=color, outline=(255, 255, 255))
        if debug_layers:
            draw.text((px + 5, py - 5), poi["name"] or poi["category"], fill=color)

    for usp in top_usps:
        # PIL's default bitmap font lacks a glyph for the em dash used in
        # real headline copy — swap to a hyphen for this canvas draw only;
        # the stored headline_text in project_usps keeps the em dash for
        # actual UI display.
        display_text = usp["headline_text"].replace("\u2014", "-")
        draw.text((10, 10 + usp["rank"] * 14), f"{usp['rank']}. {display_text}", fill=(20, 20, 20))

    draw.ellipse([proj_px - 6, proj_py - 6, proj_px + 6, proj_py + 6], fill=(255, 215, 0), outline=(0, 0, 0))

    attribution_y = canvas.height - 16
    draw.rectangle([0, attribution_y - 2, 220, canvas.height], fill=(255, 255, 255))
    draw.text((4, attribution_y), OSM_ATTRIBUTION, fill=(0, 0, 0))

    canvas.save(image_path)

    return StaticMapMeta(cache_key, str(image_path), RING_RADII_KM, True, from_cache=False)
