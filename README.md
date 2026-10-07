# Carte des arrêtés travaux – Ville de Corbas

Application web qui rend lisibles, sur une carte, les **arrêtés temporaires (AT)** signés par la mairie de Corbas :
fermetures de rues, circulation alternée, stationnement neutralisé, trottoirs fermés, événements…

- **Back-office automatique** : chaque jour, l'application interroge le
  [registre officiel des actes](https://publis2low.adullact.org/registre/216902734), récupère les nouveaux arrêtés
  numérotés `AT <numéro>/<année>`, lit le PDF (OCR), en extrait la nature de l'intervention, les mesures,
  le calendrier et le lieu, puis géocode le lieu sur le tronçon de voie concerné.
- **Front-office** : carte OpenStreetMap (MapLibre) de la commune, liste des arrêtés en cours et à venir, filtres
  par période, type d'intervention et conséquences, fiche détaillée avec lien vers l'arrêté officiel.
- **Administration** (`/admin.html`) : file « À vérifier », correction des informations et du tracé, publication.

## Architecture

```
┌──────────────────────── conteneur Docker ────────────────────────┐
│  FastAPI (uvicorn)                                                │
│   ├─ /api/arretes, /api/meta          API publique (JSON)         │
│   ├─ /api/admin/*                     API d'administration        │
│   ├─ /                                front statique (Vite)       │
│   └─ APScheduler : synchro quotidienne (6 h 15, heure de Paris)   │
│                                                                    │
│  Chaîne de traitement (backend/carteat)                            │
│   registry.py  → API JSON PubliS²low (liste + PDF)                 │
│   textract.py  → texte natif PyMuPDF, sinon OCR Tesseract (fra)    │
│   parser.py    → règles : objet, catégorie, mesures, intervenant,  │
│   dates.py        dates/horaires, localisations                    │
│   streets.py   → référentiel des voies OSM de la commune           │
│   geocode.py   → API Adresse (BAN) + tronçons OSM → GeoJSON        │
│   pipeline.py  → orchestration, statut publié / à vérifier         │
│                                                                    │
│  /data (volume) : SQLite, PDF téléchargés, référentiel OSM         │
└────────────────────────────────────────────────────────────────────┘
```

**Pas d'IA externe** : l'extraction repose sur des règles adaptées au modèle d'arrêté de Corbas. Sur les
115 arrêtés 2026 du registre, la grande majorité est lue et localisée automatiquement. Les cas
ambigus (arrêtés permanents, lieux-dits, documents d'un autre modèle) partent dans la file
**« À vérifier »** et ne sont pas publiés tant qu'un administrateur ne les a pas validés.

Règles de publication automatique : dates trouvées, au moins une mesure de circulation/stationnement,
localisation précise (adresse, carrefour, tronçon) ou rue entière explicitement citée, durée inférieure à un an.

### Localisation

| Formulation dans l'arrêté | Tracé produit |
|---|---|
| « au droit du 51 rue Centrale » | adresse BAN accrochée à la voie, tronçon de ±25 m |
| « du 31 au 49 rue Eugène Delacroix », « entre le 6 et 18 avenue Gabriel Péri » | tronçon entre les deux adresses |
| « Chemin de Grange Blanche, entre l'avenue du 8 mai 1945 et la rue Jean Macé » | tronçon entre les deux carrefours |
| « du 14-18 rue Centrale à l'intersection rue Centrale et impasse Jules Pellet » | tronçon adresse → carrefour |
| « la circulation, Rue Clément Ader, sera interdite » | toute la rue (dans la commune) |
| « L'itinéraire de déviation empruntera : … » | rues de déviation (pointillés) |

| « parking des ombrières au Parc de Loisirs » | emprise du lieu-dit (référentiel local) |
| « Route de Marennes, entre le chemin des Bruyères et le 300 Route de Marennes » | tronçon carrefour → adresse, suivi le long de la voie même si elle est découpée dans OSM |

Les noms de voies sont reconnus même avec des erreurs d'OCR ou des variantes (« av. du 08 Mai 45 »,
« rue Mirabeau » → « Rue Comte de Mirabeau »).

### Lieux-dits

Les arrêtés désignent parfois un lieu sous un nom différent de celui d'OpenStreetMap (« Parc de Loisirs »
↔ OSM « Parc de Loisirs de Corbas »), ou un lieu absent d'OSM. Ces correspondances sont décrites dans
[`backend/carteat/data/lieux.json`](backend/carteat/data/lieux.json) : nom, alias, `osm_nom` (objet OSM
dont la géométrie est utilisée en priorité) et une géométrie GeoJSON de secours.
Un fichier `lieux.json` de même format placé dans le volume de données (`/data/lieux.json`) complète ou
remplace ces entrées sans reconstruire l'image (redémarrer le conteneur, puis
`python -m carteat.cli reprocess`).

