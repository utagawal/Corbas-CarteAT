// Éléments partagés entre la page publique et l'administration.
import {
  createElement,
  Cable,
  Construction,
  Forklift,
  Truck,
  PartyPopper,
  Info,
  OctagonX,
  ArrowLeftRight,
  CircleParking,
  Footprints,
  Gauge,
  Signpost,
  type IconNode,
} from 'lucide';

export type Categorie = 'reseaux' | 'voirie' | 'chantier' | 'demenagement' | 'evenement' | 'autre';
export type Impact = 'route_barree' | 'circulation_alternee' | 'stationnement' | 'trottoir' | 'vitesse' | 'deviation';
export type Etat = 'en_cours' | 'a_venir' | 'termine' | 'inconnu';

export interface Arrete {
  id: string;
  numero: string;
  titre: string;
  categorie: Categorie;
  impacts: Impact[];
  date_debut: string | null;
  date_fin: string | null;
  jours: string[];
  horaires: string[];
  remarques: string[];
  lieu: string;
  intervenant: string;
  deviation: string[];
  note: string;
  centre: [number, number] | null;
  geojson: GeoJSON.FeatureCollection;
  etat: Etat;
  pdf_url: string | null;
  date_publication: string | null;
}

export interface Meta {
  commune: string;
  boundary: GeoJSON.Polygon | GeoJSON.MultiPolygon | null;
  bounds: [number, number, number, number] | null;
  map_style_url: string;
  categories: Record<Categorie, string>;
  impacts: Record<Impact, string>;
  derniere_synchro: string | null;
  registre_url: string;
}

export const CATEGORIES: Record<Categorie, { label: string; court: string; couleur: string; icone: IconNode }> = {
  reseaux: { label: 'Travaux de réseaux', court: 'Réseaux', couleur: '#6a3d9a', icone: Cable },
  voirie: { label: 'Travaux de voirie', court: 'Voirie', couleur: '#b4410c', icone: Construction },
  chantier: { label: 'Échafaudage, benne, nacelle', court: 'Chantier', couleur: '#7a5a00', icone: Forklift },
  demenagement: { label: 'Déménagement', court: 'Déménagement', couleur: '#0f766e', icone: Truck },
  evenement: { label: 'Événement, manifestation', court: 'Événement', couleur: '#be185d', icone: PartyPopper },
  autre: { label: 'Autre', court: 'Autre', couleur: '#4b5563', icone: Info },
};

// Ordre = gravité décroissante (la couleur d'un tracé est celle de sa mesure la plus contraignante).
export const IMPACTS: Record<Impact, { label: string; court: string; couleur: string; icone: IconNode }> = {
  route_barree: { label: 'Route barrée', court: 'Route barrée', couleur: '#d62828', icone: OctagonX },
  circulation_alternee: { label: 'Circulation alternée ou rétrécie', court: 'Circulation alternée', couleur: '#e76f00', icone: ArrowLeftRight },
  stationnement: { label: 'Stationnement neutralisé', court: 'Stationnement', couleur: '#005b94', icone: CircleParking },
  trottoir: { label: 'Trottoir ou cheminement piéton modifié', court: 'Piétons', couleur: '#7b2cbf', icone: Footprints },
  vitesse: { label: 'Vitesse limitée', court: 'Vitesse limitée', couleur: '#6b7280', icone: Gauge },
  deviation: { label: 'Déviation mise en place', court: 'Déviation', couleur: '#262532', icone: Signpost },
};
export const IMPACT_ORDER = Object.keys(IMPACTS) as Impact[];

export function mainImpact(a: Pick<Arrete, 'impacts'>): Impact | null {
  return IMPACT_ORDER.find((i) => a.impacts.includes(i) && i !== 'deviation') ?? null;
}

export function lineColor(a: Pick<Arrete, 'impacts'>): string {
  const m = mainImpact(a);
  return m ? IMPACTS[m].couleur : '#4b5563';
}

