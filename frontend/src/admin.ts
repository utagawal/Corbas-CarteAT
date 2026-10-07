import './styles.css';
import './admin.css';
import { Check, Eraser, FileText, MapPin, MousePointerClick, RefreshCw, Spline, Trash2, Undo2, X } from 'lucide';
import { type Arrete, type Categorie, type Impact, type Meta, CATEGORIES, IMPACTS, IMPACT_ORDER, api, h, icon } from './common';
import { boundsOf, createMap, maplibregl } from './mapbase';

interface AdminArrete extends Arrete {
  statut: 'publie' | 'a_verifier' | 'masque';
  motif_verification: string;
  qualite_geo: string;
  modifie_manuellement: boolean;
  erreur: string;
  objet_registre: string;
  texte?: string;
  extraction?: Record<string, unknown>;
}

const STATUTS: Record<string, string> = { a_verifier: 'À vérifier', publie: 'Publiés', masque: 'Masqués', '': 'Tous' };
const $ = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;

let meta: Meta;
let rows: AdminArrete[] = [];
let tab = 'a_verifier';
let current: AdminArrete | null = null;

// ------------------------------------------------------------------------- authentification
async function boot() {
  const me = await api<{ admin: boolean }>('api/admin/me');
  if (!me.admin) return showLogin();
  meta = await api<Meta>('api/meta');
  $('login').hidden = true;
  $('board').hidden = false;
  $('admin-actions').hidden = false;
  await loadList();
  refreshSync();
}

function showLogin() {
  $('login').hidden = false;
  $('board').hidden = true;
  $('admin-actions').hidden = true;
  $<HTMLInputElement>('pwd').focus();
}

$('login-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const err = $('login-err');
  err.textContent = '';
  try {
    await api('api/admin/login', { method: 'POST', body: JSON.stringify({ password: $<HTMLInputElement>('pwd').value }) });
    $<HTMLInputElement>('pwd').value = '';
    await boot();
  } catch (x) {
    err.textContent = (x as Error).message;
  }
});

$('btn-logout').addEventListener('click', async () => {
  await api('api/admin/logout', { method: 'POST' });
  location.reload();
});

$('btn-sync').addEventListener('click', async () => {
  const r = await api<{ statut: string }>('api/admin/sync', { method: 'POST' });
  $('sync-info').textContent = r.statut === 'lance' ? 'Synchronisation lancée…' : 'Une synchronisation est déjà en cours…';
  setTimeout(refreshSync, 4000);
});

async function refreshSync() {
  const s = await api<{ en_cours: boolean; journal: { debut: string; fin: string | null; nouveaux: number; erreurs: number; message: string }[] }>('api/admin/sync');
  const last = s.journal[0];
  const info = $('sync-info');
  if (s.en_cours) {
    info.textContent = 'Synchronisation en cours…';
    setTimeout(async () => {
      await refreshSync();
      await loadList();
    }, 8000);
  } else if (last) {
    const d = new Date(last.fin ?? last.debut);
    info.textContent = `Dernière synchronisation : ${d.toLocaleString('fr-FR')} – ${last.nouveaux} traité(s), ${last.erreurs} erreur(s). ${last.message}`;
  }
}

// ------------------------------------------------------------------------- liste
async function loadList() {
  const data = await api<{ compteurs: Record<string, number>; arretes: AdminArrete[] }>('api/admin/arretes');
  rows = data.arretes;
  const tabs = $('tabs');
  tabs.replaceChildren();
  const total = Object.values(data.compteurs).reduce((a, b) => a + b, 0);
  for (const [k, label] of Object.entries(STATUTS)) {
    const n = k ? data.compteurs[k] ?? 0 : total;
    const b = h('button', { role: 'tab', type: 'button', 'aria-selected': String(k === tab) }, `${label} (${n})`);
    b.addEventListener('click', () => {
      tab = k;
      loadList();
    });
    tabs.append(b);
  }
  renderTable();
}

