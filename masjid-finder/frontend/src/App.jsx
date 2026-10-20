import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from './api'
import { RADII, greeting, km, placeLabel } from './format'
import { store, useGeolocation, useOnline } from './useGeolocation'
import LocationSearch from './components/LocationSearch'
import MapView from './components/MapView'
import MasjidCard from './components/MasjidCard'
import { ErrorCard, NextPrayer, PrayerTable } from './components/PrayerTimes'

// Runs `fn(signal)` whenever `key` changes; cancels the previous request.
function useRequest(fn, key, enabled = true) {
  const [s, set] = useState({ data: null, error: null, loading: false })
  const [nonce, setNonce] = useState(0)
  useEffect(() => {
    if (!enabled) return
    const ctrl = new AbortController()
    set((p) => ({ ...p, loading: true, error: null }))
    fn(ctrl.signal)
      .then((data) => set({ data, error: null, loading: false }))
      .catch((e) => e.name !== 'AbortError' && set((p) => ({ ...p, error: e.message, loading: false })))
    return () => ctrl.abort()
  }, [key, enabled, nonce]) // eslint-disable-line react-hooks/exhaustive-deps
  const retry = useCallback(() => setNonce((n) => n + 1), [])
  return { ...s, retry }
}

const TABS = [['home', 'Home', '⌂'], ['list', 'Masjids', '☰'], ['map', 'Map', '◈'], ['search', 'Location', '⌕']]

