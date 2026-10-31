"""STAGE 2 CHECK — POI schema + taxonomy.

Implements: POI-01, POI-02, DB-01.
"""
import os
import pytest

from poi.categories import (
    REQUIRED_CATEGORIES,
    load_category_rules,
    add_category_rule,
    classify_tag,
)

DSN = os.environ.get(
    "HOMEXO_TEST_DSN",
    "host=localhost dbname=homexo_dev user=homexo password=homexo",
)


def test_taxonomy_covers_required_categories():
    cats = {r.category for r in load_category_rules(DSN)}
    assert REQUIRED_CATEGORIES.issubset(cats)


def test_adding_a_category_is_data_only():
    # insert a new rule row directly, no code deploy, and confirm it's picked up
    add_category_rule(DSN, osm_tag="amenity=library", category="lifestyle", subcategory="library")
    assert classify_tag(DSN, "amenity=library") == ("lifestyle", "library")


def test_unmapped_tag_returns_none():
    assert classify_tag(DSN, "amenity=totally_made_up_tag_xyz") is None