export function icon(node: IconNode, size = 18, label?: string): SVGElement {
  const el = createElement(node, { width: size, height: size, 'stroke-width': 2 });
  if (label) {
    el.setAttribute('role', 'img');
    el.setAttribute('aria-label', label);
  } else {
    el.setAttribute('aria-hidden', 'true');
  }
  el.setAttribute('focusable', 'false');
  return el;
}

// --- Dates ---------------------------------------------------------------
const fmtLong = new Intl.DateTimeFormat('fr-FR', { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' });
const fmtCourt = new Intl.DateTimeFormat('fr-FR', { day: 'numeric', month: 'short' });
const fmtMois = new Intl.DateTimeFormat('fr-FR', { month: 'short' });
const fmtJour = new Intl.DateTimeFormat('fr-FR', { weekday: 'short' });

export function parseDate(s: string | null): Date | null {
  if (!s) return null;
  const [y, m, d] = s.split('-').map(Number);
  return new Date(y, m - 1, d);
}

export function today(): Date {
  const n = new Date();
  return new Date(n.getFullYear(), n.getMonth(), n.getDate());
}

export function daysBetween(a: Date, b: Date): number {
  return Math.round((b.getTime() - a.getTime()) / 86400000);
}

export const dateLongue = (d: Date) => fmtLong.format(d);
export const dateCourte = (d: Date) => fmtCourt.format(d).replace('.', '');
export const moisCourt = (d: Date) => fmtMois.format(d).replace('.', '');
export const jourCourt = (d: Date) => fmtJour.format(d).replace('.', '');

export function periodeTexte(a: Arrete): string {
  const deb = parseDate(a.date_debut);
  const fin = parseDate(a.date_fin);
  if (!deb) return 'Dates à préciser';
  if (a.jours.length > 1) return a.jours.map((j) => dateLongue(parseDate(j)!)).join(' et ');
  if (!fin || fin.getTime() === deb.getTime()) return `Le ${dateLongue(deb)}`;
  return `Du ${dateLongue(deb)} au ${dateLongue(fin)}`;
}

export function etatTexte(a: Arrete, t = today()): { texte: string; classe: string } {
  const deb = parseDate(a.date_debut);
  const fin = parseDate(a.date_fin) ?? deb;
  if (!deb || !fin) return { texte: 'Dates à préciser', classe: 'inconnu' };
  if (fin < t) return { texte: 'Terminé', classe: 'termine' };
  if (deb <= t) {
    const reste = daysBetween(t, fin);
    return {
      texte: reste === 0 ? "En cours, dernier jour aujourd'hui" : `En cours jusqu'au ${dateCourte(fin)}`,
      classe: 'en-cours',
    };
  }
  const dans = daysBetween(t, deb);
  return { texte: dans === 1 ? 'Commence demain' : `Commence dans ${dans} jours`, classe: 'a-venir' };
}

// --- Divers --------------------------------------------------------------
export function h<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  attrs: Record<string, string | boolean | undefined> = {},
  ...children: (Node | string | null | undefined | false)[]
): HTMLElementTagNameMap[K] {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === false) continue;
    if (k === 'class') el.className = String(v);
    else el.setAttribute(k, v === true ? '' : String(v));
  }
  for (const c of children) {
    if (c === null || c === undefined || c === false) continue;
    el.append(typeof c === 'string' ? document.createTextNode(c) : c);
  }
  return el;
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set('X-Requested-With', 'carteat');
  if (init.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
  const r = await fetch(path, { ...init, headers, credentials: 'same-origin' });
  if (!r.ok) {
    let msg = `Erreur ${r.status}`;
    try {
      const j = await r.json();
      if (j.detail) msg = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail);
    } catch {
      /* réponse non JSON */
    }
    throw Object.assign(new Error(msg), { status: r.status });
  }
  return r.json() as Promise<T>;
}

export function prefersReducedMotion(): boolean {
  return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}
