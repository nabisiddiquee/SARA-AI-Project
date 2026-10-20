"""Email, stock-market, and multi-city weather features (no network used)."""

import my_python_project.actions as actions
import my_python_project.mailer as mailer
import my_python_project.stocks as stocks
import my_python_project.weather as weather
from my_python_project.ai import AIResponse

# --- email -------------------------------------------------------------------


def _draft_with(monkeypatch, reply):
    monkeypatch.setattr("my_python_project.ai.ask_ai", lambda *a, **k: reply)
    monkeypatch.setattr(mailer, "_pending", None)


def test_email_is_drafted_then_sent_only_after_yes(monkeypatch):
    _draft_with(monkeypatch, AIResponse(True, "Subject: Meeting\n\nMeeting is at 5 pm."))
    sent = []
    monkeypatch.setattr(mailer, "send", lambda d: (sent.append(d), (True, ""))[1])

    ok, msg = mailer.start_email("abc@gmail.com ko email bhejo meeting 5 baje hai")
    assert ok and "To: abc@gmail.com" in msg and "Subject: Meeting" in msg
    assert mailer.email_pending() and sent == []

    ok, msg = mailer.confirm_email("haan bhej do")
    assert ok and sent[0].to == "abc@gmail.com" and sent[0].body == "Meeting is at 5 pm."
    assert not mailer.email_pending()


def test_email_cancelled_on_no(monkeypatch):
    _draft_with(monkeypatch, AIResponse(True, "Subject: Hi\n\nHello"))
    monkeypatch.setattr(mailer, "send", lambda d: (_ for _ in ()).throw(AssertionError))
    mailer.start_email("email abc@gmail.com hello")
    ok, msg = mailer.confirm_email("nahi")
    assert "not sent" in msg


def test_email_needs_a_recipient(monkeypatch):
    _draft_with(monkeypatch, AIResponse(True, "unused"))
    monkeypatch.setattr(mailer, "load_contacts", lambda: {})
    ok, _ = mailer.start_email("email bhejo meeting kal hai")
    assert not ok and not mailer.email_pending()


def test_email_recipient_from_contacts(monkeypatch):
    monkeypatch.setattr(mailer, "load_contacts", lambda: {"boss": "boss@company.com"})
    to, rest = mailer.find_recipient("boss ko email karo chhutti chahiye")
    assert to == "boss@company.com" and "boss" not in rest


def test_send_without_smtp_settings_explains_setup(monkeypatch):
    monkeypatch.delenv("SMTP_USER", raising=False)
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)
    ok, msg = mailer.send(mailer.Draft("a@b.com", "s", "b"))
    assert not ok and "App Password" in msg


def test_parse_draft():
    assert mailer.parse_draft("Subject: Leave\n\nI am on leave.", "x") == (
        "Leave", "I am on leave.")
    assert mailer.parse_draft("Just a body", "Fallback") == ("Fallback", "Just a body")


# --- stocks ------------------------------------------------------------------


def test_extract_stock_names():
    assert stocks.extract_names("reliance aur tcs ka share price batao") == ["reliance", "tcs"]
    assert stocks.extract_names("market kaisa hai") == []
    assert stocks.extract_names("bank nifty kitna hai") == ["bank nifty"]


def test_get_stocks_formats_prices(monkeypatch):
    monkeypatch.setattr(stocks, "quote", lambda s: {
        "price": 23089.25, "change": -357.8, "pct": -1.53, "currency": "INR"})
    ok, msg = stocks.get_stocks("nifty kitna hai")
    assert ok and msg == "Nifty 50: ₹23,089.25 ▼ -357.80 (-1.53%)"


def test_get_stocks_default_is_nifty_and_sensex(monkeypatch):
    monkeypatch.setattr(stocks, "quote", lambda s: {
        "price": 1.0, "change": 0.0, "pct": 0.0, "currency": "INR"})
    ok, msg = stocks.get_stocks("share market kaisa hai")
    assert ok and msg.startswith("Nifty 50") and "Sensex" in msg


# --- weather -----------------------------------------------------------------


def test_split_cities_and_all_cities():
    assert weather.split_cities("delhi, mumbai aur patna") == ["delhi", "mumbai", "patna"]
    assert weather.split_cities("orissa") == ["orissa"]  # "or" inside a word
    assert weather.wants_all_cities("sabhi shahar ka mausam")
    assert not weather.wants_all_cities("darbhanga ka mausam")


def test_find_place_uses_region_word(monkeypatch):
    places = {
        "darbhanga": {"name": "Darbhanga", "admin1": "Bihar"},
        "bihar": {"name": "Bihar Sharif", "admin1": "Bihar"},
    }
    monkeypatch.setattr(weather, "geocode", lambda n: places.get(n.lower()))
    assert weather.find_place("bihar darbhanga")["name"] == "Darbhanga"


def test_weather_uses_default_city(monkeypatch):
    monkeypatch.setenv("SARA_CITY", "Darbhanga")
    seen = []
    monkeypatch.setattr(weather, "_weather_one",
                        lambda c, **k: (seen.append(c), (True, "ok"))[1])
    assert weather.get_weather("") == (True, "ok") and seen == ["Darbhanga"]


def test_weather_subject_keeps_city_list():
    subject = actions._extract_subject(
        "Delhi, Mumbai aur Patna ka mausam batao", ("mausam",), keep_commas=True)
    assert weather.split_cities(subject) == ["delhi", "mumbai", "patna"]
