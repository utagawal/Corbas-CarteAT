// Fond de carte MapLibre (OpenStreetMap via OpenFreeMap) et couches des arrêtés.
import * as maplibregl from 'maplibre-gl';
import type { ExpressionSpecification, LngLatBoundsLike, Map as MLMap } from 'maplibre-gl';
import 'maplibre-gl/dist/maplibre-gl.css';
// MapLibre 6 charge son worker à côté de son propre module : une fois empaqueté par Vite,
// il faut lui indiquer explicitement l'URL du fichier copié dans dist/assets.
import workerUrl from 'maplibre-gl/dist/maplibre-gl-worker.mjs?url';
import { type Arrete, type Meta, lineColor } from './common';

export const SRC = 'arretes';

maplibregl.setWorkerUrl(new URL(workerUrl, location.href).href);

export function createMap(container: HTMLElement, meta: Meta): MLMap {
  const bounds = (meta.bounds ?? [4.87, 45.64, 4.94, 45.69]) as LngLatBoundsLike;
  const map = new maplibregl.Map({
    container,
    style: meta.map_style_url,
    bounds,
    fitBoundsOptions: { padding: 20 },
    maxZoom: 19,
    minZoom: 11,
    attributionControl: { compact: true },
    cooperativeGestures: false,
    locale: {
      'NavigationControl.ZoomIn': 'Zoomer',
      'NavigationControl.ZoomOut': 'Dézoomer',
      'NavigationControl.ResetBearing': "Réorienter vers le nord",
      'GeolocateControl.FindMyLocation': 'Me localiser',
      'GeolocateControl.LocationNotAvailable': 'Position indisponible',
      'FullscreenControl.Enter': 'Plein écran',
      'FullscreenControl.Exit': 'Quitter le plein écran',
      'AttributionControl.ToggleAttribution': 'Afficher les crédits',
    },
  });
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-right');
  map.addControl(
    new maplibregl.GeolocateControl({ positionOptions: { enableHighAccuracy: true }, trackUserLocation: false }),
    'top-right',
  );
  map.addControl(new maplibregl.ScaleControl({ unit: 'metric' }), 'bottom-left');
  map.on('load', () => {
    if (meta.boundary) addBoundary(map, meta.boundary);
    addArreteLayers(map);
  });
  return map;
}

function addBoundary(map: MLMap, boundary: GeoJSON.Polygon | GeoJSON.MultiPolygon) {
  // Masque grisé hors commune pour recentrer l'attention sur Corbas.
  const rings =
    boundary.type === 'Polygon' ? [boundary.coordinates[0]] : boundary.coordinates.map((p: GeoJSON.Position[][]) => p[0]);
  const world = [[-180, -85], [180, -85], [180, 85], [-180, 85], [-180, -85]];
  map.addSource('commune-mask', {
    type: 'geojson',
    data: { type: 'Feature', properties: {}, geometry: { type: 'Polygon', coordinates: [world, ...rings] } },
  });
  map.addLayer({ id: 'commune-mask', type: 'fill', source: 'commune-mask', paint: { 'fill-color': '#262532', 'fill-opacity': 0.12 } });
  map.addSource('commune', { type: 'geojson', data: { type: 'Feature', properties: {}, geometry: boundary } });
  map.addLayer({
    id: 'commune-line', type: 'line', source: 'commune',
    paint: { 'line-color': '#005b94', 'line-width': 2, 'line-dasharray': [3, 2], 'line-opacity': 0.8 },
  });
}

function addArreteLayers(map: MLMap) {
  map.addSource(SRC, { type: 'geojson', data: { type: 'FeatureCollection', features: [] }, promoteId: 'fid' });
  const isImpact: ExpressionSpecification = ['==', ['get', 'role'], 'impact'];
  const isLine: ExpressionSpecification = ['in', ['geometry-type'], ['literal', ['LineString', 'MultiLineString']]];
  map.addLayer({
    id: 'dev-line', type: 'line', source: SRC,
    filter: ['==', ['get', 'role'], 'deviation'],
    layout: { 'line-cap': 'round', 'line-join': 'round' },
    paint: { 'line-color': '#262532', 'line-width': 3, 'line-dasharray': [1.5, 1.5], 'line-opacity': ['case', ['boolean', ['get', 'selected'], false], 0.85, 0] },
  });
  map.addLayer({
    id: 'zone-fill', type: 'fill', source: SRC,
    filter: ['all', isImpact, ['in', ['geometry-type'], ['literal', ['Polygon', 'MultiPolygon']]]],
    paint: { 'fill-color': ['get', 'color'], 'fill-opacity': ['case', ['boolean', ['get', 'selected'], false], 0.45, 0.25] },
  });
  map.addLayer({
    id: 'impact-casing', type: 'line', source: SRC, filter: ['all', isImpact, isLine],
    layout: { 'line-cap': 'round', 'line-join': 'round' },
    paint: {
      'line-color': '#ffffff',
      'line-width': ['interpolate', ['linear'], ['zoom'], 12, 5, 16, 11, 19, 20],
      'line-opacity': ['case', ['boolean', ['get', 'dimmed'], false], 0.3, 1],
    },
  });
  map.addLayer({
    id: 'impact-line', type: 'line', source: SRC, filter: ['all', isImpact, isLine],
    layout: { 'line-cap': 'round', 'line-join': 'round' },
    paint: {
      'line-color': ['get', 'color'],
      'line-width': [
        'interpolate', ['linear'], ['zoom'],
        12, ['case', ['boolean', ['get', 'selected'], false], 5, 3],
        16, ['case', ['boolean', ['get', 'selected'], false], 10, 7],
        19, ['case', ['boolean', ['get', 'selected'], false], 18, 14],
      ],
      'line-opacity': ['case', ['boolean', ['get', 'dimmed'], false], 0.3, 0.95],
    },
  });
  map.addLayer({
    id: 'impact-point', type: 'circle', source: SRC,
    filter: ['all', isImpact, ['==', ['geometry-type'], 'Point']],
    paint: {
      'circle-radius': ['interpolate', ['linear'], ['zoom'], 12, 4, 17, 9],
      'circle-color': ['get', 'color'], 'circle-stroke-color': '#fff', 'circle-stroke-width': 2,
    },
  });
}

export function toFeatures(list: Arrete[], selected: string | null): GeoJSON.FeatureCollection {
  const features: GeoJSON.Feature[] = [];
  let n = 0;
  for (const a of list) {
    const color = lineColor(a);
    for (const f of a.geojson.features ?? []) {
      features.push({
        type: 'Feature',
        geometry: f.geometry,
        properties: {
          fid: n++, id: a.id, role: f.properties?.role ?? 'impact', color,
          selected: a.id === selected, dimmed: selected !== null && a.id !== selected,
        },
      });
    }
  }
  return { type: 'FeatureCollection', features };
}

export function boundsOf(fc: GeoJSON.FeatureCollection, onlyImpact = true): maplibregl.LngLatBounds | null {
  const b = new maplibregl.LngLatBounds();
  let any = false;
  const walk = (c: unknown): void => {
    if (Array.isArray(c) && typeof c[0] === 'number') {
      b.extend(c as [number, number]);
      any = true;
    } else if (Array.isArray(c)) c.forEach(walk);
  };
  for (const f of fc.features) {
    if (onlyImpact && f.properties?.role === 'deviation') continue;
    if (f.geometry && 'coordinates' in f.geometry) walk(f.geometry.coordinates);
  }
  return any ? b : null;
}

export { maplibregl };