function renderTable() {
  const q = $<HTMLInputElement>('admin-q').value.toLowerCase();
  const body = $('table').querySelector('tbody')!;
  body.replaceChildren();
  for (const a of rows) {
    if (tab && a.statut !== tab) continue;
    if (q && ![a.numero, a.titre, a.lieu, a.objet_registre].join(' ').toLowerCase().includes(q)) continue;
    const tr = h('tr', { tabindex: '0', class: current?.id === a.id ? 'is-current' : '' },
      h('td', {}, a.numero),
      h('td', {}, h('strong', {}, a.titre), h('br'), h('span', { class: 'muted' }, a.lieu || '—'),
        a.motif_verification ? h('div', { class: 'motif' }, a.motif_verification) : null),
      h('td', {}, a.date_debut ? `${a.date_debut}${a.date_fin && a.date_fin !== a.date_debut ? ` → ${a.date_fin}` : ''}` : '—'),
      h('td', {}, h('span', { class: `st st-${a.statut}` }, STATUTS[a.statut]),
        a.modifie_manuellement ? h('span', { class: 'muted small' }, ' ✎') : null),
    );
    const open = () => openEditor(a.id);
    tr.addEventListener('click', open);
    tr.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') open();
    });
    body.append(tr);
  }
}
$('admin-q').addEventListener('input', renderTable);

// ------------------------------------------------------------------------- éditeur
let emap: maplibregl.Map | null = null;
let features: GeoJSON.Feature[] = [];
let draft: [number, number][] = [];
let mode: 'none' | 'line' | 'point' | 'delete' = 'none';

async function openEditor(id: string) {
  current = await api<AdminArrete>(`api/admin/arretes/${encodeURIComponent(id)}`);
  features = structuredClone(current.geojson.features ?? []);
  draft = [];
  mode = 'none';
  renderEditor(current);
  renderTable();
}

function field(label: string, input: HTMLElement, hint?: string) {
  const id = input.id;
  return h('div', { class: 'field' }, h('label', { for: id }, label), input, hint ? h('p', { class: 'hint' }, hint) : null);
}

