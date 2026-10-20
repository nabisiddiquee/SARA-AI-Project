"""Settings from environment variables / backend/.env (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
ROOT_DIR = BACKEND_DIR.parent


def _load_dotenv() -> None:
    env = BACKEND_DIR / ".env"
    if not env.is_file():
        return
    for raw in env.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.split(" #", 1)[0].strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


_load_dotenv()


@dataclass(frozen=True)
class Settings:
    # Optional: Google Places API (New) for ratings / reviews / open now / phone.
    # Without it, mosques come from OpenStreetMap (free, no key).
    google_maps_api_key: str = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()

    # OpenStreetMap services (no key; identify the app per their usage policies).
    contact_email: str = os.environ.get("CONTACT_EMAIL", "").strip()
    overpass_urls: tuple[str, ...] = tuple(u.strip() for u in os.environ.get(
        "OVERPASS_URLS", "https://overpass-api.de/api/interpreter,https://overpass.kumi.systems/api/interpreter"
    ).split(",") if u.strip())
    nominatim_url: str = os.environ.get("NOMINATIM_URL", "https://nominatim.openstreetmap.org").rstrip("/")

    # Prayer times: "auto" picks the usual method for the country (Karachi +
    # Hanafi Asr for India/Pakistan/Bangladesh, Umm al-Qura for Saudi Arabia ...).
    prayer_method: str = os.environ.get("PRAYER_METHOD", "auto")
    asr_method: str = os.environ.get("ASR_METHOD", "auto")          # auto | standard | hanafi

    cache_ttl_mosques: int = int(os.environ.get("CACHE_TTL_MOSQUES", "1800"))
    cache_ttl_geocode: int = int(os.environ.get("CACHE_TTL_GEOCODE", "86400"))
    cors_origins: tuple[str, ...] = tuple(o.strip() for o in os.environ.get(
        "CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",") if o.strip())
    log_level: str = os.environ.get("LOG_LEVEL", "INFO")

    @property
    def user_agent(self) -> str:
        contact = f" ({self.contact_email})" if self.contact_email else ""
        return f"MasjidFinder/1.0{contact}"


settings = Settings()
