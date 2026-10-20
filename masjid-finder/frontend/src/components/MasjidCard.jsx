import { useState } from 'react'
import { api } from '../api'
import { distance } from '../format'

export default function MasjidCard({ m, onShowOnMap }) {
  const [approx, setApprox] = useState({ state: 'idle', text: null })

  async function loadAddress() {
    setApprox({ state: 'loading', text: null })
    try {
      const r = await api.address(m.latitude, m.longitude)
      setApprox({ state: r.address ? 'done' : 'none', text: r.address })
    } catch {
      setApprox({ state: 'error', text: null })
    }
  }

  return (
    <article className="masjid card">
      <div className="masjid-top">
        <h3>{m.name}</h3>
        <span className="dist">{distance(m.distance_meters)}</span>
      </div>

      {m.address ? (
        <p className="addr">{m.address}</p>
      ) : approx.state === 'done' ? (
        <p className="addr"><span className="badge soft">Approx.</span> {approx.text}</p>
      ) : (
        <p className="addr muted">
          Address not listed.{' '}
          {approx.state === 'loading' ? 'Looking up…'
            : approx.state === 'none' ? 'No nearby address found.'
            : <button className="link" onClick={loadAddress}>{approx.state === 'error' ? 'Retry lookup' : 'Show approximate address'}</button>}
        </p>
      )}

      {(m.rating != null || m.open_now != null || m.denomination) && (
        <div className="facts">
          {m.rating != null && <span>★ {m.rating.toFixed(1)}{m.review_count != null && <span className="muted"> ({m.review_count})</span>}</span>}
          {m.open_now != null && <span className={m.open_now ? 'open' : 'closed'}>{m.open_now ? 'Open now' : 'Closed now'}</span>}
          {m.denomination && <span className="muted">{m.denomination}</span>}
        </div>
      )}

      {m.jamaat_times && (
        <p className="jamaat"><span className="badge gold">Masjid Jamaat times</span> {m.jamaat_times.schedule}</p>
      )}

      <div className="actions">
        <a className="btn primary" href={m.directions.google} target="_blank" rel="noopener noreferrer">Directions</a>
        {onShowOnMap && <button className="btn" onClick={() => onShowOnMap(m)}>Map</button>}
        {m.phone && <a className="btn" href={`tel:${m.phone.replace(/[^\d+]/g, '')}`}>Call</a>}
        {m.website && <a className="btn" href={m.website} target="_blank" rel="noopener noreferrer">Website</a>}
      </div>
      <div className="alt-links small muted">
        Open in <a href={m.directions.apple} target="_blank" rel="noopener noreferrer">Apple Maps</a>
        {' · '}<a href={m.directions.osm} target="_blank" rel="noopener noreferrer">OpenStreetMap</a>
        {m.source_url && <>{' · '}<a href={m.source_url} target="_blank" rel="noopener noreferrer">
          Source: {m.source === 'google_places' ? 'Google' : 'OSM'}</a></>}
      </div>
    </article>
  )
}
