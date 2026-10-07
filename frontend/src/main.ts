import './styles.css';
import '@fontsource/roboto/400.css';
import '@fontsource/roboto/500.css';
import '@fontsource/roboto/700.css';
import '@fontsource/lora/400-italic.css';
import { ArrowLeft, CalendarDays, Clock, ExternalLink, FileText, Map as MapIcon, MapPin, Signpost, Building2, X } from 'lucide';
import {
  type Arrete, type Categorie, type Impact, type Meta,
  CATEGORIES, IMPACTS, IMPACT_ORDER, api, dateCourte, daysBetween, etatTexte, h, icon, jourCourt, mainImpact,
  moisCourt, parseDate, periodeTexte, prefersReducedMotion, today,
} from './common';
import { SRC, boundsOf, createMap, maplibregl, toFeatures } from './mapbase';

type Periode = 'tous' | 'en_cours' | '7j' | '30j' | 'historique';
const PERIODES: [Periode, string][] = [
  ['tous', 'En cours et à venir'],
  ['en_cours', "Aujourd'hui"],
  ['7j', '7 jours'],
  ['30j', '30 jours'],
  ['historique', 'Historique'],
];

const state = {
  meta: null as Meta | null,
  actuels: [] as Arrete[],
  tous: null as Arrete[] | null,
  periode: 'tous' as Periode,
  categories: new Set<Categorie>(),
  impacts: new Set<Impact>(),
  q: '',
  selected: null as string | null,
};

const $ = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;
const app = $('app');
const liste = $('liste');
const detail = $('detail');
const compteur = $('compteur');
let map: maplibregl.Map;
const markers = new Map<string, maplibregl.Marker>();
const mobile = window.matchMedia('(max-width: 899px)');
let mapShown = !mobile.matches;

if (new URLSearchParams(location.search).get('embed') === '1') document.body.classList.add('embed');

// ------------------------------------------------------------------------- filtres
function norm(s: string) {
  return s.normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase();
}

function filtered(): Arrete[] {
  const src = state.periode === 'historique' ? state.tous ?? [] : state.actuels;
  const t = today();
  const q = norm(state.q.trim());
  return src.filter((a) => {
    if (state.categories.size && !state.categories.has(a.categorie)) return false;
    if (state.impacts.size && !a.impacts.some((i) => state.impacts.has(i))) return false;
    const deb = parseDate(a.date_debut);
    const fin = parseDate(a.date_fin) ?? deb;
    if (state.periode !== 'historique' && fin && fin < t) return false;
    if (state.periode === 'en_cours' && (!deb || deb > t)) return false;
    if (state.periode === '7j' && deb && daysBetween(t, deb) > 7) return false;
    if (state.periode === '30j' && deb && daysBetween(t, deb) > 30) return false;
    if (q) {
      const hay = norm([a.titre, a.lieu, a.intervenant, a.numero, ...a.deviation].join(' '));
      if (!q.split(/\s+/).every((w) => hay.includes(w))) return false;
    }
    return true;
  });
}

function buildFilters() {
  const per = $('f-periode');
  for (const [k, label] of PERIODES) {
    const id = `periode-${k}`;
    const input = h('input', { type: 'radio', name: 'periode', id, value: k, checked: k === state.periode });
    input.addEventListener('change', async () => {
      state.periode = k;
      if (k === 'historique' && !state.tous) {
        compteur.textContent = 'Chargement de l’historique…';
        state.tous = await api<Arrete[]>('api/arretes?periode=tous');
      }
      render();
    });
    per.append(h('span', { class: 'seg-item' }, input, h('label', { for: id }, label)));
  }
  const chip = <K extends string>(set: Set<K>, key: K, label: string, color: string, ic: Parameters<typeof icon>[0]) => {
    const b = h('button', { type: 'button', class: 'chip', 'aria-pressed': 'false' }, icon(ic, 16), h('span', {}, label));
    b.style.setProperty('--chip', color);
    b.addEventListener('click', () => {
      if (set.has(key)) set.delete(key);
      else set.add(key);
      b.setAttribute('aria-pressed', String(set.has(key)));
      render();
    });
    return b;
  };
  const fc = $('f-categories');
  for (const [k, c] of Object.entries(CATEGORIES) as [Categorie, (typeof CATEGORIES)[Categorie]][]) {
    fc.append(chip(state.categories, k, c.court, c.couleur, c.icone));
  }
  const fi = $('f-impacts');
  for (const k of IMPACT_ORDER) fi.append(chip(state.impacts, k, IMPACTS[k].court, IMPACTS[k].couleur, IMPACTS[k].icone));

  const search = $<HTMLInputElement>('recherche');
  let timer: number | undefined;
  search.addEventListener('input', () => {
    clearTimeout(timer);
    timer = window.setTimeout(() => {
      state.q = search.value;
      render();
    }, 200);
  });
  $('filtres').addEventListener('submit', (e) => e.preventDefault());
  $('reinit').addEventListener('click', () => {
    state.categories.clear();
    state.impacts.clear();
    state.q = '';
    search.value = '';
    document.querySelectorAll('.chip').forEach((c) => c.setAttribute('aria-pressed', 'false'));
    render();
  });
}

