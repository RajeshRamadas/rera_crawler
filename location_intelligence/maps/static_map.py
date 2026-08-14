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
from PIL import Image, ImageDraw, ImageFont

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

from concurrent.futures import ThreadPoolExecutor
from config import settings

OSM_ATTRIBUTION = "\u00a9 OpenStreetMap contributors"
RING_RADII_KM = [2, 5, 10, 20]
TILE_SIZE = 256
DEFAULT_ZOOM = 14
GRID_HALF = 4  # 9x9 tile grid at zoom 14 — ~18km wide, project pin centered with room for all rings
CACHE_DIR = settings.map_cache_dir


CATEGORY_COLORS = {
    "transport": (0, 102, 204), "healthcare": (204, 0, 0), "education": (0, 153, 76),
    "employment": (102, 0, 153), "shopping": (255, 140, 0), "lifestyle": (0, 153, 153),
    "civic": (90, 90, 90), "hospitality": (204, 0, 153), "future": (150, 150, 0),
}

CATEGORY_ICONS = {
    "transport": "T", "healthcare": "H", "education": "E",
    "employment": "W", "shopping": "S", "lifestyle": "L",
    "civic": "G", "hospitality": "Ho", "future": "F",
}

# ── Premium Typography ─────────────────────────────────────────────────────────
_BUNDLED_FONTS_DIR = Path(__file__).parent.parent / "assets" / "fonts"
_SYSTEM_FONTS_DIR  = Path("/usr/share/fonts/truetype/noto")

def _load_font(weight: str, size: int) -> ImageFont.FreeTypeFont:
    """Load NotoSans with fallback to bundled fonts, system fonts, or PIL default."""
    candidates = [
        _BUNDLED_FONTS_DIR / f"NotoSans-{weight}.ttf",
        _BUNDLED_FONTS_DIR / "NotoSans-Regular.ttf",
        _SYSTEM_FONTS_DIR / f"NotoSans-{weight}.ttf",
        _SYSTEM_FONTS_DIR / "NotoSans-Regular.ttf",
    ]
    for path in candidates:
        if path.exists():
            try:
                return ImageFont.truetype(str(path), size)
            except Exception:
                pass
    return ImageFont.load_default()


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
    """Returns (canvas, top_left_tile_x, top_left_tile_y). Fetches 81 tiles
    concurrently via ThreadPoolExecutor. Falls back to a plain schematic canvas
    for missing tiles.
    """
    grid_size = GRID_HALF * 2 + 1
    canvas_px = grid_size * TILE_SIZE
    cx, cy = _deg2num_float(center_lat, center_lon, zoom)
    center_x_int, center_y_int = int(cx), int(cy)
    top_left_x, top_left_y = center_x_int - GRID_HALF, center_y_int - GRID_HALF

    canvas = Image.new("RGB", (canvas_px, canvas_px), (235, 235, 230))
    tile_coords = [
        (dx, dy, top_left_x + dx, top_left_y + dy)
        for dx in range(grid_size)
        for dy in range(grid_size)
    ]

    def _fetch_one(item):
        dx, dy, tx, ty = item
        return dx, dy, _fetch_tile(tile_server_url, zoom, tx, ty)

    with ThreadPoolExecutor(max_workers=16) as executor:
        results = list(executor.map(_fetch_one, tile_coords))

    for dx, dy, tile in results:
        if tile is not None:
            canvas.paste(tile, (dx * TILE_SIZE, dy * TILE_SIZE))

    return canvas, top_left_x, top_left_y


def _latlon_to_px(lat, lon, zoom, top_left_x, top_left_y):
    x, y = _deg2num_float(lat, lon, zoom)
    return (x - top_left_x) * TILE_SIZE, (y - top_left_y) * TILE_SIZE


