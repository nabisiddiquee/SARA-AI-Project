import { useEffect, useRef, useState } from 'react'
import { api } from '../api'
import { placeLabel } from '../format'

const QUICK = ['Delhi', 'Mumbai', 'Hyderabad', 'Dubai', 'Mecca', 'Medina', 'London', 'New York']

export default function LocationSearch({ onPick, onUseGps, geoBusy, autoFocus }) {
  const [q, setQ] = useState('')
  const [results, setResults] = useState([])
  const [status, setStatus] = useState({ loading: false, error: null })
  const abort = useRef(null)

  async function run(query) {
    abort.current?.abort()
    const ctrl = new AbortController()
    abort.current = ctrl
    setStatus({ loading: true, error: null })
    try {
      const r = await api.search(query, { signal: ctrl.signal })
      setResults(r.results)
      setStatus({ loading: false, error: r.results.length ? null : `No places found for "${query}".` })
    } catch (e) {
      if (e.name !== 'AbortError') setStatus({ loading: false, error: e.message })
    }
  }

  useEffect(() => {
    const t = q.trim()
    if (t.length < 2) { setResults([]); setStatus({ loading: false, error: null }); return }
    const id = setTimeout(() => run(t), 500) // the geocoder allows ~1 request/second
    return () => clearTimeout(id)
  }, [q])

  return (
    <section className="search">
      <form className="search-box" onSubmit={(e) => { e.preventDefault(); if (q.trim().length >= 2) run(q.trim()) }}>
        <input
          type="search" value={q} onChange={(e) => setQ(e.target.value)} autoFocus={autoFocus}
          placeholder="City, area or address (e.g. Delhi, Dubai)" aria-label="Search for a location" enterKeyHint="search"
        />
      </form>
      <button className="btn gps" onClick={onUseGps} disabled={geoBusy}>
        {geoBusy ? 'Locating…' : '◎ Use my current location'}
      </button>

      {!q && (
        <div className="chips" aria-label="Popular places">
          {QUICK.map((c) => <button key={c} className="chip" onClick={() => setQ(c)}>{c}</button>)}
        </div>
      )}

      {status.loading && <p className="muted">Searching…</p>}
      {status.error && <p className="muted" role="alert">{status.error}</p>}
      <ul className="results">
        {results.map((r, i) => (
          <li key={`${r.latitude},${r.longitude},${i}`}>
            <button onClick={() => onPick({ ...r, source: 'search' })}>
              <strong>{r.name}</strong>
              <span className="small muted">{placeLabel(r)}</span>
            </button>
          </li>
        ))}
      </ul>
      <p className="small muted">Search by © OpenStreetMap contributors (Nominatim)</p>
    </section>
  )
}
