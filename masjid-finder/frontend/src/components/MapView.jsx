import { useEffect } from 'react'
import { Circle, MapContainer, Marker, Popup, TileLayer, useMap } from 'react-leaflet'
import L from 'leaflet'
import 'leaflet/dist/leaflet.css'
import { distance } from '../format'

// divIcons avoid Leaflet's default PNG markers (which break under bundlers).
const mosqueIcon = (active) => L.divIcon({
  className: '', iconSize: [30, 30], iconAnchor: [15, 36], popupAnchor: [0, -32],
  html: `<div class="pin${active ? ' active' : ''}"><span>🕌</span></div>`,
})
const youIcon = L.divIcon({ className: '', iconSize: [18, 18], iconAnchor: [9, 9], html: '<div class="you-dot"></div>' })

function Fit({ center, radius, focus }) {
  const map = useMap()
  useEffect(() => {
    if (focus) map.setView([focus.latitude, focus.longitude], 17)
    else map.fitBounds(L.latLng(center).toBounds(radius * 2), { padding: [12, 12] })
  }, [map, center[0], center[1], radius, focus])
  useEffect(() => { setTimeout(() => map.invalidateSize(), 50) }, [map])
  return null
}

export default function MapView({ place, radius, mosques, focus }) {
  const center = [place.latitude, place.longitude]
  return (
    <div className="map-wrap">
      <MapContainer center={center} zoom={14} className="map" scrollWheelZoom>
        <TileLayer
          url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
          attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
          maxZoom={19}
        />
        <Circle center={center} radius={radius} pathOptions={{ color: '#0f5132', weight: 1, fillOpacity: 0.05 }} />
        <Marker position={center} icon={youIcon}>
          <Popup>{place.source === 'gps' ? 'You are here' : 'Selected location'}</Popup>
        </Marker>
        {mosques.map((m) => (
          <Marker key={m.id} position={[m.latitude, m.longitude]} icon={mosqueIcon(focus?.id === m.id)}>
            <Popup>
              <strong>{m.name}</strong><br />
              {distance(m.distance_meters)} away
              {m.address && <><br /><span className="small">{m.address}</span></>}
              {m.rating != null && <><br />★ {m.rating.toFixed(1)} {m.review_count != null && `(${m.review_count})`}</>}
              <br /><a href={m.directions.google} target="_blank" rel="noopener noreferrer">Directions →</a>
            </Popup>
          </Marker>
        ))}
        <Fit center={center} radius={radius} focus={focus} />
      </MapContainer>
    </div>
  )
}
