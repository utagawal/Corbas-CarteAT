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
  reseaux: { label: 'Travaux de réseaux', court: 'Réseaux', couleur: '#4f4dce', icone: Cable },
  voirie: { label: 'Travaux de voirie', court: 'Voirie', couleur: '#c43c00', icone: Construction },
  chantier: { label: 'Échafaudage, benne, nacelle', court: 'Chantier', couleur: '#8a5d00', icone: Forklift },
  demenagement: { label: 'Déménagement', court: 'Déménagement', couleur: '#00786f', icone: Truck },
  evenement: { label: 'Événement', court: 'Événement', couleur: '#c2185b', icone: PartyPopper },
  autre: { label: 'Autre', court: 'Autre', couleur: '#6e6e73', icone: Info },
};

// Ordre = gravité décroissante (la couleur d'un tracé est celle de sa mesure la plus contraignante).
// Couleurs choisies pour un contraste ≥ 4,5:1 sur fond blanc (utilisées aussi en texte).
// « couleur » : version foncée (texte, contraste AA) ; « carte » : version vive pour les tracés.
export const IMPACTS: Record<Impact, { label: string; court: string; couleur: string; carte: string; icone: IconNode }> = {
  route_barree: { label: 'Route barrée', court: 'Route barrée', couleur: '#d70015', carte: '#ff3b30', icone: OctagonX },
  circulation_alternee: { label: 'Circulation alternée ou rétrécie', court: 'Circulation alternée', couleur: '#b65300', carte: '#ff9500', icone: ArrowLeftRight },
  stationnement: { label: 'Stationnement neutralisé', court: 'Stationnement', couleur: '#005b94', carte: '#0a84ff', icone: CircleParking },
  trottoir: { label: 'Trottoir ou cheminement piéton modifié', court: 'Piétons', couleur: '#8944ab', carte: '#af52de', icone: Footprints },
  vitesse: { label: 'Vitesse limitée', court: 'Vitesse limitée', couleur: '#6e6e73', carte: '#8e8e93', icone: Gauge },
  deviation: { label: 'Déviation mise en place', court: 'Déviation', couleur: '#1d1d1f', carte: '#1d1d1f', icone: Signpost },
};
export const IMPACT_ORDER = Object.keys(IMPACTS) as Impact[];

export function mainImpact(a: Pick<Arrete, 'impacts'>): Impact | null {
  return IMPACT_ORDER.find((i) => a.impacts.includes(i) && i !== 'deviation') ?? null;
}

export function lineColor(a: Pick<Arrete, 'impacts'>): string {
  const m = mainImpact(a);
  return m ? IMPACTS[m].carte : '#8e8e93';
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
export const moisCourt = (d: Date) => fmtMois.format(d);
export const jourCourt = (d: Date) => fmtJour.format(d);

export function periodeTexte(a: Arrete): string {
  const deb = parseDate(a.date_debut);
  const fin = parseDate(a.date_fin);
  if (!deb) return 'Dates à préciser';
  if (a.jours.length > 1) return a.jours.map((j) => dateLongue(parseDate(j)!)).join(' et ');
  if (!fin || fin.getTime() === deb.getTime()) return `Le ${dateLongue(deb)}`;
  return `Du ${dateLongue(deb)} au ${dateLongue(fin)}`;
}

/** « 9 oct. », « 9 → 30 oct. », « 28 sept. → 25 oct. », « 2 et 3 oct. » */
export function periodeCourte(a: Pick<Arrete, 'date_debut' | 'date_fin' | 'jours'>): string {
  const deb = parseDate(a.date_debut);
  const fin = parseDate(a.date_fin) ?? deb;
  if (!deb || !fin) return 'Dates à préciser';
  const jm = (d: Date) => `${d.getDate()} ${moisCourt(d)}`;
  if (a.jours.length === 2) {
    const [x, y] = a.jours.map((j) => parseDate(j)!);
    return x.getMonth() === y.getMonth() ? `${x.getDate()} et ${jm(y)}` : `${jm(x)} et ${jm(y)}`;
  }
  if (a.jours.length > 2) return `${a.jours.length} jours à partir du ${jm(deb)}`;
  if (deb.getTime() === fin.getTime()) return `${jourCourt(deb)} ${jm(deb)}`;
  const sameMonth = deb.getMonth() === fin.getMonth() && deb.getFullYear() === fin.getFullYear();
  const yearFin = fin.getFullYear() !== today().getFullYear() ? ` ${fin.getFullYear()}` : '';
  return `${sameMonth ? deb.getDate() : jm(deb)} → ${jm(fin)}${yearFin}`;
}

/** Étiquette courte pour les listes : « En cours », « Demain », « Dans 5 j », « Terminé ». */
export function etatCourt(a: Arrete, t = today()): { texte: string; classe: string } {
  const deb = parseDate(a.date_debut);
  const fin = parseDate(a.date_fin) ?? deb;
  if (!deb || !fin) return { texte: '', classe: 'inconnu' };
  if (fin < t) return { texte: 'Terminé', classe: 'termine' };
  if (deb <= t) return { texte: 'En cours', classe: 'en-cours' };
  const d = daysBetween(t, deb);
  return { texte: d === 1 ? 'Demain' : `Dans ${d} j`, classe: 'a-venir' };
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
