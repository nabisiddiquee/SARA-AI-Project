from my_python_project import geo, radio


def test_ghazal_station_listed_even_when_directory_is_down(monkeypatch):
    def down(*a, **k):
        raise OSError("offline")

    monkeypatch.setattr(radio, "_get", down)
    radio._cache.clear()
    names = [s["name"] for s in radio.stations("ghazal")]
    assert names[0] == "Best of Ghazals"
    radio._cache.clear()


def test_location_questions():
    for text in ("meri location kya hai", "Mera live location bolo", "where am i",
                 "main kahan hoon", "मेरी लोकेशन बताओ"):
        assert geo.is_location_question(text), text
    for text in ("masjid ki location", "delhi location", "send location to rahul"):
        assert not geo.is_location_question(text), text


def test_location_reply_prefers_gps_place(monkeypatch):
    monkeypatch.setattr(geo, "_last_place", {"ok": True, "area": "Shaheen Bagh", "city": "Delhi",
                                             "state": "Delhi", "postcode": "110025", "country": "India"})
    ok, msg = geo.location_reply(hi=False, ip_info={"city": "Ghaziabad"})
    assert ok and msg == "You are at Shaheen Bagh, Delhi, 110025, India right now."


def test_location_reply_falls_back_to_ip_city(monkeypatch):
    monkeypatch.setattr(geo, "_last_place", None)
    ok, msg = geo.location_reply(hi=False, ip_info={"city": "Delhi", "country": "India"})
    assert ok and "Delhi, India" in msg
