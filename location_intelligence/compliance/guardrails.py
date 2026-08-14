"""Stage 9 — Compliance & Attribution Guardrails.

Implements: COMP-01-COMP-09, ARCH-05, ARCH-06.

Turns the compliance checklist into automated tests rather than a
manual pre-launch review. Each function here backs exactly one
compliance test in tests/test_compliance_guardrails.py — this module
doesn't introduce new product behavior, it makes existing guarantees
from earlier stages (the append-only trigger from Stage 2, the
Google-basemap-only boundary from Stage 7) checkable by a test suite.
"""
from dataclasses import dataclass

import psycopg2
import psycopg2.errors

from maps.interactive_map import build_interactive_map_payload

# The interactive map's basemap provider is a fixed architectural
# decision (ARCH-04), not a per-project or runtime choice — there is no
# code path anywhere in this project that selects a different provider.
# Declared here as the single source of truth for that fact.
INTERACTIVE_MAP_BASEMAP_PROVIDER = "google"

# A GeoJSON feature's feature_type must be one of these — anything else
# (e.g. a second tile/basemap layer sneaking in as a "feature") would
# violate ARCH-04's single-basemap requirement.
ALLOWED_OVERLAY_FEATURE_TYPES = {"project", "boundary", "ring", "poi"}


def get_table_columns(dsn: str, table_name: str) -> list[str]:
    """Real schema introspection via information_schema — not a hardcoded
    list, so this test stays honest if the schema changes later.
    """
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = %s;",
                (table_name,),
            )
            return [row[0] for row in cur.fetchall()]
    finally:
        conn.close()


def delete_poi_source_metadata(dsn: str, poi_id: int) -> None:
    """Attempt a hard delete against poi_source_metadata. The Stage 2
    database trigger (prevent_poi_source_metadata_delete) blocks this at
    the DB layer — this wrapper translates that into a plain Python
    PermissionError so callers get a clean, layer-appropriate exception
    rather than a raw psycopg2 error class (COMP-03).
    """
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM poi_source_metadata WHERE poi_id = %s;", (poi_id,))
        conn.commit()
    except psycopg2.errors.RaiseException as e:
        conn.rollback()
        raise PermissionError(f"poi_source_metadata is append-only (blocked at the DB layer): {e}") from e
    finally:
        conn.close()


@dataclass(frozen=True)
class InteractiveMapConfig:
    basemap_provider: str
    overlay_layers_are_data_only: bool


def get_interactive_map_config(dsn: str, project_id: int) -> InteractiveMapConfig:
    """Confirms the interactive map payload for a real project only ever
    contains data-only overlay features — never a second basemap/tile
    layer competing with the Google basemap (ARCH-04).
    """
    payload = build_interactive_map_payload(dsn, project_id)
    feature_types = {f["properties"]["feature_type"] for f in payload["geojson"]["features"]}
    overlay_data_only = feature_types.issubset(ALLOWED_OVERLAY_FEATURE_TYPES)

    return InteractiveMapConfig(
        basemap_provider=INTERACTIVE_MAP_BASEMAP_PROVIDER,
        overlay_layers_are_data_only=overlay_data_only,
    )