// ------------------------------------------------------------------------- liste
function groupe(a: Arrete, t: Date): string {
  const deb = parseDate(a.date_debut);
  const fin = parseDate(a.date_fin) ?? deb;
  if (!deb || !fin) return 'Dates à préciser';
  if (fin < t) return 'Terminés';
  if (deb <= t) return 'En cours';
  const d = daysBetween(t, deb);
  const finSemaine = 7 - ((t.getDay() + 6) % 7); // jours restants jusqu'à lundi prochain
  if (d < finSemaine) return 'Cette semaine';
  if (d < finSemaine + 7) return 'La semaine prochaine';
  const m = new Intl.DateTimeFormat('fr-FR', { month: 'long', year: 'numeric' }).format(deb);
  return `En ${m}`;
}

function sortList(list: Arrete[], t: Date): Arrete[] {
  const key = (a: Arrete) => {
    const fin = parseDate(a.date_fin) ?? parseDate(a.date_debut);
    const term = fin && fin < t;
    // en cours/à venir : par date de début ; terminés : du plus récent au plus ancien
    return term ? `z${String(99999999 - Number((a.date_fin ?? '').replace(/-/g, '')))}` : `a${a.date_debut ?? '9'}`;
  };
  return [...list].sort((x, y) => key(x).localeCompare(key(y)));
}

function item(a: Arrete): HTMLElement {
  const cat = CATEGORIES[a.categorie] ?? CATEGORIES.autre;
  const deb = parseDate(a.date_debut);
  const etat = etatTexte(a);
  const date = deb
    ? h('span', { class: 'cal', 'aria-hidden': 'true' },
        h('span', { class: 'cal-m' }, moisCourt(deb)), h('span', { class: 'cal-d' }, String(deb.getDate())),
        h('span', { class: 'cal-w' }, jourCourt(deb)))
    : h('span', { class: 'cal cal-unknown', 'aria-hidden': 'true' }, '?');
  const impacts = h('ul', { class: 'impacts-mini', 'aria-label': 'Conséquences' },
    ...a.impacts.filter((i) => i !== 'vitesse').map((i) => {
      const li = h('li', { title: IMPACTS[i].label }, icon(IMPACTS[i].icone, 14), h('span', { class: 'sr-only' }, IMPACTS[i].label));
      li.style.setProperty('--c', IMPACTS[i].couleur);
      return li;
    }));
  const btn = h('button', { type: 'button', class: 'item-btn', 'aria-describedby': `etat-${a.id}` },
    date,
    h('span', { class: 'item-body' },
      h('span', { class: 'item-cat' }, icon(cat.icone, 14), cat.court),
      h('span', { class: 'item-title' }, a.titre),
      a.lieu ? h('span', { class: 'item-lieu' }, a.lieu) : null,
      h('span', { class: `etat ${etat.classe}`, id: `etat-${a.id}` }, etat.texte),
    ),
  );
  btn.style.setProperty('--cat', cat.couleur);
  btn.addEventListener('click', () => select(a.id, { fly: true, fromList: true }));
  btn.addEventListener('mouseenter', () => highlight(a.id, true));
  btn.addEventListener('mouseleave', () => highlight(a.id, false));
  const li = h('li', { class: 'item', 'data-id': a.id }, btn, a.impacts.length ? impacts : null);
  return li;
}

