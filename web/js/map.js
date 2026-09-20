import { el, fmt, haversineKm } from './util.js';

const L = window.L;
const ESRI = 'https://server.arcgisonline.com/ArcGIS/rest/services/Canvas';
const TILES = {
  light: { base: `${ESRI}/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}`, labels: `${ESRI}/World_Light_Gray_Reference/MapServer/tile/{z}/{y}/{x}` },
  dark: { base: `${ESRI}/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}`, labels: `${ESRI}/World_Dark_Gray_Reference/MapServer/tile/{z}/{y}/{x}` },
};
const ATTRIBUTION = 'Tiles &copy; Esri &mdash; Esri, DeLorme, NAVTEQ &middot; Routing &copy; OSRM/OpenStreetMap';

const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

export class TripMap {
  constructor(elementId) {
    this.map = L.map(elementId, { zoomControl: false, preferCanvas: true, attributionControl: true }).setView([21.5, 79], 5);
    L.control.zoom({ position: 'bottomright' }).addTo(this.map);
    this.map.attributionControl.setPrefix(false);
    this.tile = null;
    this.routeLayers = [];
    this.markerLayer = L.layerGroup().addTo(this.map);
    this.dotLayer = L.layerGroup().addTo(this.map);
    this.cursor = null;
    this.km = [];
    this.geometry = [];
    this.setTheme(document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light');
  }

  setTheme(theme) {
    [this.tile, this.labelTile].forEach((t) => t && this.map.removeLayer(t));
    this.tile = L.tileLayer(TILES[theme].base, { attribution: ATTRIBUTION, maxZoom: 16, maxNativeZoom: 16 }).addTo(this.map);
    this.labelTile = L.tileLayer(TILES[theme].labels, { maxZoom: 16, maxNativeZoom: 16, pane: 'shadowPane' }).addTo(this.map);
    this.tile.bringToBack();
    if (this.lastDraw) this.redrawRoutes();
  }

  padding() {
    const wide = window.innerWidth > 860;
    return wide
      ? { paddingTopLeft: [470, 70], paddingBottomRight: [50, 50] }
      : { paddingTopLeft: [24, 70], paddingBottomRight: [24, Math.round(window.innerHeight * 0.34)] };
  }

  showRoutes(routes, activeIndex, onPick) {
    this.lastDraw = { routes, activeIndex, onPick };
    this.redrawRoutes();
    const bounds = L.latLngBounds(routes[activeIndex].geometry);
    this.map.fitBounds(bounds, this.padding());
  }

  redrawRoutes() {
    const { routes, activeIndex, onPick } = this.lastDraw;
    this.routeLayers.forEach((l) => this.map.removeLayer(l));
    this.routeLayers = [];
    const casing = document.documentElement.dataset.theme === 'dark' ? '#0B0D12' : '#FFFFFF';

    routes.forEach((route, i) => {
      if (i === activeIndex) return;
      const line = L.polyline(route.geometry, { color: css('--ink-3'), weight: 6, opacity: 0.7, lineCap: 'round' }).addTo(this.map);
      line.on('click', () => onPick(i));
      line.bindTooltip(`${route.label} · ${fmt.km(route.distance_km)} · ${fmt.mins(route.duration_mins)}`, { sticky: true });
      this.routeLayers.push(line);
    });
    const active = routes[activeIndex];
    this.routeLayers.push(L.polyline(active.geometry, { color: casing, weight: 11, opacity: 0.95, lineCap: 'round', lineJoin: 'round' }).addTo(this.map));
    this.routeLayers.push(L.polyline(active.geometry, { color: css('--route'), weight: 5.5, lineCap: 'round', lineJoin: 'round' }).addTo(this.map));

    this.geometry = active.geometry;
    this.km = [0];
    for (let i = 1; i < this.geometry.length; i++) this.km.push(this.km[i - 1] + haversineKm(this.geometry[i - 1], this.geometry[i]));
  }

  clearOverlays() {
    this.markerLayer.clearLayers();
    this.dotLayer.clearLayers();
    this.hideCursor();
  }

  drawEndpoints(start, end) {
    L.marker([start.lat, start.lng], { icon: L.divIcon({ className: '', html: '<div class="mk-start"></div>', iconSize: [22, 22], iconAnchor: [11, 11] }), keyboard: false, zIndexOffset: 500 }).addTo(this.markerLayer);
    L.marker([end.lat, end.lng], { icon: L.divIcon({ className: '', html: '<div class="mk-end"></div>', iconSize: [26, 26], iconAnchor: [13, 13] }), keyboard: false, zIndexOffset: 500 }).addTo(this.markerLayer);
  }

  drawChargers(chargers) {
    this.dotLayer.clearLayers();
    const up = css('--route'), down = css('--down');
    for (const c of chargers) {
      const marker = c.ok
        ? L.circleMarker([c.lat, c.lng], { radius: 3.5, weight: 0, fillColor: up, fillOpacity: 0.5 })
        : L.circleMarker([c.lat, c.lng], { radius: 4.5, weight: 2, color: down, fillOpacity: 0 });
      marker.addTo(this.dotLayer);
    }
  }

  drawStops(stops, onSelect) {
    this.stopMarkers = [];
    stops.forEach((s) => {
      if (s.backup) {
        const b = L.marker([s.backup.lat, s.backup.lng], {
          icon: L.divIcon({ className: '', html: `<div class="mk-backup"><span>B${s.index}</span></div>`, iconSize: [24, 24], iconAnchor: [12, 12] }),
          zIndexOffset: 600, keyboard: false,
        }).addTo(this.markerLayer);
        b.bindPopup(this.popup(`Backup for stop ${s.index}`, s.backup));
      }
      const m = L.marker([s.lat, s.lng], {
        icon: L.divIcon({ className: '', html: `<div class="mk-stop">${s.index}</div>`, iconSize: [34, 34], iconAnchor: [17, 17] }),
        zIndexOffset: 1000, keyboard: false,
      }).addTo(this.markerLayer);
      m.bindPopup(this.popup(`Stop ${s.index}`, s));
      m.on('click', () => onSelect && onSelect(s.index));
      this.stopMarkers.push(m);
    });
  }

  popup(title, c) {
    const box = el('div', {}, el('b', { text: title }), el('div', { text: c.name }));
    const status = !c.checked ? 'Not health-checked yet' : c.is_working ? 'Reported working' : 'Flagged as possibly down';
    box.append(el('div', { text: `${fmt.int(c.power_kw)} kW · ${status}` }));
    return box;
  }

  focusStop(index) {
    const m = this.stopMarkers && this.stopMarkers[index - 1];
    if (!m) return;
    this.map.flyTo(m.getLatLng(), Math.max(this.map.getZoom(), 10), { duration: 0.8 });
    m.openPopup();
  }

  fitAll() {
    if (!this.geometry.length) return;
    this.map.fitBounds(L.latLngBounds(this.geometry), this.padding());
  }

  showCursorAtKm(km) {
    if (!this.km.length) return;
    let lo = 0, hi = this.km.length - 1;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (this.km[mid] < km) lo = mid + 1; else hi = mid; }
    const pt = this.geometry[lo];
    if (!this.cursor) {
      this.cursor = L.marker(pt, { icon: L.divIcon({ className: '', html: '<div class="mk-cursor"></div>', iconSize: [16, 16], iconAnchor: [8, 8] }), interactive: false, zIndexOffset: 2000 }).addTo(this.map);
    } else this.cursor.setLatLng(pt);
  }

  hideCursor() {
    if (this.cursor) { this.map.removeLayer(this.cursor); this.cursor = null; }
  }

  invalidate() { this.map.invalidateSize(); }
}