Le plus durable reste de nommer l'objet dans OpenStreetMap : il est repris au rafraîchissement mensuel
du référentiel (`python -m carteat.cli refresh-osm` pour l'avoir tout de suite).

### Données personnelles (RGPD)

- Les noms et adresses des **particuliers** (déménagements, bennes…) ne sont jamais publiés : le titre
  est reconstruit à partir du type d'intervention et du lieu ; l'adresse de domiciliation du demandeur
  est retirée avant la localisation. Le texte OCR et l'objet du registre restent internes (admin).
- Aucun cookie hors session d'administration, aucun traceur ; police système (aucune police téléchargée, pas de Google Fonts).
- Pages « Mentions légales » et « Déclaration d'accessibilité » fournies : **les éléments surlignés
  (éditeur, hébergeur, DPO, contact) sont à compléter par la mairie.**

### Accessibilité (RGAA)

Liste accessible au clavier et aux lecteurs d'écran, équivalente à la carte ; lien d'évitement ; boutons
de filtre `aria-pressed` ; compteur de résultats annoncé (`aria-live`) ; contrastes AA ; focus visible ;
respect de `prefers-reduced-motion`. Un audit RGAA reste nécessaire pour déclarer un taux de conformité.

## Déploiement sur maps.utagawavtt.com/ATCorbas

Prérequis : Docker + Docker Compose, nginx avec HTTPS (déjà en place).

```bash
git clone https://github.com/utagawal/Corbas-CarteAT.git /var/data/carteat-corbas && cd /var/data/carteat-corbas
cp .env.example .env && chmod 600 .env
# 1. clé de session
sed -i "s|^SECRET_KEY=.*|SECRET_KEY=$(openssl rand -base64 48 | tr -d '\n/+=')|" .env
# 2. mot de passe administrateur (la commande affiche la ligne à coller dans .env)
docker compose build
docker compose run --rm carteat python -m carteat.cli hash-password
# 3. démarrage (port 8085 en local uniquement, modifiable via HOST_PORT)
docker compose up -d
docker compose logs -f   # le premier import (OCR des arrêtés 2026) prend ~15-20 min
```

nginx : copier [`deploy/nginx-ATCorbas.conf`](deploy/nginx-ATCorbas.conf) dans `/etc/nginx/snippets/ATCorbas.conf`,
ajouter `include /etc/nginx/snippets/ATCorbas.conf;` dans le bloc `server` HTTPS de `maps.utagawavtt.com`
(avant `location / {`), puis `sudo nginx -t && sudo systemctl reload nginx`. Le snippet utilise
`location ^~` (prioritaire sur les locations regex des tuiles) et `more_set_headers` (module headers-more).

Le dossier du code ne doit pas porter le nom du chemin public (`/var/data/ATCorbas` serait sous le
`root /var/data/` de nginx) : d'où `/var/data/carteat-corbas`, et `.env` en `chmod 600`.

Derrière Cloudflare : ne pas activer *Rocket Loader* ni *Email obfuscation* sur `/ATCorbas/*`
(scripts injectés bloqués par la politique de sécurité du site).

- Carte publique : https://maps.utagawavtt.com/ATCorbas/
- Administration : https://maps.utagawavtt.com/ATCorbas/admin.html

Mise à jour : `cd /var/data/carteat-corbas && git pull && docker compose up -d --build`.

### Déploiement automatique

[`deploy/deploy.sh`](deploy/deploy.sh) récupère `main`, reconstruit l'image, redémarre le conteneur,
vérifie que l'application répond (sinon **retour automatique à la version précédente**), puis met à jour
le snippet nginx s'il a changé (testé par `nginx -t` avant rechargement). Sans nouveauté sur `main`, il ne
fait rien. Journal : `/var/log/carteat-deploy.log`.

| Action | Commande |
|---|---|
| Déployer maintenant | `sudo /var/data/carteat-corbas/deploy/deploy.sh` |
| Voir s'il y a une mise à jour | `sudo /var/data/carteat-corbas/deploy/deploy.sh --check` |
| Redéployer + recalculer les arrêtés | `sudo /var/data/carteat-corbas/deploy/deploy.sh --force --reprocess` |
| Déploiement automatique (toutes les 15 min) | `echo '*/15 * * * * root /var/data/carteat-corbas/deploy/deploy.sh >/dev/null 2>&1' \| sudo tee /etc/cron.d/carteat-deploy` |
| Suivre le journal | `sudo tail -f /var/log/carteat-deploy.log` |

Une version qui a échoué n'est pas retentée par le cron (relancer avec `--force` après correction).
Le déploiement est refusé si le dossier contient des modifications locales non committées.

### Intégration sur corbas.fr

```html
<iframe src="https://maps.utagawavtt.com/ATCorbas/?embed=1"
        title="Carte des travaux et événements sur la voie publique à Corbas"
        style="width:100%;height:80vh;border:0" loading="lazy" allow="geolocation"></iframe>
```

`?embed=1` masque l'en-tête (le site hôte a déjà le sien). Les domaines autorisés à intégrer la carte se
règlent avec `FRAME_ANCESTORS` (par défaut `corbas.fr` et ses sous-domaines). Lien direct vers un arrêté :
`…/ATCorbas/#at=<identifiant>`.

### Configuration (`.env`)

| Variable | Défaut | Rôle |
|---|---|---|
| `HOST_PORT` | 8085 | port publié sur 127.0.0.1 |
| `PORT` | 8080 | port interne du conteneur |
| `SECRET_KEY` | — | signature des sessions admin (**obligatoire**) |
| `ADMIN_PASSWORD_HASH` | — | empreinte scrypt du mot de passe admin |
| `START_YEAR` | 2026 | arrêtés importés à partir de cette année |
| `SYNC_HOUR` / `SYNC_MINUTE` | 6 / 15 | heure de la synchro quotidienne (Europe/Paris) |
| `NUMERO_REGEX` | `^\s*AT\s*\d+\s*/\s*\d+\s*$` | sélection des actes au registre |
| `MAP_STYLE_URL` | OpenFreeMap Positron | style du fond de carte |
| `FRAME_ANCESTORS` | corbas.fr | sites autorisés en iframe |
| `BAN_URL`, `OVERPASS_URLS` | services publics | géocodage et référentiel OSM |

### Commandes utiles

```bash
docker compose exec carteat python -m carteat.cli sync          # synchroniser maintenant
docker compose exec carteat python -m carteat.cli reprocess     # ré-extraire (hors corrections manuelles)
docker compose exec carteat python -m carteat.cli refresh-osm   # rafraîchir le référentiel des voies
```

Sauvegarde : le volume `carteat-data` (fichier `carteat.sqlite3` + PDF).

## Développement

```bash
# back
cd backend && python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest
DATA_DIR=../data ADMIN_PASSWORD=dev COOKIE_SECURE=false python -m carteat.cli serve
pytest
# front (proxy /api vers :8080)
cd frontend && npm install && npm run dev
```

Tesseract (`tesseract-ocr`, `tesseract-ocr-fra`) doit être installé pour l'OCR.

## Crédits

Données © contributeurs OpenStreetMap (ODbL) · Base Adresse Nationale (Licence Ouverte) · fond de carte
OpenFreeMap / OpenMapTiles · MapLibre GL JS · icônes Lucide.
