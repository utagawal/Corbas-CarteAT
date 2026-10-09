import './styles.css';
import { ArrowRight, ChevronLeft, ChevronRight, Clock, ExternalLink, FileText, Map as MapIcon, X } from 'lucide';
import {
  type Arrete, type Categorie, type Impact, type Meta,
  CATEGORIES, IMPACTS, IMPACT_ORDER, api, dateCourte, daysBetween, etatCourt, etatTexte, h, icon, mainImpact,
  parseDate, periodeCourte, periodeTexte, prefersReducedMotion, today,
} from './common';
import { SRC, boundsOf, createMap, maplibregl, toFeatures } from './mapbase';

type Periode = 'tous' | 'en_cours' | '7j' | '30j';
const PERIODES: [Periode, string][] = [
  ['tous', 'Tout'],
  ['en_cours', "Aujourd'hui"],
  ['7j', '7 jours'],
  ['30j', '30 jours'],
];

const state = {
  meta: null as Meta | null,
  actuels: [] as Arrete[],
  tous: null as Arrete[] | null,
  periode: 'tous' as Periode,
  historique: false,
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
const filtres = $('filtres');
let map: maplibregl.Map | undefined;
const markers = new Map<string, maplibregl.Marker>();
const mobile = window.matchMedia('(max-width: 899px)');
let mapShown = !mobile.matches;

if (new URLSearchParams(location.search).get('embed') === '1') document.body.classList.add('embed');

// ------------------------------------------------------------------------- données
async function loadTous(): Promise<boolean> {
  if (state.tous) return true;
  try {
    state.tous = await api<Arrete[]>('api/arretes?periode=tous');
    return true;
  } catch (e) {
    console.error(e);
    compteur.textContent = 'Impossible de charger les arrêtés terminés.';
    return false;
  }
}

// ------------------------------------------------------------------------- filtres
function norm(s: string) {
  return s.normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase();
}

function filtered(): Arrete[] {
  const src = state.historique ? state.tous ?? state.actuels : state.actuels;
  const t = today();
  const q = norm(state.q.trim());
  return src.filter((a) => {
    if (state.categories.size && !state.categories.has(a.categorie)) return false;
    if (state.impacts.size && !a.impacts.some((i) => state.impacts.has(i))) return false;
    const deb = parseDate(a.date_debut);
    const fin = parseDate(a.date_fin) ?? deb;
    const termine = fin !== null && fin < t;
    if (termine && !(state.historique && state.periode === 'tous')) return false;
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
    input.addEventListener('change', () => {
      state.periode = k;
      render();
    });
    per.append(h('span', { class: 'seg-item' }, input, h('label', { for: id }, label)));
  }

  const chip = <K extends string>(set: Set<K>, key: K, label: string, color: string) => {
    const b = h('button', { type: 'button', class: 'chip', 'aria-pressed': 'false' },
      h('span', { class: 'dot', 'aria-hidden': 'true' }), label);
    b.style.setProperty('--c', color);
    b.addEventListener('click', () => {
      if (set.has(key)) set.delete(key);
      else set.add(key);
      b.setAttribute('aria-pressed', String(set.has(key)));
      render();
    });
    return b;
  };
  for (const [k, c] of Object.entries(CATEGORIES) as [Categorie, (typeof CATEGORIES)[Categorie]][]) {
    $('f-categories').append(chip(state.categories, k, c.court, c.couleur));
  }
  for (const k of IMPACT_ORDER) $('f-impacts').append(chip(state.impacts, k, IMPACTS[k].court, IMPACTS[k].couleur));

  const toggle = $<HTMLButtonElement>('btn-filtres');
  const panel = $('plus-filtres');
  toggle.addEventListener('click', () => {
    panel.hidden = !panel.hidden;
    toggle.setAttribute('aria-expanded', String(!panel.hidden));
  });

  const hist = $<HTMLInputElement>('f-historique');
  hist.addEventListener('change', async () => {
    if (hist.checked && !(await loadTous())) {
      hist.checked = false;
      return;
    }
    state.historique = hist.checked;
    render();
  });

  const search = $<HTMLInputElement>('recherche');
  let timer: number | undefined;
  search.addEventListener('input', () => {
    clearTimeout(timer);
    timer = window.setTimeout(() => {
      state.q = search.value;
      render();
    }, 150);
  });
  filtres.addEventListener('submit', (e) => e.preventDefault());
  $('reinit').addEventListener('click', () => {
    state.categories.clear();
    state.impacts.clear();
    state.historique = false;
    state.q = '';
    search.value = '';
    hist.checked = false;
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
  if (d < finSemaine + 7) return 'Semaine prochaine';
  const m = new Intl.DateTimeFormat('fr-FR', { month: 'long', year: 'numeric' }).format(deb);
  return m[0].toUpperCase() + m.slice(1);
}

function sortList(list: Arrete[], t: Date): Arrete[] {
  const key = (a: Arrete) => {
    const fin = parseDate(a.date_fin) ?? parseDate(a.date_debut);
    // en cours / à venir : par date de début ; terminés : du plus récent au plus ancien
    if (fin && fin < t) return `z${String(99999999 - Number((a.date_fin ?? '').replace(/-/g, '')))}`;
    return `a${a.date_debut ?? '9'}${a.date_fin ?? ''}`;
  };
  return [...list].sort((x, y) => key(x).localeCompare(key(y)));
}

function row(a: Arrete): HTMLElement {
  const cat = CATEGORIES[a.categorie] ?? CATEGORIES.autre;
  const imp = mainImpact(a);
  const etat = etatCourt(a);
  const ic = h('span', { class: `row-icon ${etat.classe}`, 'aria-hidden': 'true' }, icon(cat.icone, 18));
  ic.style.setProperty('--cat', cat.couleur);
  const impact = imp ? h('span', { class: 'row-impact' }, IMPACTS[imp].court) : null;
  impact?.style.setProperty('--c', IMPACTS[imp!].couleur);
  const btn = h('button', { type: 'button', class: 'row-btn' },
    ic,
    h('span', { class: 'row-main' },
      h('span', { class: 'row-title' }, a.titre),
      a.lieu ? h('span', { class: 'row-sub' }, a.lieu) : null,
      h('span', { class: 'row-meta' }, impact, impact ? h('span', { 'aria-hidden': 'true' }, ' · ') : null,
        h('span', {}, periodeCourte(a))),
    ),
    // L'état (en cours, à venir…) est déjà donné par le titre du groupe : pas de pastille ici.
    h('span', { class: 'row-trail' }, icon(ChevronRight, 16)),
  );
  btn.addEventListener('click', () => select(a.id, { fly: true }));
  btn.addEventListener('mouseenter', () => highlight(a.id, true));
  btn.addEventListener('mouseleave', () => highlight(a.id, false));
  btn.addEventListener('focus', () => highlight(a.id, true));
  btn.addEventListener('blur', () => highlight(a.id, false));
  return h('li', { class: 'row', 'data-id': a.id }, btn);
}

function renderList(list: Arrete[]) {
  const t = today();
  liste.replaceChildren();
  if (!list.length) {
    const vide = !state.actuels.length && !state.historique;
    liste.append(h('div', { class: 'empty' },
      h('p', { class: 'empty-title' }, vide ? 'Rien à signaler' : 'Aucun résultat'),
      h('p', {}, vide
        ? 'Aucun travaux ni événement en cours ou annoncé sur la voie publique.'
        : 'Essayez une autre période ou retirez des filtres.')));
    return;
  }
  const groups = new Map<string, Arrete[]>();
  for (const a of sortList(list, t)) {
    const g = groupe(a, t);
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g)!.push(a);
  }
  let i = 0;
  for (const [g, items] of groups) {
    const hid = `g-${i++}`;
    liste.append(
      h('h2', { class: 'group-title', id: hid }, g, h('span', { class: 'group-count' }, String(items.length))),
      h('ul', { class: 'group', 'aria-labelledby': hid }, ...items.map(row)),
    );
  }
}

// ------------------------------------------------------------------------- détail
function renderDetail(a: Arrete) {
  const cat = CATEGORIES[a.categorie] ?? CATEGORIES.autre;
  const etat = etatTexte(a);
  const back = h('button', { type: 'button', class: 'back' }, icon(ChevronLeft, 20), 'Liste');
  back.addEventListener('click', () => select(null));

  const item = (label: string, ...content: (Node | string | null)[]) =>
    h('div', { class: 'cell' }, h('dt', {}, label), h('dd', {}, ...content));


  const conseq = a.impacts.length
    ? h('ul', { class: 'impacts' }, ...a.impacts.map((i) => {
        const li = h('li', {}, h('span', { class: 'dot', 'aria-hidden': 'true' }), IMPACTS[i].label);
        li.style.setProperty('--c', IMPACTS[i].couleur);
        return li;
      }))
    : null;

  const voir = h('button', { type: 'button', class: 'btn btn-tinted only-mobile' }, icon(MapIcon, 18), 'Voir sur la carte');
  voir.addEventListener('click', () => {
    setView('carte');
    fly(a);
  });

  const ic = h('span', { class: 'd-icon', 'aria-hidden': 'true' }, icon(cat.icone, 24));
  ic.style.setProperty('--cat', cat.couleur);

  detail.replaceChildren(...[
    back,
    h('header', { class: 'd-head' },
      ic,
      h('p', { class: 'd-cat' }, cat.label),
      h('h2', { id: 'detail-titre' }, a.titre),
      h('span', { class: `pill ${etat.classe}` }, etat.texte),
    ),
    quandBlock(a),
    h('dl', { class: 'cells' },
      item('Où', a.lieu || 'Voir le document officiel'),
      conseq
        ? item('Mesures autorisées', conseq,
            h('p', { class: 'cell-hint' }, 'Appliquées seulement lorsque l’intervenant est sur place, pas forcément pendant toute la période.'))
        : null,
      a.deviation.length ? item('Déviation', a.deviation.join(', ')) : null,
      a.intervenant ? item('Intervenant', a.intervenant) : null,
    ),
    a.note ? h('p', { class: 'd-note' }, a.note) : null,
    h('div', { class: 'd-actions' },
      a.pdf_url
        ? h('a', { class: 'btn', href: a.pdf_url, target: '_blank', rel: 'noopener' },
            icon(FileText, 18), `Arrêté ${a.numero}`, icon(ExternalLink, 14),
            h('span', { class: 'sr-only' }, ' (PDF, nouvel onglet)'))
        : null,
      voir,
    ),
    h('p', { class: 'd-legal' }, 'Informations extraites automatiquement de l’arrêté officiel, qui seul fait foi.'),
  ].filter((n): n is HTMLElement => n !== null));
  detail.setAttribute('aria-labelledby', 'detail-titre');
}

// ------------------------------------------------------------------------- « Quand » (façon Calendrier)
const fmtJourSem = new Intl.DateTimeFormat('fr-FR', { weekday: 'short' });
const fmtMoisLong = new Intl.DateTimeFormat('fr-FR', { month: 'short' });

/** Page de calendrier : bandeau (jour de la semaine), numéro du jour, mois. */
function calTile(d: Date, horaire?: string): HTMLElement {
  return h('div', { class: 'cal-tile' },
    h('span', { class: 'cal-top' }, fmtJourSem.format(d).replace('.', '')),
    h('span', { class: 'cal-day' }, String(d.getDate())),
    h('span', { class: 'cal-month' }, fmtMoisLong.format(d).replace('.', '')),
    horaire ? h('span', { class: 'cal-hours' }, horaire) : null,
  );
}

/** « 7h00–17h00 » → « 7h–17h », « 8h30–12h00 » → « 8h30–12h ». */
function heureCourte(s: string): string {
  return s.replace(/(\d{1,2})h00\b/g, '$1h');
}

function plural(n: number, mot: string) {
  return `${n} ${mot}${n > 1 ? 's' : ''}`;
}

function quandBlock(a: Arrete): HTMLElement {
  const deb = parseDate(a.date_debut);
  const fin = parseDate(a.date_fin) ?? deb;
  const box = h('section', { class: 'when', 'aria-labelledby': 'when-title' },
    h('h3', { class: 'when-title', id: 'when-title' }, 'Période autorisée'));
  if (!deb || !fin) {
    box.append(h('p', { class: 'when-text' }, 'Dates à préciser : voir le document officiel.'));
    return box;
  }
  const t = today();
  // Texte complet pour les lecteurs d'écran ; le visuel est décoratif.
  box.append(h('p', { class: 'sr-only' }, periodeTexte(a) + (a.horaires.length ? `, ${a.horaires.join(', ')}` : '')));
  const visual = h('div', { class: 'when-visual', 'aria-hidden': 'true' });
  box.append(visual);

  let horairesAffiches = false;
  if (a.jours.length > 1) {
    // Jours distincts (ex. déménagement vendredi soir et samedi) : une page par jour,
    // avec l'horaire correspondant quand l'arrêté en donne un par jour.
    const parJour = a.horaires.length === a.jours.length;
    horairesAffiches = parJour;
    visual.append(h('div', { class: 'cal-row' },
      ...a.jours.map((j, i) => calTile(parseDate(j)!, parJour ? heureCourte(a.horaires[i]) : undefined))));
  } else if (deb.getTime() === fin.getTime()) {
    visual.append(h('div', { class: 'cal-row' }, calTile(deb)));
  } else {
    visual.append(...periodeVisuelle(deb, fin, t));
  }
  if (a.jours.length > 1 || deb.getTime() === fin.getTime()) {
    // Jours isolés : simple repère temporel, sans barre de progression.
    const [etat, label] = deb > t
      ? ['a-venir', daysBetween(t, deb) === 1 ? 'Demain' : `Dans ${plural(daysBetween(t, deb), 'jour')}`]
      : fin < t ? ['termine', 'Terminé'] : ['en-cours', deb.getTime() === fin.getTime() ? "Aujourd'hui" : 'En cours'];
    visual.append(h('div', { class: `progress ${etat}` }, h('span', { class: 'progress-label' }, label)));
  }

  let notes = [...(horairesAffiches ? [] : a.horaires), ...a.remarques].map(heureCourte);
  if (notes.includes('du lundi au vendredi')) notes = notes.filter((n) => n !== 'hors week-end');
  if (notes.length) {
    visual.append(h('ul', { class: 'hours' }, ...notes.map((n) => h('li', {}, icon(Clock, 14), n))));
  }
  // Un arrêté autorise une intervention pendant une période ; il ne dit pas que le chantier l'occupe en entier.
  box.append(h('p', { class: 'when-hint' },
    'L’arrêté autorise l’intervention pendant cette période : les travaux n’ont pas forcément lieu tous les jours, ni toute la journée.'));
  return box;
}

function periodeVisuelle(deb: Date, fin: Date, t: Date): HTMLElement[] {
  const total = daysBetween(deb, fin) + 1;
  const row = h('div', { class: 'cal-row' },
    calTile(deb),
    h('div', { class: 'cal-span' }, icon(ArrowRight, 18), h('span', {}, plural(total, 'jour'))),
    calTile(fin));

  // Barre de progression : où en est-on aujourd'hui ?
  let pct = 0;
  let label: string;
  let etat = 'a-venir';
  if (fin < t) {
    pct = 100;
    etat = 'termine';
    label = `Terminé depuis ${plural(daysBetween(fin, t), 'jour')}`;
  } else if (deb <= t) {
    const jour = daysBetween(deb, t) + 1;
    const reste = daysBetween(t, fin);
    pct = Math.max(4, Math.round((jour / total) * 100));
    etat = 'en-cours';
    label = `Jour ${jour} sur ${total} · ${reste === 0 ? "dernier jour aujourd'hui" : `fin dans ${plural(reste, 'jour')}`}`;
  } else {
    const dans = daysBetween(t, deb);
    label = dans === 1 ? 'Commence demain' : `Commence dans ${plural(dans, 'jour')}`;
  }
  const fill = h('span', { class: 'progress-fill' });
  fill.style.width = `${pct}%`;
  return [row, h('div', { class: `progress ${etat}` },
    h('span', { class: 'progress-track' }, fill),
    h('span', { class: 'progress-label' }, label))];
}

// ------------------------------------------------------------------------- carte
function markerEl(a: Arrete): HTMLElement {
  const cat = CATEGORIES[a.categorie] ?? CATEGORIES.autre;
  const el = h('div', { class: `marker ${etatCourt(a).classe}`, title: a.titre });
  el.style.setProperty('--cat', cat.couleur);
  el.append(icon(cat.icone, 16));
  el.addEventListener('click', (e) => {
    e.stopPropagation();
    select(a.id, { fromMap: true });
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
  // Sur grand écran, le panneau flottant masque la gauche de la carte : on décale le cadrage.
  const padding = mobile.matches ? 48 : { top: 80, bottom: 80, left: 460, right: 80 };
  const duration = prefersReducedMotion() ? 0 : 700;
  if (b) map.fitBounds(b, { padding, maxZoom: 17, duration });
  else if (a.centre) map.easeTo({ center: a.centre, zoom: 16, duration });
}

// ------------------------------------------------------------------------- sélection & vues
function findArrete(id: string | null): Arrete | undefined {
  if (!id) return undefined;
  return state.actuels.find((a) => a.id === id) ?? state.tous?.find((a) => a.id === id);
}

function select(id: string | null, o: { fly?: boolean; fromMap?: boolean; silent?: boolean } = {}) {
  const prev = state.selected;
  const a = findArrete(id);
  state.selected = a ? a.id : null;
  app.classList.toggle('has-detail', !!a);
  if (a) {
    renderDetail(a);
    detail.hidden = false;
    liste.hidden = true;
    filtres.hidden = true;
    if (o.fly) fly(a);
    if (o.fromMap && mobile.matches) showPreview(a);
    else hidePreview();
    if (!(o.fromMap && mobile.matches)) {
      // Remonter le seul panneau (scrollIntoView ferait défiler toute la page sur mobile).
      $('panneau').scrollTop = 0;
      detail.focus({ preventScroll: true });
    }
    if (!o.silent) history.replaceState(null, '', `#at=${encodeURIComponent(a.id)}`);
  } else {
    detail.hidden = true;
    liste.hidden = false;
    filtres.hidden = false;
    hidePreview();
    history.replaceState(null, '', location.pathname + location.search);
    const btn = prev ? liste.querySelector<HTMLButtonElement>(`[data-id="${CSS.escape(prev)}"] .row-btn`) : null;
    (btn ?? liste).focus();
  }
  renderMap(filtered());
}

function showPreview(a: Arrete) {
  let p = document.getElementById('apercu');
  if (!p) {
    p = h('div', { id: 'apercu', class: 'preview', role: 'dialog', 'aria-label': 'Aperçu de l’arrêté' });
    $('carte-zone').append(p);
  }
  const etat = etatCourt(a);
  const close = h('button', { type: 'button', class: 'icon-btn', 'aria-label': 'Fermer' }, icon(X, 16));
  close.addEventListener('click', () => select(null));
  const more = h('button', { type: 'button', class: 'btn' }, 'Voir le détail');
  more.addEventListener('click', () => {
    setView('liste');
    detail.focus();
  });
  p.replaceChildren(
    close,
    h('p', { class: 'preview-title' }, a.titre),
    h('p', { class: 'preview-meta' }, etat.texte ? h('span', { class: `pill ${etat.classe}` }, etat.texte) : null, ' ', periodeCourte(a)),
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
        map.fitBounds(state.meta.bounds as [number, number, number, number], { padding: 16, duration: 0 });
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
  mobile.addEventListener('change', () => map?.resize());
}

// ------------------------------------------------------------------------- rendu global
function render() {
  const list = filtered();
  const n = list.length;
  compteur.textContent = n === 0 ? 'Aucun arrêté' : `${n} arrêté${n > 1 ? 's' : ''}`;
  const nbF = state.categories.size + state.impacts.size + (state.historique ? 1 : 0);
  const badge = $('nb-filtres');
  badge.hidden = nbF === 0;
  badge.textContent = String(nbF);
  renderList(list);
  if (state.selected && !list.some((a) => a.id === state.selected)) select(null);
  else renderMap(list);
}

function buildLegend() {
  const lg = $('legende');
  const lines = (['route_barree', 'circulation_alternee', 'stationnement', 'trottoir'] as Impact[]).map((i) => {
    const sw = h('span', { class: 'sw' });
    sw.style.background = IMPACTS[i].carte;
    return h('li', {}, sw, IMPACTS[i].court);
  });
  lg.append(h('ul', {},
    ...lines,
    h('li', {}, h('span', { class: 'sw sw-dash' }), 'Déviation'),
    h('li', {}, h('span', { class: 'sw-dot en-cours' }), 'En cours'),
    h('li', {}, h('span', { class: 'sw-dot a-venir' }), 'À venir'),
  ));
  (lg as HTMLDetailsElement).open = !mobile.matches;
}

async function openFromHash() {
  const m = location.hash.match(/at=([^&]+)/);
  if (!m) return;
  const id = decodeURIComponent(m[1]);
  if (!findArrete(id) && (await loadTous()) && state.tous?.some((a) => a.id === id)) {
    // Lien vers un arrêté terminé : on bascule sur l'historique pour pouvoir l'afficher.
    state.historique = true;
    $<HTMLInputElement>('f-historique').checked = true;
    render();
  }
  if (findArrete(id)) select(id, { fly: true, silent: true });
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
      $('maj').textContent = `Mis à jour le ${dateCourte(d)} ${d.getFullYear()} à ${d.toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit' })}.`;
    }
    render();
    const m = createMap($('carte'), meta);
    map = m;
    m.on('load', () => {
      render();
      void openFromHash();
    });
    for (const layer of ['impact-line', 'zone-fill', 'impact-point']) {
      m.on('click', layer, (e) => {
        const id = e.features?.[0]?.properties?.id as string | undefined;
        if (id) select(id, { fromMap: true });
      });
      m.on('mouseenter', layer, () => (m.getCanvas().style.cursor = 'pointer'));
      m.on('mouseleave', layer, () => (m.getCanvas().style.cursor = ''));
    }
  } catch (e) {
    compteur.textContent = 'Impossible de charger les données. Veuillez réessayer plus tard.';
    console.error(e);
  }
}

void init();
