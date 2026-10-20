import { useEffect, useState } from 'react'
import { PRAYERS, countdown } from '../format'

export function NextPrayer({ prayer, onElapsed }) {
  const next = prayer?.next_prayer
  const [left, setLeft] = useState(null)

  useEffect(() => {
    if (!next) return setLeft(null)
    const target = new Date(next.datetime).getTime()
    let fired = false
    const tick = () => {
      const s = Math.max(0, Math.round((target - Date.now()) / 1000))
      setLeft(s)
      if (s === 0 && !fired) { fired = true; onElapsed?.() }
    }
    tick()
    const id = setInterval(tick, 1000)
    return () => clearInterval(id)
  }, [next?.datetime, onElapsed])

  if (!next) return null
  return (
    <section className="next card" aria-live="polite">
      <div className="next-label">Next prayer{next.is_tomorrow ? ' · tomorrow' : ''}</div>
      <div className="next-row">
        <span className="next-name">{next.name}</span>
        <span className="next-time">{next.time}</span>
      </div>
      <div className="next-count">in <strong>{left === null ? '…' : countdown(left)}</strong></div>
    </section>
  )
}

export function PrayerTable({ prayer, loading, error, onRetry }) {
  if (error) return <ErrorCard message={error} onRetry={onRetry} />
  if (loading && !prayer) return <section className="card skeleton" style={{ height: 230 }} aria-busy="true" />
  if (!prayer) return null
  const current = prayer.next_prayer && !prayer.next_prayer.is_tomorrow ? prayer.next_prayer.name.toLowerCase() : null
  return (
    <section className="card">
      <div className="card-head">
        <h2>Today's prayer times</h2>
        <span className="muted small">{new Date(prayer.date + 'T12:00').toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'short' })}</span>
      </div>
      <ul className="times">
        {PRAYERS.map(([key, label]) => (
          <li key={key} className={key === current ? 'is-next' : key === 'sunrise' ? 'is-sunrise' : ''}>
            <span>{label}</span>
            <span className="t">{prayer.times_12h[key]}</span>
          </li>
        ))}
      </ul>
      <p className="note">
        <span className="badge">Calculated</span> {prayer.method.name} · Asr: {prayer.method.asr.split(' ')[0]} · {prayer.timezone}.
        {' '}Masjids set their own Jamaat times, which can differ. Check with the masjid.
      </p>
    </section>
  )
}

export function ErrorCard({ message, onRetry }) {
  return (
    <section className="card error" role="alert">
      <p>{message}</p>
      {onRetry && <button className="btn" onClick={onRetry}>Try again</button>}
    </section>
  )
}