function renderList(list: Arrete[]) {
  const t = today();
  liste.replaceChildren();
  if (!list.length) {
    liste.append(h('p', { class: 'empty' }, state.actuels.length || state.periode === 'historique'
      ? 'Aucun arrêté ne correspond à ces critères.'
      : 'Aucun travaux ni événement en cours ou annoncé pour le moment.'));
    return;
  }
  let current = '';
  let ol: HTMLOListElement | null = null;
  for (const a of sortList(list, t)) {
    const g = groupe(a, t);
    if (g !== current) {
      current = g;
      const hid = `g-${liste.children.length}`;
      ol = h('ol', { class: 'items', 'aria-labelledby': hid });
      liste.append(h('h2', { class: 'group-title', id: hid }, g), ol);
    }
    ol!.append(item(a));
  }
}

// ------------------------------------------------------------------------- détail
function renderDetail(a: Arrete) {
  const cat = CATEGORIES[a.categorie] ?? CATEGORIES.autre;
  const etat = etatTexte(a);
  const back = h('button', { type: 'button', class: 'back' }, icon(ArrowLeft, 18), 'Retour à la liste');
  back.addEventListener('click', () => select(null));
  const row = (ic: Parameters<typeof icon>[0], title: string, ...content: (Node | string | null)[]) =>
    h('div', { class: 'd-row' }, icon(ic, 20), h('div', {}, h('h3', {}, title), ...content));

  const quand = [h('p', {}, periodeTexte(a))];
  if (a.horaires.length) quand.push(h('p', { class: 'muted' }, icon(Clock, 14), ' ', a.horaires.join(', ')));
  if (a.remarques.length) quand.push(h('p', { class: 'muted' }, a.remarques.join(', ')));

  const conseq = h('ul', { class: 'd-impacts' },
    ...a.impacts.map((i) => {
      const li = h('li', {}, icon(IMPACTS[i].icone, 18), IMPACTS[i].label);
      li.style.setProperty('--c', IMPACTS[i].couleur);
      return li;
    }));

  const voir = h('button', { type: 'button', class: 'btn btn-outline only-mobile' }, icon(MapIcon, 18), 'Voir sur la carte');
  voir.addEventListener('click', () => {
    setView('carte');
    fly(a);
  });

  detail.replaceChildren(...[
    back,
    h('header', { class: 'd-head' },
      h('span', { class: 'd-cat' }, icon(cat.icone, 16), cat.label),
      h('h2', { id: 'detail-titre' }, a.titre),
      h('span', { class: `etat ${etat.classe}` }, etat.texte),
    ),
    row(CalendarDays, 'Quand ?', ...quand),
    row(MapPin, 'Où ?', h('p', {}, a.lieu || 'Voir le document officiel')),
    a.impacts.length ? h('div', { class: 'd-row d-row-wide' }, h('h3', {}, 'Conséquences'), conseq) : null,
    a.deviation.length ? row(Signpost, 'Itinéraire de déviation', h('p', {}, a.deviation.join(', '))) : null,
    a.intervenant ? row(Building2, 'Intervenant', h('p', {}, a.intervenant)) : null,
    a.note ? h('p', { class: 'd-note' }, a.note) : null,
    h('div', { class: 'd-actions' },
      voir,
      a.pdf_url
        ? h('a', { class: 'btn', href: a.pdf_url, target: '_blank', rel: 'noopener' },
            icon(FileText, 18), `Arrêté ${a.numero} (PDF)`, icon(ExternalLink, 14),
            h('span', { class: 'sr-only' }, ' – ouvre un nouvel onglet'))
        : null,
    ),
    h('p', { class: 'd-legal muted' }, 'Informations extraites automatiquement de l’arrêté officiel, qui seul fait foi.'),
  ].filter((n): n is HTMLElement => n !== null));
  detail.setAttribute('aria-labelledby', 'detail-titre');
}

