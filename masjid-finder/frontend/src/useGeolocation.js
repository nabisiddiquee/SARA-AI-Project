import { useCallback, useEffect, useState } from 'react'

// Every way getting the device location can go, mapped to a message the user can act on.
export const GEO_STATES = {
  idle: null,
  locating: 'Finding your location…',
  unsupported: "This browser can't share your location. Enter your city or location manually.",
  insecure: 'Location needs a secure (https) connection. Enter your city or location manually.',
  denied: 'Location permission is off. Enter your city or location manually.',
  unavailable: "Couldn't get a GPS fix. Turn on location / GPS, or enter your city or location manually.",
  timeout: 'Getting your location took too long. Try again, or enter your city or location manually.',
  granted: null,
}

const CODE_TO_STATE = { 1: 'denied', 2: 'unavailable', 3: 'timeout' }

export function useGeolocation() {
  const [state, setState] = useState('idle')
  const [coords, setCoords] = useState(null) // { latitude, longitude, accuracy }

  const locate = useCallback(() => {
    if (!('geolocation' in navigator)) return setState('unsupported')
    if (!window.isSecureContext) return setState('insecure')
    setState('locating')
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        setCoords({ latitude: pos.coords.latitude, longitude: pos.coords.longitude, accuracy: pos.coords.accuracy })
        setState('granted')
      },
      (err) => setState(CODE_TO_STATE[err.code] || 'unavailable'),
      { enableHighAccuracy: true, timeout: 15000, maximumAge: 60000 },
    )
  }, [])

  // Follow permission changes made in browser settings while the app is open.
  useEffect(() => {
    let status
    navigator.permissions?.query({ name: 'geolocation' }).then((s) => {
      status = s
      if (s.state === 'denied') setState('denied')
      s.onchange = () => (s.state === 'granted' ? locate() : s.state === 'denied' && setState('denied'))
    }).catch(() => {})
    return () => { if (status) status.onchange = null }
  }, [locate])

  return { state, coords, locate, message: GEO_STATES[state] }
}

export function useOnline() {
  const [online, setOnline] = useState(navigator.onLine)
  useEffect(() => {
    const on = () => setOnline(true)
    const off = () => setOnline(false)
    window.addEventListener('online', on)
    window.addEventListener('offline', off)
    return () => { window.removeEventListener('online', on); window.removeEventListener('offline', off) }
  }, [])
  return online
}

// Per-device convenience only (last place, radius). Storage may be blocked: never required.
export const store = {
  get(key, fallback) {
    try { const v = localStorage.getItem(key); return v ? JSON.parse(v) : fallback } catch { return fallback }
  },
  set(key, value) {
    try { localStorage.setItem(key, JSON.stringify(value)) } catch { /* ignore */ }
  },
}
