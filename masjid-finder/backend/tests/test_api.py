"""API tests. Offline tests always run; live ones (real OSM data) need
RUN_LIVE=1 and internet:  RUN_LIVE=1 pytest -q
"""

import os

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import maps_service

client = TestClient(app)
live = pytest.mark.skipif(os.environ.get("RUN_LIVE") != "1", reason="set RUN_LIVE=1 for network tests")


def test_haversine_known_distance():
    # Delhi (India Gate) -> Agra (Taj Mahal) is ~178 km in a straight line
    d = maps_service.haversine_m(28.6129, 77.2295, 27.1751, 78.0421)
    assert 175_000 < d < 181_000


def test_directions_links():
    links = maps_service.directions(28.5, 77.2, "Jama Masjid")
    assert links["google"].startswith("https://www.google.com/maps/dir/?api=1&destination=28.500000,77.200000")


def test_prayer_endpoint():
    r = client.get("/api/prayer-times", params={"latitude": 28.6139, "longitude": 77.2090, "date": "2026-10-02"})
    assert r.status_code == 200
    body = r.json()
    assert body["timezone"] == "Asia/Kolkata"
    assert body["method"]["id"] == "KARACHI"
    assert body["kind"] == "calculated"
    assert set(body["times"]) == {"fajr", "sunrise", "dhuhr", "asr", "maghrib", "isha"}


def test_validation_errors():
    assert client.get("/api/mosques/nearby", params={"latitude": 200, "longitude": 0}).status_code == 422
    assert client.get("/api/mosques/nearby", params={"latitude": 0, "longitude": 0, "radius": 50000}).status_code == 422
    assert client.get("/api/prayer-times", params={"latitude": 0}).status_code == 422


@live
def test_live_nearby_okhla():
    r = client.get("/api/mosques/nearby", params={"latitude": 28.5617, "longitude": 77.2826, "radius": 3000})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] > 0
    d = [m["distance_meters"] for m in body["mosques"]]
    assert d == sorted(d) and d[-1] <= 3000
    for m in body["mosques"]:
        assert m["source"] in ("openstreetmap", "google_places")
        assert m["source_url"]


@live
def test_live_search_and_reverse():
    r = client.get("/api/location/search", params={"q": "Mecca"})
    assert r.status_code == 200 and r.json()["results"][0]["country_code"] == "SA"
    r = client.get("/api/location/reverse", params={"latitude": 19.076, "longitude": 72.8777})
    assert r.status_code == 200 and r.json()["country_code"] == "IN"
