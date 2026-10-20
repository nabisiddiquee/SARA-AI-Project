"""Current date and time, with optional per-city timezone.

The LLM brain has no live clock, so Sara answers time/date questions here.
Without a city it uses the local system time; with a city it looks up that
place's timezone via Open-Meteo's geocoder. City lookups need the ``tzdata``
package on Windows (install with ``pip install -e ".[time]"``).
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

_HI_DAYS = {
    0: "सोमवार", 1: "मंगलवार", 2: "बुधवार", 3: "गुरुवार",
    4: "शुक्रवार", 5: "शनिवार", 6: "रविवार",
}
_HI_MONTHS = {
    1: "जनवरी", 2: "फ़रवरी", 3: "मार्च", 4: "अप्रैल", 5: "मई", 6: "जून",
    7: "जुलाई", 8: "अगस्त", 9: "सितंबर", 10: "अक्टूबर", 11: "नवंबर", 12: "दिसंबर",
}


def get_datetime(city: str = "", *, lang: str = "en") -> tuple[bool, str]:
    """Return ``(ok, message)`` with the current time/date.

    If ``city`` is given, the time is reported in that city's timezone; else the
    local system timezone is used. Never raises.
    """
    city = (city or "").strip()
    place = ""
    try:
        if city:
            from .weather import geocode

            loc = geocode(city)
            if not loc:
                msg = (
                    f"माफ़ कीजिए, '{city}' नाम का शहर नहीं मिला।" if lang == "hi"
                    else f"Sorry, I couldn't find a place called {city!r}."
                )
                return False, msg
            now = datetime.now(ZoneInfo(loc.get("timezone") or "UTC"))
            place = loc.get("name") or city
        else:
            now = datetime.now().astimezone()
    except Exception as e:  # noqa: BLE001
        msg = (
            f"अभी समय की जानकारी नहीं मिली: {e}" if lang == "hi"
            else f"I couldn't get the time right now: {e}"
        )
        return False, msg

    time_str = now.strftime("%I:%M %p").lstrip("0")
    if lang == "hi":
        where = f"{place} में " if place else ""
        msg = (
            f"{where}अभी समय {time_str} है — "
            f"{_HI_DAYS[now.weekday()]}, {now.day} {_HI_MONTHS[now.month]} {now.year}।"
        )
    else:
        where = f"in {place} " if place else ""
        msg = f"It's {time_str} {where}— {now.strftime('%A')}, {now.day} {now.strftime('%B %Y')}."
    return True, msg
