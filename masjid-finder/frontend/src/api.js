// Thin client for the FastAPI backend. All third-party keys stay on the server.
const BASE = (import.meta.env.VITE_API_BASE || '').replace(/\/$/, '')

export class ApiError extends Error {
  constructor(message, status) {
    super(message)
    this.status = status
  }
}

async function get(path, params, { signal } = {}) {
  const qs = new URLSearchParams(Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== ''))
  if (!navigator.onLine) throw new ApiError("You're offline. Connect to the internet and try again.", 0)
  let res
  try {
    res = await fetch(`${BASE}${path}?${qs}`, { signal, headers: { Accept: 'application/json' } })
  } catch (e) {
    if (e.name === 'AbortError') throw e
    throw new ApiError('Could not reach the server. Check your connection and try again.', 0)
  }
  let body = null
  try { body = await res.json() } catch { /* non-JSON error page */ }
  if (!res.ok) throw new ApiError(body?.detail || `Server error (${res.status}).`, res.status)
  return body
}

export const api = {
  nearby: (latitude, longitude, radius, opts) => get('/api/mosques/nearby', { latitude, longitude, radius }, opts),
  address: (latitude, longitude, opts) => get('/api/mosques/address', { latitude, longitude }, opts),
  prayerTimes: (latitude, longitude, { country, method, asr } = {}, opts) =>
    get('/api/prayer-times', { latitude, longitude, country, method, asr }, opts),
  search: (q, opts) => get('/api/location/search', { q, limit: 6 }, opts),
  reverse: (latitude, longitude, opts) => get('/api/location/reverse', { latitude, longitude }, opts),
}
