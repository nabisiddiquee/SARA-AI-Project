export const PRAYERS = [
  ['fajr', 'Fajr'], ['sunrise', 'Sunrise'], ['dhuhr', 'Dhuhr'],
  ['asr', 'Asr'], ['maghrib', 'Maghrib'], ['isha', 'Isha'],
]

export const RADII = [1000, 3000, 5000, 10000, 20000]

export const distance = (m) => (m < 1000 ? `${m} m` : `${(m / 1000).toFixed(m < 10000 ? 1 : 0)} km`)

export const km = (m) => `${m / 1000} km`

export function countdown(seconds) {
  if (seconds <= 0) return 'now'
  const h = Math.floor(seconds / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  const s = seconds % 60
  return h ? `${h}h ${String(m).padStart(2, '0')}m` : `${m}m ${String(s).padStart(2, '0')}s`
}

export function placeLabel(p) {
  if (!p) return ''
  const parts = [p.area, p.city, p.state, p.country].filter(Boolean)
  return [...new Set(parts)].join(', ') || p.display_name || `${p.latitude.toFixed(4)}, ${p.longitude.toFixed(4)}`
}

export function greeting(date = new Date()) {
  const h = date.getHours()
  return h < 12 ? 'Good morning' : h < 17 ? 'Good afternoon' : 'Good evening'
}
