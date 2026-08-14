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
DEFAULT_ZOOM = 14
GRID_HALF = 4  # 9×9 tile grid at zoom 14 — ~18km wide, project pin centered with room for all rings
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
            cur.execute("SELECT name, ST_X(geom) AS lon, ST_Y(geom) AS lat FROM rera_projects WHERE id = %s;", (project_id,))
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
    mpp = _meters_per_pixel(project["lat"], zoom)
    proj_px, proj_py = _latlon_to_px(project["lat"], project["lon"], zoom, top_left_x, top_left_y)

    # ── Layer 1: Semi-transparent ring fills ─────────────────────────────────
    ring_overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ring_draw = ImageDraw.Draw(ring_overlay)
    ring_fills  = [(60, 100, 220, 18), (80, 120, 210, 12), (100, 140, 200, 8), (120, 160, 190, 5)]
    ring_stroke = [(60, 100, 220, 200), (80, 120, 210, 180), (100, 140, 200, 160), (120, 160, 190, 140)]
    for i, radius_km in enumerate(reversed(RING_RADII_KM)):
        r_px = (radius_km * 1000) / mpp
        ring_draw.ellipse(
            [proj_px - r_px, proj_py - r_px, proj_px + r_px, proj_py + r_px],
            fill=ring_fills[i], outline=ring_stroke[i], width=2,
        )
    canvas = Image.alpha_composite(canvas.convert("RGBA"), ring_overlay).convert("RGB")
    draw = ImageDraw.Draw(canvas)

    # ── Layer 2: Ring distance labels ────────────────────────────────────────
    for radius_km in RING_RADII_KM:
        r_px = (radius_km * 1000) / mpp
        lx = proj_px + r_px * 0.68
        ly = proj_py - r_px * 0.68
        tb = draw.textbbox((lx, ly), f"{radius_km}km")
        draw.rectangle([tb[0]-3, tb[1]-2, tb[2]+3, tb[3]+2], fill=(255, 255, 255), outline=(80, 80, 200))
        draw.text((lx, ly), f"{radius_km}km", fill=(30, 30, 160))

    # ── Layer 3: POI markers + leader-line label chips ────────────────────────
    import math as _math
    sorted_pois = sorted(pois, key=lambda x: x["distance_km"])
    LEADER = 60
    CATEGORY_ICONS = {
        "transport": "T", "healthcare": "H", "education": "E",
        "employment": "W", "shopping": "S", "lifestyle": "L",
        "civic": "G", "hospitality": "Ho", "future": "F",
    }
    for poi in sorted_pois:
        px, py = _latlon_to_px(poi["lat"], poi["lon"], zoom, top_left_x, top_left_y)
        if not (0 <= px <= canvas.width and 0 <= py <= canvas.height):
            continue
        color = CATEGORY_COLORS.get(poi["category"], (100, 100, 100))
        # Outer white halo + colored dot + icon letter
        draw.ellipse([px-9, py-9, px+9, py+9], fill=(255, 255, 255))
        draw.ellipse([px-7, py-7, px+7, py+7], fill=color)
        icon = CATEGORY_ICONS.get(poi["category"], "?")
        ib = draw.textbbox((0, 0), icon)
        draw.text((px - (ib[2]-ib[0])//2, py - (ib[3]-ib[1])//2 - 1), icon, fill=(255, 255, 255))
        # Leader line
        dx = px - proj_px; dy = py - proj_py
        angle = _math.atan2(dy, dx)
        lx = int(px + _math.cos(angle) * LEADER)
        ly = int(py + _math.sin(angle) * LEADER)
        draw.line([(px, py), (lx, ly)], fill=color, width=2)
        # Label chip: white bg + colored left accent bar + "Name  X.Xkm"
        dist_f = float(poi["distance_km"])
        label_text = f"{poi['name']}  {dist_f:.1f}km"
        tb = draw.textbbox((0, 0), label_text)
        lw, lh = tb[2]-tb[0], tb[3]-tb[1]
        ax = (lx + 6) if _math.cos(angle) >= 0 else (lx - lw - 10)
        ay = ly - lh // 2
        ax = max(4, min(canvas.width - lw - 14, ax))
        ay = max(4, min(canvas.height - lh - 8, ay))
        draw.rectangle([ax-4, ay-3, ax+lw+6, ay+lh+3], fill=(255, 255, 255), outline=(200, 200, 200))
        draw.rectangle([ax-4, ay-3, ax-1, ay+lh+3], fill=color)   # colored accent bar
        draw.text((ax+2, ay), label_text, fill=(15, 15, 15))

    # ── Layer 4: Dark USP card (top-left, drawn directly) ────────────────────
    if top_usps:
        CARD_BG   = (22, 24, 34)
        CARD_GOLD = (255, 200, 30)
        card_w  = 490
        card_h  = 30 + len(top_usps) * 22 + 10
        cx1, cy1 = 14, 14
        cx2, cy2 = cx1 + card_w, cy1 + card_h
        draw.rectangle([cx1, cy1, cx2, cy2], fill=CARD_BG)
        draw.rectangle([cx1, cy1, cx2, cy1 + 4], fill=CARD_GOLD)   # gold top accent
        draw.text((cx1 + 10, cy1 + 8), "TOP LOCATION HIGHLIGHTS", fill=CARD_GOLD)
        for usp in top_usps:
            row_y = cy1 + 32 + (usp["rank"] - 1) * 22
            cat = next((p["category"] for p in pois if p["name"] and p["name"] in usp["headline_text"]), None)
            dot_color = CATEGORY_COLORS.get(cat, (180, 180, 180)) if cat else (180, 180, 180)
            draw.ellipse([cx1+10, row_y+5, cx1+17, row_y+12], fill=dot_color)
            display_text = usp["headline_text"].replace("\u2014", "-")
            if len(display_text) > 64:
                display_text = display_text[:61] + "..."
            draw.text((cx1 + 23, row_y + 1), display_text, fill=(235, 235, 235))

    # ── Layer 5: Central project pin ─────────────────────────────────────────
    # Drop shadow
    draw.ellipse([proj_px-11, proj_py-9, proj_px+11, proj_py+13], fill=(30, 30, 30))
    # White outer ring
    draw.ellipse([proj_px-11, proj_py-11, proj_px+11, proj_py+11], fill=(255, 255, 255))
    # Gold fill
    draw.ellipse([proj_px-9, proj_py-9, proj_px+9, proj_py+9], fill=(255, 210, 0), outline=(170, 120, 0), width=2)
    # Dark inner center dot
    draw.ellipse([proj_px-3, proj_py-3, proj_px+3, proj_py+3], fill=(120, 70, 0))
    # Name badge above pin
    proj_name = project["name"]
    nb = draw.textbbox((0, 0), proj_name)
    nw, nh = nb[2]-nb[0], nb[3]-nb[1]
    bx = max(6, min(canvas.width - nw - 18, proj_px - nw//2))
    by = proj_py - nh - 26
    draw.rectangle([bx-8, by-5, bx+nw+8, by+nh+5], fill=(255, 210, 0), outline=(150, 100, 0), width=2)
    draw.text((bx, by), proj_name, fill=(25, 15, 0))
    draw.line([(proj_px, by+nh+5), (proj_px, proj_py-11)], fill=(150, 100, 0), width=2)

    # ── Layer 6: OSM attribution ──────────────────────────────────────────────
    attr_y = canvas.height - 18
    attr_tb = draw.textbbox((6, attr_y), OSM_ATTRIBUTION)
    draw.rectangle([0, attr_y-3, attr_tb[2]+10, canvas.height], fill=(255, 255, 255))
    draw.text((6, attr_y), OSM_ATTRIBUTION, fill=(80, 80, 80))

    canvas.save(image_path)
    return StaticMapMeta(cache_key, str(image_path), RING_RADII_KM, True, from_cache=False)