// ------------------------------------------------------------------------- carte
function markerEl(a: Arrete): HTMLElement {
  const cat = CATEGORIES[a.categorie] ?? CATEGORIES.autre;
  const imp = mainImpact(a);
  const el = h('div', { class: `marker ${a.etat === 'a_venir' ? 'is-future' : ''}`, title: a.titre });
  el.style.setProperty('--cat', cat.couleur);
  if (imp) el.style.setProperty('--imp', IMPACTS[imp].couleur);
  el.append(icon(cat.icone, 18));
  el.addEventListener('click', (e) => {
    e.stopPropagation();
    select(a.id, { fly: false, fromMap: true });
  });
  return el;
}

function renderMap(list: Arrete[]) {
  if (!map || !map.getSource(SRC)) return;
  (map.getSource(SRC) as maplibregl.GeoJSONSource).setData(toFeatures(list, state.selected));
  const keep = new Set(list.map((a) => a.id));
  for (const [id, m] of markers) {
    if (!keep.has(id)) {
      m.remove();
      markers.delete(id);
    }
  }
  for (const a of list) {
    if (!a.centre) continue;
    let m = markers.get(a.id);
    if (!m) {
      m = new maplibregl.Marker({ element: markerEl(a), anchor: 'center' }).setLngLat(a.centre).addTo(map);
      markers.set(a.id, m);
    }
    m.getElement().classList.toggle('is-selected', a.id === state.selected);
    m.getElement().classList.toggle('is-dimmed', state.selected !== null && a.id !== state.selected);
  }
}

function highlight(id: string, on: boolean) {
  markers.get(id)?.getElement().classList.toggle('is-hover', on);
}

function fly(a: Arrete) {
  if (!map) return;
  const b = boundsOf(a.geojson);
  const opts = { padding: mobile.matches ? 60 : 120, maxZoom: 17, duration: prefersReducedMotion() ? 0 : 800 };
  if (b) map.fitBounds(b, opts);
  else if (a.centre) map.easeTo({ center: a.centre, zoom: 16, duration: opts.duration });
}

// ------------------------------------------------------------------------- sélection & vues
function findArrete(id: string | null): Arrete | undefined {
  if (!id) return undefined;
  return state.actuels.find((a) => a.id === id) ?? state.tous?.find((a) => a.id === id);
}

function select(id: string | null, o: { fly?: boolean; fromList?: boolean; fromMap?: boolean; silent?: boolean } = {}) {
  const prev = state.selected;
  state.selected = id;
  const a = findArrete(id);
  if (a) {
    renderDetail(a);
    detail.hidden = false;
    liste.hidden = true;
    $('filtres').hidden = true;
    compteur.hidden = true;
    if (o.fly) fly(a);
    if (o.fromMap && mobile.matches) showPreview(a);
    else hidePreview();
    if (!o.fromMap || !mobile.matches) detail.focus({ preventScroll: false });
    if (!o.silent) history.replaceState(null, '', `#at=${encodeURIComponent(a.id)}`);
  } else {
    state.selected = null;
    detail.hidden = true;
    liste.hidden = false;
    $('filtres').hidden = false;
    compteur.hidden = false;
    hidePreview();
    history.replaceState(null, '', location.pathname + location.search);
    const btn = prev ? liste.querySelector<HTMLButtonElement>(`[data-id="${CSS.escape(prev)}"] .item-btn`) : null;
    (btn ?? liste).focus();
  }
  renderMap(filtered());
}

function showPreview(a: Arrete) {
  let p = document.getElementById('apercu');
  if (!p) {
    p = h('div', { id: 'apercu', class: 'preview', role: 'dialog', 'aria-label': 'Aperçu' });
    $('carte-zone').append(p);
  }
  const etat = etatTexte(a);
  const close = h('button', { type: 'button', class: 'icon-btn', 'aria-label': 'Fermer l’aperçu' }, icon(X, 18));
  close.addEventListener('click', () => select(null));
  const more = h('button', { type: 'button', class: 'btn' }, 'Voir le détail');
  more.addEventListener('click', () => {
    setView('liste');
    detail.focus();
  });
  p.replaceChildren(
    close,
    h('strong', {}, a.titre),
    h('span', { class: `etat ${etat.classe}` }, etat.texte),
    h('span', { class: 'muted' }, periodeTexte(a)),
    more,
  );
  p.hidden = false;
}

