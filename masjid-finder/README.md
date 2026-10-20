# Masjid Finder

Find masjids near you, get directions, and see today's prayer times with a live countdown to the next prayer. Works from GPS, or from a city or address you type in.

- **Real data only.** Mosques come from OpenStreetMap (Overpass API). If you add a Google key, Google Places results are merged in. Nothing is invented: rating, open-now, phone, website and Jamaat times appear only when the source has them.
- **Calculated vs. Jamaat.** Prayer times are astronomical calculations for your location, and the app labels them that way. A masjid's own Jamaat times appear separately, and only when the source publishes them (the OSM `service_times` tag).

## Architecture

```
Browser (React + Vite + Leaflet)
  │  GET /api/...   (no third-party keys in the browser)
  ▼
FastAPI backend
  ├─ mosque_service   → Overpass API (OSM)  [+ Google Places API (New) if key set]
  ├─ location_service → Nominatim (search / reverse), timezonefinder (offline)
  ├─ prayer_service   → local calculation (PrayTimes algorithm), no API
  ├─ maps_service     → haversine distance, Google / Apple / OSM direction links
  └─ cache            → in-memory TTL cache (swap for Redis when scaling out)
```

| Need | Choice | Why |
|---|---|---|
| Mosque data | OpenStreetMap via Overpass | Free, no key, worldwide, returns every mapped mosque in the radius |
| Ratings, reviews, open now | Google Places API (New), optional | Only Google has these. Its key stays on the server. |
| Location search and reverse | Nominatim | Free; limited to 1 request/second, which the backend throttles and caches |
| Prayer times | Calculated locally | No rate limits, works offline on the server. Verified against Aladhan within ±2 min. |
| Timezone | timezonefinder + tzdata | Offline lookup from coordinates |
| Map | Leaflet + OSM tiles | Free, no key, light on mobile |

The **auto** prayer method chooses by country:

| Country | Method | Asr |
|---|---|---|
| India, Pakistan, Bangladesh | Karachi | Hanafi |
| Saudi Arabia | Umm al-Qura | standard |
| UAE | Dubai | standard |
| USA, Canada | ISNA | standard |
| Anywhere else | MWL | standard |

You can override both the method and the Asr calculation with `PRAYER_METHOD` and `ASR_METHOD` in `.env`, or per request.

## Folder structure

```
masjid-finder/
├─ backend/
│  ├─ app/
│  │  ├─ main.py               FastAPI app, CORS, error handlers, serves frontend/dist
│  │  ├─ config.py             settings from .env
│  │  ├─ api/                  mosque_routes.py, prayer_routes.py, location_routes.py
│  │  ├─ services/             mosque, prayer, location, maps services + cache
│  │  └─ models/               mosque.py, prayer.py (response schemas)
│  ├─ tests/                   test_prayer_times.py, test_api.py
│  ├─ requirements.txt
│  └─ .env.example
├─ frontend/
│  ├─ src/                     App.jsx, api.js, useGeolocation.js, format.js, components/
│  ├─ package.json
│  └─ vite.config.js           proxies /api to :8000 in development
└─ .gitignore                  ignores .env, node_modules, .venv, dist
```

## API

| Endpoint | Purpose |
|---|---|
| `GET /api/mosques/nearby?latitude=&longitude=&radius=5000` | Masjids sorted by distance. `radius` is 100–20000 m; the app offers 1, 3, 5, 10 and 20 km. |
| `GET /api/mosques/address?latitude=&longitude=` | Approximate address for a masjid that has none listed, marked `approximate: true` |
| `GET /api/prayer-times?latitude=&longitude=&date=YYYY-MM-DD` | Today's times plus the next prayer. Optional parameters: `method`, `asr`, `country`, `timezone`. |
| `GET /api/location/search?q=Dubai` | Manual location search |
| `GET /api/location/reverse?latitude=&longitude=` | City, state, country and timezone |
| `GET /api/health`, `GET /docs` | Health check and interactive API docs |

