from __future__ import annotations

from pydantic import BaseModel, Field


class NextPrayer(BaseModel):
    name: str
    time: str
    datetime: str = Field(description="ISO datetime in the location's timezone")
    seconds_remaining: int
    is_tomorrow: bool = False


class PrayerTimesResponse(BaseModel):
    date: str
    timezone: str
    location: dict[str, float]
    method: dict[str, str]
    times: dict[str, str] = Field(description="24-hour HH:MM, location's local time")
    times_12h: dict[str, str]
    next_prayer: NextPrayer | None = None
    kind: str = "calculated"
    note: str = ("Calculated astronomical prayer times for this location. Individual masjids may "
                 "hold Azan / Jamaat at different times - check the masjid's own schedule.")