function hidePreview() {
  const p = document.getElementById('apercu');
  if (p) p.hidden = true;
}

function setView(v: 'liste' | 'carte') {
  app.dataset.view = v;
  document.querySelectorAll<HTMLButtonElement>('.view-switch [role=tab]').forEach((b) => {
    const on = b.dataset.view === v;
    b.setAttribute('aria-selected', String(on));
    b.tabIndex = on ? 0 : -1;
  });
  if (v === 'carte') {
    requestAnimationFrame(() => {
      if (!map) return;
      map.resize();
      // La carte a été créée masquée (largeur nulle) : on recadre sur la commune au premier affichage.
      if (!mapShown && !state.selected && state.meta?.bounds) {
        map.fitBounds(state.meta.bounds as [number, number, number, number], { padding: 20, duration: 0 });
      }
      mapShown = true;
    });
  }
}

function buildViewSwitch() {
  const tabs = [...document.querySelectorAll<HTMLButtonElement>('.view-switch [role=tab]')];
  tabs.forEach((b, i) => {
    b.addEventListener('click', () => setView(b.dataset.view as 'liste' | 'carte'));
    b.addEventListener('keydown', (e) => {
      if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
        const n = tabs[(i + (e.key === 'ArrowRight' ? 1 : tabs.length - 1)) % tabs.length];
        n.focus();
        n.click();
      }
    });
  });
}

// ------------------------------------------------------------------------- rendu global
function render() {
  const list = filtered();
  const n = list.length;
  compteur.textContent = n === 0 ? 'Aucun arrêté' : `${n} arrêté${n > 1 ? 's' : ''}`;
  const nbF = state.categories.size + state.impacts.size;
  const badge = $('nb-filtres');
  badge.hidden = nbF === 0;
  badge.textContent = String(nbF);
  renderList(list);
  if (state.selected && !list.some((a) => a.id === state.selected)) select(null);
  else renderMap(list);
}

function buildLegend() {
  const lg = $('legende');
  const items = (['route_barree', 'circulation_alternee', 'stationnement', 'trottoir'] as Impact[]).map((i) => {
    const sw = h('span', { class: 'sw' });
    sw.style.background = IMPACTS[i].couleur;
    return h('li', {}, sw, IMPACTS[i].court);
  });
  const dev = h('li', {}, h('span', { class: 'sw sw-dash' }), 'Déviation');
  const futur = h('li', {}, h('span', { class: 'sw-marker is-future' }), 'À venir');
  const encours = h('li', {}, h('span', { class: 'sw-marker' }), 'En cours');
  lg.append(h('ul', {}, ...items, dev, encours, futur));
  (lg as HTMLDetailsElement).open = !mobile.matches;
}

async function init() {
  buildViewSwitch();
  buildFilters();
  buildLegend();
  try {
    const [meta, actuels] = await Promise.all([api<Meta>('api/meta'), api<Arrete[]>('api/arretes')]);
    state.meta = meta;
    state.actuels = actuels;
    if (meta.registre_url) $<HTMLAnchorElement>('lien-registre').href = meta.registre_url;
    if (meta.derniere_synchro) {
      const d = new Date(meta.derniere_synchro);
      $('maj').textContent = `Dernière mise à jour : ${dateCourte(d)} ${d.getFullYear()} à ${d.toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit' })}.`;
    }
    map = createMap($('carte'), meta);
    map.on('load', () => {
      render();
      const m = location.hash.match(/at=([^&]+)/);
      if (m) {
        const id = decodeURIComponent(m[1]);
        if (findArrete(id)) select(id, { fly: true, silent: true });
      }
    });
    for (const layer of ['impact-line', 'zone-fill', 'impact-point']) {
      map.on('click', layer, (e) => {
        const id = e.features?.[0]?.properties?.id as string | undefined;
        if (id) select(id, { fromMap: true });
      });
      map.on('mouseenter', layer, () => (map.getCanvas().style.cursor = 'pointer'));
      map.on('mouseleave', layer, () => (map.getCanvas().style.cursor = ''));
    }
    render();
  } catch (e) {
    compteur.textContent = 'Impossible de charger les données. Veuillez réessayer plus tard.';
    console.error(e);
  }
}

init();
