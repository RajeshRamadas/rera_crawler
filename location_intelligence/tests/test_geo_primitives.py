"""STAGE 1 CHECK — pure-function tests, zero DB connection opened.

Implements: GEO-02, GEO-03, GEO-04.
"""
import pytest

from geo.distance import haversine_km
from geo.bearing import bearing_deg
from geo.direction import compass_16
from geo.distance_bands import distance_band


def test_haversine_known_pair():
    # Bengaluru city centre to Kempegowda International Airport coordinates.
    # NOTE: spec draft assumed 30-45km for this pair; actual great-circle
    # distance is ~28km. Corrected here per Stage 1 rule ("if a test fails,
    # fix that stage") rather than loosening the implementation.
    d = haversine_km(12.9716, 77.5946, 13.1986, 77.7066)
    assert 25 < d < 32


def test_haversine_zero_distance():
    assert haversine_km(12.97, 77.59, 12.97, 77.59) == 0


def test_haversine_symmetric():
    d1 = haversine_km(12.9716, 77.5946, 13.1986, 77.7066)
    d2 = haversine_km(13.1986, 77.7066, 12.9716, 77.5946)
    assert d1 == pytest.approx(d2)


def test_bearing_due_north():
    b = bearing_deg(12.0, 77.0, 13.0, 77.0)
    assert 358 <= b or b <= 2  # ~0deg


def test_bearing_due_east():
    b = bearing_deg(12.0, 77.0, 12.0, 78.0)
    assert 88 <= b <= 92  # ~90deg


def test_bearing_range():
    b = bearing_deg(12.9716, 77.5946, 13.1986, 77.2066)
    assert 0 <= b < 360


def test_compass_16_covers_all_points():
    seen = {compass_16(b) for b in range(0, 360, 5)}
    assert len(seen) == 16


def test_compass_16_cardinal_points():
    assert compass_16(0) == "N"
    assert compass_16(90) == "E"
    assert compass_16(180) == "S"
    assert compass_16(270) == "W"


def test_distance_band_edges():
    assert distance_band(0) == "immediate"
    assert distance_band(1.999) == "immediate"
    assert distance_band(2.0) == "daily"
    assert distance_band(19.999) == "strategic"
    assert distance_band(20.0) is None  # outside default radius


def test_distance_band_all_named_bands():
    assert distance_band(1) == "immediate"
    assert distance_band(3) == "daily"
    assert distance_band(7) == "city"
    assert distance_band(15) == "strategic"