def _compute_cache_key(dsn_or_conn, project_id: int) -> str:
    """Changes whenever relationships (Stage 5) or scores under ANY model
    version (Stage 6) have been recomputed for this project — computed via
    a single query.
    """
    if isinstance(dsn_or_conn, str):
        conn = psycopg2.connect(dsn_or_conn)
        should_close = True
    else:
        conn = dsn_or_conn
        should_close = False

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 
                    (SELECT max(computed_at) FROM project_pois WHERE project_id = %s) AS rel_ts,
                    (SELECT max(computed_at) FROM location_scores WHERE project_id = %s) AS score_ts,
                    (SELECT count(*) FROM location_scores WHERE project_id = %s) AS score_count;
                """,
                (project_id, project_id, project_id),
            )
            rel_ts, score_ts, score_count = cur.fetchone()
    finally:
        if should_close:
            conn.close()

    raw = f"{project_id}:{rel_ts}:{score_ts}:{score_count}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def render_static_map(
    dsn: str,
    project_id: int,
    tile_server_url: str = settings.tile_server_url,
    zoom: int = DEFAULT_ZOOM,
    debug_layers: bool = False,
) -> StaticMapMeta:
    conn = psycopg2.connect(dsn)
    try:
        cache_key = _compute_cache_key(conn, project_id)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        image_path = CACHE_DIR / f"{project_id}_{cache_key}.png"

        if image_path.exists():
            return StaticMapMeta(cache_key, str(image_path), RING_RADII_KM, True, from_cache=True)

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

    # ── Fonts ─────────────────────────────────────────────────────────────────
    font_xs   = _load_font("Regular",  11)
    font_sm   = _load_font("Regular",  13)
    font_md   = _load_font("SemiBold", 14)
    font_lg   = _load_font("Bold",     16)
    font_icon = _load_font("Bold",     11)
    font_usp_hdr = _load_font("Bold",  13)
    font_usp_row = _load_font("Regular", 13)

    # ── Collision registry (list of [x1,y1,x2,y2] placed boxes) ─────────────
    placed_boxes: list[tuple[int,int,int,int]] = []

    def _no_overlap(x1, y1, x2, y2, pad=4) -> bool:
        for bx1,by1,bx2,by2 in placed_boxes:
            if x1-pad < bx2 and x2+pad > bx1 and y1-pad < by2 and y2+pad > by1:
                return False
        return True

    def _try_place(candidates):
        """Return first (x,y) candidate whose bounding box doesn't overlap."""
        for cx, cy, w, h in candidates:
            if _no_overlap(cx, cy, cx+w, cy+h):
                placed_boxes.append((cx, cy, cx+w, cy+h))
                return cx, cy, w, h
        # Force-place last candidate (fallback)
        cx, cy, w, h = candidates[-1]
        placed_boxes.append((cx, cy, cx+w, cy+h))
        return cx, cy, w, h

    # ── Layer 1: Semi-transparent ring fills ─────────────────────────────────
    ring_overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ring_draw = ImageDraw.Draw(ring_overlay)
    ring_fills  = [(60, 100, 220, 15), (80, 120, 210, 10), (100, 140, 200, 6), (120, 160, 190, 4)]
    ring_stroke = [(60, 100, 220, 200), (80, 120, 210, 170), (100, 140, 200, 140), (120, 160, 190, 120)]
    for i, radius_km in enumerate(reversed(RING_RADII_KM)):
        r_px = (radius_km * 1000) / mpp
        ring_draw.ellipse(
            [proj_px - r_px, proj_py - r_px, proj_px + r_px, proj_py + r_px],
            fill=ring_fills[i], outline=ring_stroke[i], width=2,
        )
    canvas = Image.alpha_composite(canvas.convert("RGBA"), ring_overlay).convert("RGB")
    draw = ImageDraw.Draw(canvas)

    # ── Layer 2: Ring distance labels (NotoSans Regular 11) ──────────────────
    for radius_km in RING_RADII_KM:
        r_px = (radius_km * 1000) / mpp
        lx = int(proj_px + r_px * 0.70)
        ly = int(proj_py - r_px * 0.70)
        label = f"{radius_km}km"
        tb = draw.textbbox((lx, ly), label, font=font_xs)
        # soft white pill
        draw.rounded_rectangle([tb[0]-5, tb[1]-3, tb[2]+5, tb[3]+3], radius=4,
                                fill=(255,255,255,230), outline=(100,120,210))
        draw.text((lx, ly), label, font=font_xs, fill=(40, 40, 180))

    # ── Layer 3: POI markers + collision-aware label chips ───────────────────
    sorted_pois = sorted(pois, key=lambda x: x["distance_km"])
    LEADER = 70       # leader line length in px
    DOT_R  = 10       # dot radius

    for poi in sorted_pois:
        px, py = _latlon_to_px(poi["lat"], poi["lon"], zoom, top_left_x, top_left_y)
        if not (DOT_R < px < canvas.width - DOT_R and DOT_R < py < canvas.height - DOT_R):
            continue
        color = CATEGORY_COLORS.get(poi["category"], (100, 100, 100))

        # Drop shadow
        draw.ellipse([px-DOT_R, py-DOT_R+2, px+DOT_R, py+DOT_R+2], fill=(0,0,0,60))
        # White halo
        draw.ellipse([px-DOT_R, py-DOT_R, px+DOT_R, py+DOT_R], fill=(255,255,255))
        # Colored circle
        draw.ellipse([px-DOT_R+2, py-DOT_R+2, px+DOT_R-2, py+DOT_R-2], fill=color)
        # Category icon letter (white, bold 11pt)
        icon = CATEGORY_ICONS.get(poi["category"], "?")
        ib = draw.textbbox((0,0), icon, font=font_icon)
        iw, ih = ib[2]-ib[0], ib[3]-ib[1]
        draw.text((px-iw//2, py-ih//2-1), icon, font=font_icon, fill=(255,255,255))

        # Leader line pointing away from project center
        dx = px - proj_px; dy = py - proj_py
        angle = math.atan2(dy, dx)
        lx = int(px + math.cos(angle) * LEADER)
        ly = int(py + math.sin(angle) * LEADER)
        draw.line([(px, py), (lx, ly)], fill=color, width=2)

        # Label chip: "Name  1.4km" in NotoSans SemiBold 14pt
        dist_f = float(poi["distance_km"])
        name_part = poi["name"] or poi["category"].title()
        dist_part = f"  {dist_f:.1f}km"
        label_text = name_part + dist_part
        tb = draw.textbbox((0, 0), label_text, font=font_md)
        lw, lh = tb[2]-tb[0], tb[3]-tb[1]
        PAD_X, PAD_Y = 8, 5

        # Generate 8 candidate positions clockwise from leader tip
        tip_right = lx + 6
        tip_left  = lx - lw - PAD_X*2 - 4
        tip_above = ly - lh - PAD_Y*2 - 4
        tip_below = ly + 4
        tip_vctr  = ly - (lh + PAD_Y*2)//2

        candidates = [
            (tip_right, tip_vctr,  lw + PAD_X*2, lh + PAD_Y*2),
            (tip_left,  tip_vctr,  lw + PAD_X*2, lh + PAD_Y*2),
            (lx - (lw+PAD_X*2)//2, tip_above, lw + PAD_X*2, lh + PAD_Y*2),
            (lx - (lw+PAD_X*2)//2, tip_below, lw + PAD_X*2, lh + PAD_Y*2),
            (tip_right, tip_above, lw + PAD_X*2, lh + PAD_Y*2),
            (tip_right, tip_below, lw + PAD_X*2, lh + PAD_Y*2),
            (tip_left,  tip_above, lw + PAD_X*2, lh + PAD_Y*2),
            (tip_left,  tip_below, lw + PAD_X*2, lh + PAD_Y*2),
        ]
        # Clamp candidates to canvas
        valid = []
        for cx, cy, cw, ch in candidates:
            cx = max(4, min(canvas.width  - cw - 4, cx))
            cy = max(4, min(canvas.height - ch - 4, cy))
            valid.append((cx, cy, cw, ch))

        bx, by, bw, bh = _try_place(valid)

        # Draw chip: white rounded rect + colored left bar
        draw.rounded_rectangle([bx, by, bx+bw, by+bh], radius=5,
                                fill=(255,255,255), outline=(210,210,215))
        draw.rounded_rectangle([bx, by, bx+4, by+bh], radius=2, fill=color)
        # Name in SemiBold, distance in Regular-light
        nx, ny = bx + PAD_X + 2, by + PAD_Y
        # Draw name part bold, dist part regular
        nb = draw.textbbox((nx, ny), name_part, font=font_md)
        draw.text((nx, ny), name_part, font=font_md, fill=(18,18,22))
        draw.text((nx + (nb[2]-nb[0]), ny + 1), dist_part, font=font_sm, fill=(100,100,110))

    # ── Layer 4: Dark USP card ────────────────────────────────────────────────
    if top_usps:
        CARD_BG   = (18, 20, 30)
        CARD_GOLD = (255, 195, 25)
        card_w  = 500
        row_h   = 26
        card_h  = 36 + len(top_usps) * row_h + 10
        cx1, cy1 = 14, 14
        cx2, cy2 = cx1 + card_w, cy1 + card_h
        # Card body
        draw.rounded_rectangle([cx1, cy1, cx2, cy2], radius=8, fill=CARD_BG)
        # Gold top accent stripe
        draw.rounded_rectangle([cx1, cy1, cx2, cy1+4], radius=4, fill=CARD_GOLD)
        # Header text
        draw.text((cx1+12, cy1+9), "TOP LOCATION HIGHLIGHTS", font=font_usp_hdr, fill=CARD_GOLD)
        # Rows
        for usp in top_usps:
            row_y = cy1 + 36 + (usp["rank"] - 1) * row_h
            cat = next((p["category"] for p in pois if p["name"] and p["name"] in usp["headline_text"]), None)
            dot_color = CATEGORY_COLORS.get(cat, (160, 160, 180)) if cat else (160, 160, 180)
            # Category dot
            draw.ellipse([cx1+12, row_y+6, cx1+20, row_y+14], fill=dot_color)
            display_text = usp["headline_text"].replace("\u2014", "-")
            if len(display_text) > 66:
                display_text = display_text[:63] + "..."
            draw.text((cx1 + 26, row_y+2), display_text, font=font_usp_row, fill=(230, 230, 238))
        # Register card in collision list so POI labels avoid it
        placed_boxes.append((cx1, cy1, cx2, cy2))

    # ── Layer 5: Central project pin ─────────────────────────────────────────
    PR = 13
    # Soft shadow
    draw.ellipse([proj_px-PR, proj_py-PR+3, proj_px+PR, proj_py+PR+3], fill=(0,0,0,50))
    # White outer ring
    draw.ellipse([proj_px-PR, proj_py-PR, proj_px+PR, proj_py+PR], fill=(255,255,255))
    # Gold fill
    draw.ellipse([proj_px-PR+2, proj_py-PR+2, proj_px+PR-2, proj_py+PR-2],
                 fill=(255, 210, 0), outline=(170, 120, 0), width=2)
    # Inner dark dot
    draw.ellipse([proj_px-4, proj_py-4, proj_px+4, proj_py+4], fill=(110, 65, 0))

    # Name badge above pin (NotoSans Bold 16pt)
    proj_name = project["name"]
    nb_tb = draw.textbbox((0, 0), proj_name, font=font_lg)
    nw, nh = nb_tb[2]-nb_tb[0], nb_tb[3]-nb_tb[1]
    bx = max(8, min(canvas.width - nw - 28, proj_px - nw//2))
    by = proj_py - nh - 30
    draw.rounded_rectangle([bx-10, by-6, bx+nw+10, by+nh+6], radius=6,
                            fill=(255, 210, 0), outline=(150, 100, 0), width=2)
    draw.text((bx, by), proj_name, font=font_lg, fill=(25, 15, 0))
    # Stem
    draw.line([(proj_px, by+nh+6), (proj_px, proj_py-PR)], fill=(150, 100, 0), width=2)

    # ── Layer 6: OSM attribution ──────────────────────────────────────────────
    attr_y = canvas.height - 20
    attr_tb = draw.textbbox((6, attr_y), OSM_ATTRIBUTION, font=font_xs)
    draw.rectangle([0, attr_y-4, attr_tb[2]+10, canvas.height], fill=(255,255,255))
    draw.text((6, attr_y), OSM_ATTRIBUTION, font=font_xs, fill=(90, 90, 90))

    canvas.save(image_path)
    return StaticMapMeta(cache_key, str(image_path), RING_RADII_KM, True, from_cache=False)