Errors always come back as `{"detail": "human-readable message"}`, with these status codes:

| Status | Meaning |
|---|---|
| 422 | Invalid input |
| 503 | Upstream service busy |
| 502 | Upstream service failed |

## Setup

**Backend** (Python 3.10+):

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate          # Windows   (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt
copy .env.example .env          # macOS/Linux: cp .env.example .env
uvicorn app.main:app --reload --port 8000
```

**Frontend** (Node 18+):

```bash
cd frontend
npm install
npm run dev                     # http://localhost:5173  (/api is proxied to :8000)
```

### API keys

- **None are required.** OpenStreetMap, Nominatim and the prayer calculation are all free.
- **Optional: Google Places.** It adds ratings, reviews, open now, phone and website. To set it up:
  1. Enable "Places API (New)" in Google Cloud.
  2. Create a key restricted to that API (and to your server's IP).
  3. Put it in `backend/.env` as `GOOGLE_MAPS_API_KEY=...`.

  The key is only ever used by the backend. Never put it in `frontend/.env`, because anything named `VITE_*` ends up in the browser.
- **Contact email.** Set `CONTACT_EMAIL` in `.env`. The OpenStreetMap usage policies ask for a way to contact you.

## Location states handled

| State | What the user sees |
|---|---|
| Permission granted | GPS location, then reverse-geocoded to area, city, state and country |
| Permission denied | "Location permission is off. Enter your city or location manually." The app opens the search screen. |
| GPS off / position unavailable | Message, plus *Try again* and *Enter location* |
| Timeout (15 s) | Message, plus *Try again* |
| Browser without geolocation | Manual search |
| Page not on HTTPS | Manual search. Browsers allow GPS only on HTTPS or localhost. |
| Offline | Banner. The last loaded data stays visible and refreshes when the connection returns. |
| No mosques in the radius | Message, plus a one-tap "Search within N km" with the next larger radius |

## Testing

```bash
cd backend
pytest -q                       # offline: prayer maths vs Aladhan (Delhi, Makkah), next prayer, validation
RUN_LIVE=1 pytest -q            # + real Overpass / Nominatim calls (needs internet)
```

Checked on 2026-10-02:

- **Delhi** (Karachi method, Hanafi Asr) and **Makkah** (Umm al-Qura) match Aladhan within 1 minute.
- **Okhla, New Delhi:** real results from the API.

  | Radius | Mosques |
  |---|---|
  | 3 km | 17 |
  | 10 km | 73 |
  | 20 km | 146 |

  Results were sorted nearest first, and every distance was ≤ the radius.
- **Searching "Mecca"** returns Makkah, Saudi Arabia first. Nominatim's raw order puts a hamlet in Italy first, so the backend re-ranks results by Nominatim's importance score.

## Deployment

1. Build the frontend with `cd frontend && npm run build`. FastAPI then serves `frontend/dist` from the same origin, so no CORS setup is needed.
2. Run the backend:
   ```bash
   uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 2
   ```
   Put it behind HTTPS, because browsers need HTTPS for GPS. Caddy, nginx and a managed platform all work: Render, Railway, Fly.io or Cloud Run.
3. Set the environment variables on the host instead of shipping `.env`.
4. Plan for heavier traffic:
   - The public Nominatim and Overpass servers are shared and rate-limited. Self-host them, or switch to a paid geocoder.
   - Move the cache to Redis so it is shared between workers.

## Why React (web) first, not Flutter

- **One URL works everywhere.** Phones, laptops and WhatsApp-shared links all open it, with no app store install. Geolocation, `tel:` links and opening Google or Apple Maps all work from the browser.
- **The map stack is mature on the web.** Leaflet and OSM tiles are free and light. Flutter maps (`flutter_map` or `google_maps_flutter`) work too, but add native build and signing work.
- **It can be installed later as a PWA.** That gives a home-screen icon with no store review.
- **Choose Flutter when you need native features**, such as Azan notifications at prayer time even when the app is closed, background location, or widgets. The backend API here stays the same either way; only the client changes.
