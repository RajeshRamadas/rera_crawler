"""Stage 2 — POI Taxonomy.

Implements: POI-01, POI-02. Tag -> category mapping is data-driven
(category_rules table), so adding a category is a data change, not a
code deploy.
"""
from dataclasses import dataclass
from typing import Optional

import psycopg2
import psycopg2.extras

REQUIRED_CATEGORIES = {
    "transport", "healthcare", "education", "employment",
    "shopping", "lifestyle", "civic", "hospitality", "future",
}


@dataclass(frozen=True)
class CategoryRule:
    osm_tag: str
    category: str
    subcategory: Optional[str]


def _connect(dsn: str):
    return psycopg2.connect(dsn)


def load_category_rules(dsn: str) -> list[CategoryRule]:
    """Load all category rules from the database (data-driven taxonomy, POI-02)."""
    conn = _connect(dsn)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT osm_tag, category, subcategory FROM category_rules;")
            return [CategoryRule(r["osm_tag"], r["category"], r["subcategory"]) for r in cur.fetchall()]
    finally:
        conn.close()


def add_category_rule(dsn: str, osm_tag: str, category: str, subcategory: Optional[str] = None) -> None:
    """Insert a new taxonomy rule — proves adding a category needs no code change."""
    conn = _connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO category_rules (osm_tag, category, subcategory)
                VALUES (%s, %s, %s)
                ON CONFLICT (osm_tag) DO UPDATE
                    SET category = EXCLUDED.category, subcategory = EXCLUDED.subcategory;
                """,
                (osm_tag, category, subcategory),
            )
        conn.commit()
    finally:
        conn.close()


def classify_tag(dsn: str, osm_tag: str) -> Optional[tuple[str, Optional[str]]]:
    """Return (category, subcategory) for a single 'key=value' OSM tag, or None if unmapped."""
    conn = _connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT category, subcategory FROM category_rules WHERE osm_tag = %s;",
                (osm_tag,),
            )
            row = cur.fetchone()
            return (row[0], row[1]) if row else None
    finally:
        conn.close()