export default function App() {
  const [place, setPlace] = useState(() => store.get('mf.place', null))
  const [radius, setRadius] = useState(() => store.get('mf.radius', 5000))
  const [tab, setTab] = useState('home')
  const [focus, setFocus] = useState(null)
  const geo = useGeolocation()
  const online = useOnline()
  const asked = useRef(false)

  // Ask for GPS once on load (the browser shows its own permission prompt).
  useEffect(() => { if (!asked.current) { asked.current = true; geo.locate() } }, [geo.locate])

  // GPS fix -> use it, then fill in city / state / country.
  useEffect(() => {
    if (!geo.coords) return
    const { latitude, longitude, accuracy } = geo.coords
    setPlace({ latitude, longitude, accuracy, source: 'gps' })
    setFocus(null)
    if (tab === 'search') setTab('home')
    const ctrl = new AbortController()
    api.reverse(latitude, longitude, { signal: ctrl.signal })
      .then((r) => setPlace((p) => (p && p.latitude === latitude && p.longitude === longitude ? { ...p, ...r, latitude, longitude } : p)))
      .catch(() => {})
    return () => ctrl.abort()
  }, [geo.coords]) // eslint-disable-line react-hooks/exhaustive-deps

  // Without any location, the only useful screen is manual search.
  useEffect(() => {
    if (!place && ['denied', 'unavailable', 'timeout', 'unsupported', 'insecure'].includes(geo.state)) setTab('search')
  }, [geo.state, place])

  useEffect(() => { if (place) store.set('mf.place', place) }, [place])
  useEffect(() => { store.set('mf.radius', radius) }, [radius])

  const coordKey = place ? `${place.latitude.toFixed(5)},${place.longitude.toFixed(5)}` : ''
  const mosques = useRequest((signal) => api.nearby(place.latitude, place.longitude, radius, { signal }),
    `${coordKey}|${radius}|${online}`, !!place && online)
  const prayer = useRequest((signal) => api.prayerTimes(place.latitude, place.longitude, { country: place.country_code }, { signal }),
    `${coordKey}|${place?.country_code || ''}|${online}`, !!place && online)

  const onPrayerElapsed = useCallback(() => setTimeout(prayer.retry, 1500), [prayer.retry])

  function pick(p) {
    setPlace(p)
    setFocus(null)
    setTab('home')
  }

  const list = mosques.data?.mosques || []
  const name = place?.city || place?.name || placeLabel(place)

  return (
    <div className="app">
      <header className="top">
        <div>
          <div className="brand">Masjid Finder</div>
          {place && (
            <button className="where" onClick={() => setTab('search')} title="Change location">
              <span aria-hidden>📍</span> {placeLabel(place)}
              {place.source === 'gps' && place.accuracy > 1000 && <span className="badge soft">approx.</span>}
            </button>
          )}
        </div>
      </header>

      {!online && <div className="banner warn" role="status">You're offline. Showing what was loaded before. Reconnect to refresh.</div>}
      {geo.message && geo.state !== 'locating' && place?.source !== 'search' && (
        <div className="banner" role="status">
          {geo.message}
          <span className="banner-actions">
            {tab !== 'search' && <button className="link" onClick={() => setTab('search')}>Enter location</button>}
            {['timeout', 'unavailable'].includes(geo.state) && <button className="link" onClick={geo.locate}>Try again</button>}
          </span>
        </div>
      )}
      {geo.state === 'locating' && !place && <div className="banner soft" role="status">Finding your location…</div>}

      <main>
        {tab === 'search' && (
          <LocationSearch onPick={pick} onUseGps={geo.locate} geoBusy={geo.state === 'locating'} autoFocus={!place} />
        )}

        {tab !== 'search' && !place && (
          <section className="card empty">
            <h2>Assalamu alaikum</h2>
            <p>Allow location access to see masjids near you and today's prayer times, or search for a place.</p>
            <div className="actions">
              <button className="btn primary" onClick={geo.locate} disabled={geo.state === 'locating'}>Use my location</button>
              <button className="btn" onClick={() => setTab('search')}>Enter city manually</button>
            </div>
          </section>
        )}

        {tab === 'home' && place && (
          <>
            <p className="hello">{greeting()}{name ? <>, here's <strong>{name}</strong></> : ''}</p>
            <NextPrayer prayer={prayer.data} onElapsed={onPrayerElapsed} />
            <PrayerTable prayer={prayer.data} loading={prayer.loading} error={prayer.error} onRetry={prayer.retry} />
            <section>
              <div className="section-head">
                <h2>Nearest masjids</h2>
                {list.length > 3 && <button className="link" onClick={() => setTab('list')}>See all {list.length}</button>}
              </div>
              <MosqueResults req={mosques} radius={radius} items={list.slice(0, 3)} setRadius={setRadius}
                onShowOnMap={(m) => { setFocus(m); setTab('map') }} />
            </section>
          </>
        )}

        {tab === 'list' && place && (
          <>
            <RadiusChips radius={radius} setRadius={setRadius} />
            {mosques.loading ? <p className="muted small">Searching within {km(radius)}…</p> : mosques.data && <p className="muted small">{list.length} masjid{list.length === 1 ? '' : 's'} within {km(radius)}, nearest first</p>}
            <MosqueResults req={mosques} radius={radius} items={list} setRadius={setRadius}
              onShowOnMap={(m) => { setFocus(m); setTab('map') }} />
          </>
        )}

        {tab === 'map' && place && (
          <>
            <RadiusChips radius={radius} setRadius={(r) => { setFocus(null); setRadius(r) }} />
            {mosques.error && <ErrorCard message={mosques.error} onRetry={mosques.retry} />}
            <MapView place={place} radius={radius} mosques={list} focus={focus} />
          </>
        )}

        {mosques.data && tab !== 'search' && place && (
          <p className="attrib small muted">{mosques.data.attribution}</p>
        )}
      </main>

      <nav className="tabs" aria-label="Sections">
        {TABS.map(([id, label, icon]) => (
          <button key={id} className={tab === id ? 'on' : ''} onClick={() => setTab(id)} aria-current={tab === id ? 'page' : undefined}>
            <span aria-hidden>{icon}</span>{label}
          </button>
        ))}
      </nav>
    </div>
  )
}

function RadiusChips({ radius, setRadius }) {
  return (
    <div className="chips" role="radiogroup" aria-label="Search radius">
      {RADII.map((r) => (
        <button key={r} role="radio" aria-checked={r === radius} className={`chip${r === radius ? ' on' : ''}`} onClick={() => setRadius(r)}>
          {km(r)}
        </button>
      ))}
    </div>
  )
}

function MosqueResults({ req, radius, items, setRadius, onShowOnMap }) {
  if (req.error) return <ErrorCard message={req.error} onRetry={req.retry} />
  if (req.loading && !req.data) return [0, 1, 2].map((i) => <div key={i} className="card skeleton" style={{ height: 130 }} aria-busy="true" />)
  if (!req.data) return null
  if (!items.length) {
    const bigger = RADII.find((r) => r > radius)
    return (
      <section className="card empty">
        <p>No masjids are listed within {km(radius)} of this location in the map data.</p>
        {bigger && <button className="btn" onClick={() => setRadius(bigger)}>Search within {km(bigger)}</button>}
      </section>
    )
  }
  return (
    <div className={req.loading ? 'refreshing' : ''}>
      {items.map((m) => <MasjidCard key={m.id} m={m} onShowOnMap={onShowOnMap} />)}
    </div>
  )
}