function renderEditor(a: AdminArrete) {
  const ed = $('editor');
  ed.hidden = false;
  const close = h('button', { type: 'button', class: 'icon-btn', 'aria-label': 'Fermer' }, icon(X, 18));
  close.addEventListener('click', () => {
    ed.hidden = true;
    current = null;
    renderTable();
  });

  const titre = h('input', { id: 'e-titre', type: 'text', value: a.titre });
  const cat = h('select', { id: 'e-cat' }, ...Object.entries(CATEGORIES).map(([k, c]) => h('option', { value: k, selected: k === a.categorie }, c.label)));
  const imp = h('fieldset', { class: 'checks' }, h('legend', {}, 'Conséquences'),
    ...IMPACT_ORDER.map((k) => h('label', {}, h('input', { type: 'checkbox', value: k, checked: a.impacts.includes(k) }), ' ', IMPACTS[k].label)));
  const deb = h('input', { id: 'e-deb', type: 'date', value: a.date_debut ?? '' });
  const fin = h('input', { id: 'e-fin', type: 'date', value: a.date_fin ?? '' });
  const hor = h('input', { id: 'e-hor', type: 'text', value: a.horaires.join(', ') });
  const lieu = h('textarea', { id: 'e-lieu', rows: '2' }, a.lieu);
  const inter = h('input', { id: 'e-inter', type: 'text', value: a.intervenant });
  const note = h('textarea', { id: 'e-note', rows: '2' }, a.note);
  const statut = h('fieldset', { class: 'checks inline' }, h('legend', {}, 'Statut'),
    ...(['publie', 'a_verifier', 'masque'] as const).map((k) =>
      h('label', {}, h('input', { type: 'radio', name: 'e-statut', value: k, checked: a.statut === k }), ' ', { publie: 'Publié', a_verifier: 'À vérifier', masque: 'Masqué' }[k])));

  const geoBtn = h('button', { type: 'button', class: 'btn btn-outline small' }, icon(MapPin, 16), 'Localiser à partir du texte « Lieu »');
  geoBtn.addEventListener('click', async () => {
    const r = await api<{ geojson: GeoJSON.FeatureCollection; quality: string }>('api/admin/geocoder', {
      method: 'POST', body: JSON.stringify({ texte: lieu.value }),
    });
    if (!r.geojson.features.length) return toast('Aucun lieu reconnu dans ce texte (indiquez le type de voie : « 12 rue Centrale », « Rue X entre la rue Y et la rue Z »…).', true);
    features = [...features.filter((f) => f.properties?.role === 'deviation'), ...r.geojson.features];
    redraw(true);
    toast(`Tracé recalculé (qualité : ${r.quality}). Pensez à enregistrer.`);
  });

  const save = async (publish: boolean) => {
    const body: Record<string, unknown> = {
      titre: titre.value,
      categorie: cat.value as Categorie,
      impacts: [...imp.querySelectorAll<HTMLInputElement>('input:checked')].map((i) => i.value as Impact),
      date_debut: deb.value || null,
      date_fin: fin.value || null,
      horaires: hor.value.split(',').map((s) => s.trim()).filter(Boolean),
      lieu: lieu.value,
      intervenant: inter.value,
      note: note.value,
      statut: publish ? 'publie' : (statut.querySelector<HTMLInputElement>('input:checked')?.value ?? a.statut),
      geojson: { type: 'FeatureCollection', features },
    };
    try {
      current = await api<AdminArrete>(`api/admin/arretes/${encodeURIComponent(a.id)}`, { method: 'PUT', body: JSON.stringify(body) });
      toast('Enregistré.');
      await loadList();
      renderEditor(current);
    } catch (x) {
      toast((x as Error).message, true);
    }
  };
  const bSave = h('button', { type: 'button', class: 'btn' }, icon(Check, 16), 'Enregistrer');
  bSave.addEventListener('click', () => save(false));
  const bPub = h('button', { type: 'button', class: 'btn btn-ok' }, icon(Check, 16), 'Enregistrer et publier');
  bPub.addEventListener('click', () => save(true));
  const bRe = h('button', { type: 'button', class: 'btn btn-outline' }, icon(RefreshCw, 16), 'Relancer l’extraction automatique');
  bRe.addEventListener('click', async () => {
    if (!confirm('Les modifications manuelles de cet arrêté seront remplacées par le résultat automatique. Continuer ?')) return;
    current = await api<AdminArrete>(`api/admin/arretes/${encodeURIComponent(a.id)}/retraiter`, { method: 'POST', body: '{}' });
    features = structuredClone(current.geojson.features ?? []);
    await loadList();
    renderEditor(current);
    toast('Extraction relancée.');
  });

  // Outils de dessin
  const tool = (m: typeof mode, ic: Parameters<typeof icon>[0], label: string) => {
    const b = h('button', { type: 'button', class: 'tool', 'aria-pressed': String(mode === m), 'data-mode': m }, icon(ic, 16), label);
    b.addEventListener('click', () => setMode(mode === m ? 'none' : m));
    return b;
  };
  const finish = h('button', { type: 'button', class: 'tool' }, icon(Check, 16), 'Terminer la ligne');
  finish.addEventListener('click', finishLine);
  const undo = h('button', { type: 'button', class: 'tool' }, icon(Undo2, 16), 'Annuler le dernier point');
  undo.addEventListener('click', () => {
    draft.pop();
    redraw();
  });
  const clear = h('button', { type: 'button', class: 'tool danger' }, icon(Eraser, 16), 'Tout effacer');
  clear.addEventListener('click', () => {
    if (!confirm('Effacer tous les tracés de cet arrêté ?')) return;
    features = [];
    draft = [];
    redraw();
  });

  const mapDiv = h('div', { class: 'emap', id: 'emap' });
  const info = h('dl', { class: 'meta' },
    h('dt', {}, 'Numéro'), h('dd', {}, a.numero),
    h('dt', {}, 'Objet au registre'), h('dd', {}, a.objet_registre || '—'),
    h('dt', {}, 'Localisation auto'), h('dd', {}, a.qualite_geo),
    a.motif_verification ? h('dt', {}, 'À vérifier car') : null, a.motif_verification ? h('dd', {}, a.motif_verification) : null,
    a.erreur ? h('dt', {}, 'Erreur') : null, a.erreur ? h('dd', { class: 'error' }, a.erreur) : null,
  );
  const docs = h('p', {},
    h('a', { href: `api/admin/arretes/${encodeURIComponent(a.id)}/pdf`, target: '_blank', rel: 'noopener', class: 'btn btn-outline small' }, icon(FileText, 16), 'Ouvrir le PDF'));
  const ocr = h('details', { class: 'ocr' }, h('summary', {}, 'Texte extrait du document'), h('pre', {}, a.texte ?? ''));

  ed.replaceChildren(
    h('div', { class: 'ed-head' }, h('h2', {}, `${a.numero} — ${a.titre}`), close),
    h('div', { class: 'ed-grid' },
      h('form', { class: 'ed-form', id: 'ed-form' },
        field('Titre public', titre),
        field('Type', cat),
        imp,
        h('div', { class: 'row2' }, field('Début', deb), field('Fin', fin)),
        field('Horaires', hor, 'Séparés par des virgules, ex. « 7h00–17h00, hors week-end »'),
        field('Lieu (texte affiché)', lieu),
        geoBtn,
        h('p', { class: 'hint' }, 'Formulations reconnues : « 12 rue Centrale », « du 6 au 18 avenue Gabriel Péri », « Rue X entre la rue Y et la rue Z », « angle de la rue X et de la rue Y », « Rue X » (rue entière).'),
        field('Intervenant', inter, 'Ne jamais saisir le nom d’un particulier.'),
        field('Note publique', note),
        statut,
        h('div', { class: 'ed-actions' }, bSave, bPub, bRe),
        info, docs, ocr,
      ),
      h('div', { class: 'ed-map' },
        h('div', { class: 'tools', role: 'toolbar', 'aria-label': 'Outils de tracé' },
          tool('line', Spline, 'Tracer une ligne'), tool('point', MousePointerClick, 'Placer un point'),
          tool('delete', Trash2, 'Supprimer un tracé'), finish, undo, clear),
        h('p', { class: 'hint', id: 'tool-hint' }, 'Choisissez un outil. Ligne : cliquez chaque point puis « Terminer » (ou double-clic).'),
        mapDiv,
      ),
    ),
  );
  ed.querySelector<HTMLFormElement>('#ed-form')!.addEventListener('submit', (e) => e.preventDefault());
  setupMap(mapDiv);
  ed.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function setMode(m: typeof mode) {
  if (mode === 'line' && m !== 'line') finishLine();
  mode = m;
  document.querySelectorAll<HTMLButtonElement>('.tool[data-mode]').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.mode === m)));
  const hints = {
    none: 'Choisissez un outil.',
    line: 'Cliquez sur la carte pour ajouter des points, puis « Terminer la ligne » ou double-cliquez.',
    point: 'Cliquez sur la carte pour placer un point.',
    delete: 'Cliquez sur un tracé pour le supprimer.',
  };
  $('tool-hint').textContent = hints[m];
  if (emap) {
    emap.getCanvas().style.cursor = m === 'none' ? '' : 'crosshair';
    if (m === 'line') emap.doubleClickZoom.disable();
    else emap.doubleClickZoom.enable();
  }
}

