"""Live stock-market prices via Yahoo Finance (free, no API key).

Understands index names (Nifty, Sensex, Bank Nifty, Dow, Nasdaq...), company
names ("Tata Motors", "Reliance", "Apple"), and ticker symbols (TCS.NS, AAPL).
With no name ("market kaisa hai") it gives a Nifty + Sensex summary.
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request

_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
_SEARCH_URL = "https://query2.finance.yahoo.com/v1/finance/search"
_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

# Friendly names -> (symbol, display name). Checked before searching Yahoo.
_INDICES: dict[str, tuple[str, str]] = {
    "bank nifty": ("^NSEBANK", "Bank Nifty"),
    "banknifty": ("^NSEBANK", "Bank Nifty"),
    "nifty it": ("^CNXIT", "Nifty IT"),
    "nifty 50": ("^NSEI", "Nifty 50"),
    "nifty": ("^NSEI", "Nifty 50"),
    "sensex": ("^BSESN", "Sensex"),
    "dow jones": ("^DJI", "Dow Jones"),
    "dow": ("^DJI", "Dow Jones"),
    "nasdaq": ("^IXIC", "Nasdaq"),
    "s&p 500": ("^GSPC", "S&P 500"),
    "s&p": ("^GSPC", "S&P 500"),
    "sp500": ("^GSPC", "S&P 500"),
    "gold": ("GC=F", "Gold (per ounce)"),
    "sona": ("GC=F", "Gold (per ounce)"),
    "silver": ("SI=F", "Silver (per ounce)"),
    "chandi": ("SI=F", "Silver (per ounce)"),
    "crude oil": ("CL=F", "Crude oil"),
    "bitcoin": ("BTC-USD", "Bitcoin"),
    "dollar": ("INR=X", "US Dollar to Rupee"),
}

_DEFAULT = ("nifty", "sensex")

# Words removed when pulling the company/index name out of a question.
_FILLER = {
    "stock", "stocks", "share", "shares", "price", "prices", "rate", "rates",
    "bhav", "market", "stock market", "share market", "today", "aaj", "abhi",
    "ka", "ki", "ke", "kya", "hai", "hain", "kitna", "kitni", "kaisa", "kaisi",
    "batao", "bata", "dikhao", "what", "is", "the", "of", "how", "current",
    "live", "value", "info", "information", "update", "updates", "mein",
    "chal", "raha", "rahi", "gaya", "status", "tell", "me", "please", "and",
    "aur", "check", "karo", "kar", "do", "quote", "index", "level", "trading",
    "शेयर", "स्टॉक", "मार्केट", "बाजार", "का", "की", "के", "क्या", "है",
}

# Words that mark a stock-market request (used by the dispatcher).
STOCK_KWS = (
    "stock", "stocks", "share price", "share market", "stock market", "shares",
    "nifty", "sensex", "bank nifty", "nasdaq", "dow jones", "share ka bhav",
    "शेयर", "स्टॉक", "सेंसेक्स", "निफ्टी", "शेयर बाजार",
)


def _get_json(url: str, params: dict | None = None) -> dict:
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
        return json.load(resp)


def extract_names(text: str) -> list[str]:
    """Split "tcs aur infosys ka share price" -> ["tcs", "infosys"]."""
    low = text.lower()
    for phrase in sorted(_FILLER, key=len, reverse=True):
        if " " in phrase:
            low = low.replace(phrase, " ")
    parts = re.split(r",|\band\b|\baur\b|\bvs\b|&(?!p)", low)
    names = []
    for part in parts:
        words = [w for w in re.sub(r"[?!.]", " ", part).split() if w not in _FILLER]
        if words:
            names.append(" ".join(words))
    return names


def resolve(name: str) -> tuple[str, str] | None:
    """Map a spoken name to ``(symbol, display name)``."""
    key = name.lower().strip()
    if key in _INDICES:
        return _INDICES[key]
    if re.fullmatch(r"[A-Za-z0-9^.=-]{1,15}", name) and any(c in name for c in ".^="):
        return name.upper(), name.upper()
    data = _get_json(_SEARCH_URL, {"q": name, "quotesCount": 6, "newsCount": 0})
    quotes = [q for q in data.get("quotes", []) if q.get("symbol")]
    if not quotes:
        return None
    # Prefer Indian listings (NSE, then BSE), then any equity/index/ETF.
    for suffix in (".NS", ".BO"):
        for q in quotes:
            if q["symbol"].endswith(suffix):
                return q["symbol"], q.get("longname") or q.get("shortname") or q["symbol"]
    q = quotes[0]
    return q["symbol"], q.get("longname") or q.get("shortname") or q["symbol"]


def quote(symbol: str) -> dict:
    """Latest price, change, and currency for ``symbol``."""
    data = _get_json(_CHART_URL.format(symbol=urllib.parse.quote(symbol)),
                     {"range": "1d", "interval": "1d"})
    meta = data["chart"]["result"][0]["meta"]
    price = meta["regularMarketPrice"]
    prev = meta.get("chartPreviousClose") or meta.get("previousClose") or price
    change = price - prev
    pct = meta.get("regularMarketChangePercent")
    if pct is None:
        pct = (change / prev * 100) if prev else 0.0
    return {"price": price, "change": change, "pct": pct,
            "currency": meta.get("currency", "")}


def _format(label: str, q: dict, lang: str) -> str:
    sym = {"INR": "₹", "USD": "$"}.get(q["currency"], "")
    arrow = "▲" if q["change"] >= 0 else "▼"
    base = f"{label}: {sym}{q['price']:,.2f} {arrow} {q['change']:+,.2f} ({q['pct']:+.2f}%)"
    if lang == "hi":
        word = "ऊपर" if q["change"] >= 0 else "नीचे"
        return f"{base} — {word}"
    return base


def get_stocks(text: str, *, lang: str = "en") -> tuple[bool, str]:
    """Answer a stock/market question. Never raises."""
    names = extract_names(text) or list(_DEFAULT)
    lines, missing = [], []
    try:
        for name in names[:6]:
            hit = resolve(name)
            if not hit:
                missing.append(name)
                continue
            symbol, label = hit
            lines.append(_format(label, quote(symbol), lang))
    except Exception as e:  # noqa: BLE001
        return False, (f"अभी शेयर बाजार की जानकारी नहीं ला पाई: {e}" if lang == "hi"
                       else f"I couldn't fetch market data right now: {e}")
    found = bool(lines)
    if missing:
        lines.append(f"नहीं मिला: {', '.join(missing)}" if lang == "hi"
                     else f"Not found: {', '.join(missing)}")
    return found, "\n".join(lines)