function finishLine() {
  if (draft.length >= 2) features.push({ type: 'Feature', properties: { role: 'impact' }, geometry: { type: 'LineString', coordinates: draft } });
  draft = [];
  redraw();
}

function editData(): GeoJSON.FeatureCollection {
  const fs: GeoJSON.Feature[] = features.map((f, i) => ({ ...f, properties: { ...(f.properties ?? {}), i } }));
  if (draft.length) {
    fs.push({ type: 'Feature', properties: { role: 'draft', i: -1 }, geometry: { type: 'LineString', coordinates: draft.length > 1 ? draft : [draft[0], draft[0]] } });
    draft.forEach((c) => fs.push({ type: 'Feature', properties: { role: 'vertex', i: -1 }, geometry: { type: 'Point', coordinates: c } }));
  }
  return { type: 'FeatureCollection', features: fs };
}

function redraw(fit = false) {
  if (!emap?.getSource('edit')) return;
  (emap.getSource('edit') as maplibregl.GeoJSONSource).setData(editData());
  if (fit) {
    const b = boundsOf({ type: 'FeatureCollection', features });
    if (b) emap.fitBounds(b, { padding: 60, maxZoom: 17, duration: 0 });
  }
}

function setupMap(div: HTMLElement) {
  emap?.remove();
  emap = createMap(div, meta);
  emap.on('load', () => {
    const m = emap!;
    m.addSource('edit', { type: 'geojson', data: editData() });
    const role = (r: string): maplibregl.ExpressionSpecification => ['==', ['get', 'role'], r];
    m.addLayer({ id: 'e-dev', type: 'line', source: 'edit', filter: role('deviation'), paint: { 'line-color': '#262532', 'line-width': 3, 'line-dasharray': [1.5, 1.5] } });
    m.addLayer({ id: 'e-fill', type: 'fill', source: 'edit', filter: ['all', role('impact'), ['==', ['geometry-type'], 'Polygon']], paint: { 'fill-color': '#d62828', 'fill-opacity': 0.3 } });
    m.addLayer({ id: 'e-line', type: 'line', source: 'edit', filter: ['all', role('impact'), ['in', ['geometry-type'], ['literal', ['LineString', 'MultiLineString']]]], layout: { 'line-cap': 'round' }, paint: { 'line-color': '#d62828', 'line-width': 7, 'line-opacity': 0.85 } });
    m.addLayer({ id: 'e-point', type: 'circle', source: 'edit', filter: ['all', role('impact'), ['==', ['geometry-type'], 'Point']], paint: { 'circle-radius': 8, 'circle-color': '#d62828', 'circle-stroke-color': '#fff', 'circle-stroke-width': 2 } });
    m.addLayer({ id: 'e-draft', type: 'line', source: 'edit', filter: role('draft'), paint: { 'line-color': '#e76f00', 'line-width': 4, 'line-dasharray': [2, 1] } });
    m.addLayer({ id: 'e-vertex', type: 'circle', source: 'edit', filter: role('vertex'), paint: { 'circle-radius': 5, 'circle-color': '#fff', 'circle-stroke-color': '#e76f00', 'circle-stroke-width': 2 } });
    redraw(true);
    m.on('click', (e) => {
      const c: [number, number] = [+e.lngLat.lng.toFixed(6), +e.lngLat.lat.toFixed(6)];
      if (mode === 'line') {
        draft.push(c);
        redraw();
      } else if (mode === 'point') {
        features.push({ type: 'Feature', properties: { role: 'impact' }, geometry: { type: 'Point', coordinates: c } });
        redraw();
      } else if (mode === 'delete') {
        const box: [maplibregl.PointLike, maplibregl.PointLike] = [[e.point.x - 6, e.point.y - 6], [e.point.x + 6, e.point.y + 6]];
        const hit = m.queryRenderedFeatures(box, { layers: ['e-line', 'e-point', 'e-fill', 'e-dev'] })[0];
        if (hit && typeof hit.properties?.i === 'number' && hit.properties.i >= 0) {
          features.splice(hit.properties.i, 1);
          redraw();
        }
      }
    });
    m.on('dblclick', (e) => {
      if (mode === 'line') {
        e.preventDefault();
        finishLine();
      }
    });
  });
}

let toastTimer: number | undefined;
function toast(msg: string, error = false) {
  let t = document.getElementById('toast');
  if (!t) {
    t = h('div', { id: 'toast', class: 'toast', role: 'status', 'aria-live': 'polite' });
    document.body.append(t);
  }
  t.textContent = msg;
  t.classList.toggle('is-error', error);
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => (t!.hidden = true), 5000);
}

boot().catch((e) => {
  if ((e as { status?: number }).status === 401) showLogin();
  else toast((e as Error).message, true);
});
