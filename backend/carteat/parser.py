"""Extraction par règles des informations d'un arrêté temporaire (AT) de Corbas.

Entrée : le texte (OCR ou texte natif) du PDF. Sortie : un dictionnaire structuré
(objet, catégorie, mesures, calendrier, localisations à géocoder…).
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date

from .dates import extract_calendar
from .streets import StreetIndex
from .textutil import clean_ocr, norm, one_line, strip_accents

# --------------------------------------------------------------------------- référentiels
CATEGORIES = {
    "reseaux": "Travaux de réseaux",
    "voirie": "Travaux de voirie",
    "chantier": "Échafaudage, benne, nacelle",
    "demenagement": "Déménagement",
    "evenement": "Événement, manifestation",
    "autre": "Autre",
}
IMPACTS = {
    "route_barree": "Route barrée",
    "circulation_alternee": "Circulation alternée ou rétrécie",
    "stationnement": "Stationnement neutralisé",
    "trottoir": "Trottoir ou cheminement piéton modifié",
    "vitesse": "Vitesse limitée",
    "deviation": "Déviation",
}

# (motif, poids) — évalués sur l'objet (poids ×3) puis sur les articles.
CATEGORY_RULES: dict[str, list[tuple[str, int]]] = {
    "demenagement": [(r"demenag", 10)],
    "evenement": [
        (r"spectacle|manifestation|\bfete|foire|vide[ -]grenier|carnaval|vogue|defile|commemoration"
         r"|ceremonie|\bchrono|forum|cirque|brocante|festival|concert|semaine bleue|boudin|marche de"
         r"|kermesse|braderie|course|animation|exposition|feu d artifice|bal\b|repas|soiree|ducasse", 6),
    ],
    "chantier": [
        (r"echafaudage|\bbenne|nacelle|\bgrue|livraison|dechargement|chape|toupie|ravalement|facade"
         r"|toiture|elagage|demolition|camion|monte[ -]meuble|engin de levage|pompe a beton|bigbag", 5),
    ],
    "reseaux": [
        (r"assainissement|\bgaz\b|grdf|eau potable|\baep\b|electri|enedis|\bbt\b|\bhta\b|telecom|fibre"
         r"|orange|chambre|branchement|raccordement|canalisation|eclairage|reseau|conduite|\bposte\b"
         r"|site radio|\btsg\b|cable|gc pour|sondage", 4),
    ],
    "voirie": [
        (r"chaussee|enrobe|tranchee|trottoir|voirie|amenagement|ralentisseur|marquage|pave|plateau"
         r"|entourage|espaces? verts?|trapeze|domaine public|bordure|signalisation horizontale", 3),
    ],
}

RX_NUMERO = re.compile(r"Arr[êe]t[ée]\s+temporaire\s+N\s*[°o]?\s*:?\s*(\d{1,4})\s*/\s*(\d{2,4})", re.I)
RX_OBJET = re.compile(
    r"Objet\s*:\s*(.+?)(?=\n\s*(?:Le\s+Maire|La\s+Pr[ée]sidente|Le\s+Pr[ée]sident|VU\b)|\n\s*\n\s*\n)",
    re.S | re.I,
)
RX_SIGNATURE = re.compile(r"[AÀ]\s+Corbas,?\s+le\s+(\d{1,2})\s*/\s*(\d{1,2})\s*/\s*(\d{4})", re.I)
RX_ARTICLE = re.compile(r"(?:^|\n)\s*Article\s+(\d+|dernier|unique)\s*(?:er)?\s*[:.\-]?", re.I)
RX_PAGE_NOISE = re.compile(
    r"^\s*(Page\s*:?\s*\d+|Dernière page|Police du stationnement|Police de la circulation"
    r"|Extrait du registre des arrêtés.*|Arrêté de la Présidente.*|Arrêté du Président.*"
    r"|de la Métropole de Lyon|Métropole|#signature#)\s*$",
    re.I | re.M,
)
RX_PERSON = re.compile(
    r"\b(Monsieur|Madame|Mademoiselle|Mme|Mlle|Mr|M\.)\s+((?:[A-ZÀ-Ÿ][\wÀ-ÿ'’\-]+\s*){1,4})"
)
RX_COMPANY = re.compile(
    r"(?:l['’]\s*)?(?i:entreprise|soci[ée]t[ée]|ets\.?|établissements?)\s+(?:SARL|SAS|SA\b)?\s*"
    r"([A-Z0-9][A-Za-z0-9À-ÿ&'’.\- ]{1,50}?)(?=\s*(?:[,;]|domicil|\(|pour|doit|est\b|sont\b|\n|$))"
)
RX_COMPANY_BARE = re.compile(r"\b(?:SARL|SAS|SASU|EURL)\s+([A-Z][A-Za-z0-9À-ÿ&'’.\- ]{1,40}?)(?=\s*(?:,|domicili|\n))")
# Adresse postale de domiciliation (demandeur) : à ne pas confondre avec le lieu des travaux.
RX_DOMICILE = re.compile(
    r"(domicili[ée]e?s?|demeurant|sise?)\s+(?:au\s+)?[^;\n]{0,90}?\b\d{5}\s+[A-Za-zÀ-ÿ'\- ]{2,30}", re.I
)
# Adresse postale complète (avec code postal) : « 24 rue Louis Blanc; 75010 Paris ».
RX_POSTAL = re.compile(
    r"\b\d{1,4}\s*(?:bis|ter)?,?\s+(?:rue|avenue|av\.?|chemin|route|impasse|allée|place|boulevard|bd|quai|cours)\b"
    r"[^;,\n]{1,60}?[,;\s–-]+\d{5}\s+[A-Za-zÀ-ÿ'\-]+(?:\s+(?:sur|en|de|du|la|le|les|lès|d')\s*[A-Za-zÀ-ÿ'\-]+)*",
    re.I,
)

GENERIC_PLACES = {"corbas", "parking", "police municipale", "mairie", "ecole", "gymnase", "eglise"}

ART = r"(?:(?:de|du|des|d|a|au|aux|sur) )?(?:(?:la|l|le|les) )?"
ST = r"§(\d+)"


@dataclass
class Extraction:
    numero: str | None = None
    objet: str | None = None
    date_signature: str | None = None
    categorie: str = "autre"
    impacts: list[str] = field(default_factory=list)
    calendrier: dict = field(default_factory=dict)
    localisations: list[dict] = field(default_factory=list)
    deviation: list[str] = field(default_factory=list)
    lieu_texte: str | None = None
    entreprise: str | None = None
    particulier: bool = False
    mesures_texte: str = ""
    avertissements: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- découpage
def split_articles(text: str) -> list[tuple[str, str]]:
    parts = list(RX_ARTICLE.finditer(text))
    out = []
    for i, m in enumerate(parts):
        end = parts[i + 1].start() if i + 1 < len(parts) else len(text)
        out.append((m.group(1).lower(), text[m.end():end].strip()))
    return out


def anonymize(s: str) -> str:
    """Supprime les noms de personnes physiques (RGPD)."""
    s = RX_PERSON.sub("un particulier", s)
    return re.sub(r"\s{2,}", " ", s).strip()


def remove_postal_addresses(s: str) -> str:
    """Retire les adresses de domiciliation des demandeurs (≠ lieu de l'intervention)."""
    s = RX_DOMICILE.sub(" ", one_line(s))

    def repl(m: re.Match) -> str:
        before = s[max(0, m.start() - 14):m.start()].lower()
        # « au droit du 53 Avenue de la Villerme - 69960 CORBAS » désigne bien le lieu.
        if re.search(r"(droit du|niveau du|\bau|\bdu)\s*$", before):
            return m.group(0)
        return " "

    return RX_POSTAL.sub(repl, s)


def _is_measure_article(t: str) -> bool:
    low = strip_accents(t.lower())
    if re.search(r"signalisation correspondante|r\s*417|non[- ]respect|recours|charges? chacun|execution du present", low):
        return False
    return bool(re.search(r"circulation|stationn|pieton|trottoir|autoris|interdi|reserv|vitesse|chantier|itineraire|voie", low))


def classify(objet: str, articles: str) -> str:
    scores = {k: 0 for k in CATEGORY_RULES}
    o, a = norm(objet or ""), norm(articles or "")
    for cat, rules in CATEGORY_RULES.items():
        for rx, w in rules:
            scores[cat] += 3 * w * len(re.findall(rx, o)) + w * min(len(re.findall(rx, a)), 3)
    # « Réfection de tranchée/chaussée » relève de la voirie même si un réseau est cité.
    if re.search(r"refection|reprise", o) and re.search(r"tranchee|chaussee|enrobe|trottoir", o):
        scores["voirie"] += 20
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "autre"


def detect_impacts(articles: str) -> list[str]:
    a = norm(articles)
    out = []
    pieton_interdit = re.search(r"circulation pietonne (sera |est )?interdit", a)
    if (
        re.search(r"circulation(?: [a-z0-9§]+){0,8}? (?:sera|est|seront|sont) interdit", a)
        and not (pieton_interdit and not re.search(r"circulation (?!pietonne)(?:[a-z0-9§]+ ){0,8}?(?:sera|est|seront|sont) interdit", a))
    ) or re.search(r"route barree|acces interdit|circulation et le stationnement[^.]{0,120}interdit|stationnement et la circulation[^.]{0,120}interdit|circulation[^.]{0,30}interrompue|ferm\w+ a la circulation", a):
        out.append("route_barree")
    if re.search(r"demi chaussee|alternat|chaussee retrecie|une seule voie|feux tricolores|sens unique|retreci|a la reduire|reduction de la circulation|reductions de la circulation", a):
        out.append("circulation_alternee")
    if re.search(r"stationnement(?: [a-z0-9]+){0,10}? (?:sera |est |seront |sont )?(?:interdit|reserve|neutralise|genant)|places? de stationnement|stationnements? reserve|autorise\w* a stationner|stationnement (?:sera |est )?autorise|interdit\w* au stationnement|deposer une benne|reservees?", a):
        out.append("stationnement")
    if re.search(r"pieton|trottoir", a):
        out.append("trottoir")
    if re.search(r"vitesse[^.]{0,40}(?:reduite|limitee)|30 kms? ?h|30 km h", a):
        out.append("vitesse")
    if re.search(r"deviation", a):
        out.append("deviation")
    return out


# --------------------------------------------------------------------------- localisations
def _placeholder(text: str, idx: StreetIndex):
    tokens = re.sub(r"\b0+(\d)", r"\1", norm(text)).split()
    matches = idx.find_streets(tokens)
    out, names, i, k = [], [], 0, 0
    for m in matches:
        out.extend(tokens[i:m.start])
        out.append(f"§{k}")
        names.append(m.name)
        i, k = m.end, k + 1
    out.extend(tokens[i:])
    return " ".join(out), names


def _anchor_regexes():
    num = r"(\d{1,4})(?: (?:bis|ter))?(?: (\d{1,4}))?"
    return {
        "range": re.compile(
            rf"(?:entre le|entre les|du|des|depuis le) (\d{{1,4}})(?: \d{{1,4}})? (?:et le|et les|et|au|aux|a|jusqu au) "
            rf"(?:\d{{1,4}} )?(\d{{1,4}}) {ART}{ST}"
        ),
        "between": re.compile(
            rf"{ST}(?: (?!entre)[a-z0-9]+){{0,8}} entre {ART}{ST}(?: et| -)? {ART}{ST}"
        ),
        "intersection": re.compile(
            rf"(?:intersection|croisement|angle|carrefour)(?: (?:de|du|des|d|entre))? {ART}{ST}(?: et| -)? {ART}{ST}"
        ),
        "address": re.compile(rf"(?<![\d§])\b{num} {ART}{ST}"),
    }


_RX = None
RX_CONNECT = re.compile(r"^ ?(?:a|au|aux|jusqu a|jusqu au|jusqu aux|et|et le|et la|et l|vers)(?: (?:l|la|le|les))? ?$")


def extract_locations(text: str, idx: StreetIndex) -> tuple[list[dict], list[str]]:
    """Retourne (localisations, rues citées) à partir d'un texte de mesures."""
    global _RX
    _RX = _RX or _anchor_regexes()
    s, names = _placeholder(text, idx)
    used = [False] * len(s)
    anchors: list[dict] = []  # {start, end, spec}
    locs: list[dict] = []

    def mark(a, b):
        for j in range(a, b):
            used[j] = True

    def free(a, b):
        return not any(used[a:b])

    for m in _RX["range"].finditer(s):
        st = names[int(m.group(3))]
        locs.append({"kind": "segment", "street": st,
                     "from": {"type": "address", "num": m.group(1), "street": st},
                     "to": {"type": "address", "num": m.group(2), "street": st}})
        mark(m.start(), m.end())
    for m in _RX["between"].finditer(s):
        if not free(m.start(), m.end()):
            continue
        a, b, c = (names[int(g)] for g in m.groups())
        if a in (b, c):
            continue
        locs.append({"kind": "segment", "street": a,
                     "from": {"type": "intersection", "streets": [a, b]},
                     "to": {"type": "intersection", "streets": [a, c]}})
        mark(m.start(), m.end())
    for m in _RX["intersection"].finditer(s):
        if not free(m.start(), m.end()):
            continue
        a, b = names[int(m.group(1))], names[int(m.group(2))]
        if a != b:
            anchors.append({"start": m.start(), "end": m.end(),
                            "spec": {"type": "intersection", "streets": [a, b]}})
            mark(m.start(), m.end())
    for m in _RX["address"].finditer(s):
        if not free(m.start(), m.end()):
            continue
        n1 = int(m.group(1))
        if n1 == 0 or n1 > 2000:
            continue
        anchors.append({"start": m.start(), "end": m.end(),
                        "spec": {"type": "address", "num": m.group(1), "street": names[int(m.group(3))],
                                 "num2": m.group(2)}})
        mark(m.start(), m.end())
    anchors.sort(key=lambda a: a["start"])

    consumed = set()
    for i in range(len(anchors) - 1):
        a, b = anchors[i], anchors[i + 1]
        if i in consumed:
            continue
        between = s[a["end"]:b["start"]]
        if RX_CONNECT.match(between):
            sa = _streets_of(a["spec"])
            sb = _streets_of(b["spec"])
            common = [x for x in sa if x in sb]
            locs.append({"kind": "segment", "street": common[0] if common else None,
                         "from": a["spec"], "to": b["spec"]})
            consumed |= {i, i + 1}
    for i, a in enumerate(anchors):
        if i not in consumed:
            locs.append({"kind": "point", "anchor": a["spec"]})

    # Rues citées sans précision (« la circulation, Rue Clément Ader, sera interdite »).
    cited = []
    for k, name in enumerate(names):
        for m in re.finditer(rf"§{k}(?!\d)", s):
            if used[m.start()]:
                continue
            before = s[max(0, m.start() - 30):m.start()]
            if re.search(r"(direction|vers|sens|provenance)(?: [a-z]+){0,3} ?$", before):
                continue
            if name not in cited:
                cited.append(name)
    return locs, cited


def _streets_of(spec: dict) -> list[str]:
    return spec["streets"] if spec["type"] == "intersection" else [spec["street"]]


# --------------------------------------------------------------------------- point d'entrée
def parse(text: str, idx: StreetIndex, ref_date: date | None = None) -> Extraction:
    ex = Extraction()
    text = clean_ocr(text)
    m = RX_NUMERO.search(text)
    if m:
        y = m.group(2)
        ex.numero = f"{int(m.group(1))}/{y if len(y) == 4 else '20' + y}"
    m = RX_SIGNATURE.search(text)
    if m:
        try:
            ex.date_signature = date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat()
        except ValueError:
            pass
    ref = date.fromisoformat(ex.date_signature) if ex.date_signature else (ref_date or date.today())

    m = RX_OBJET.search(text)
    objet = ""
    if m:
        objet = one_line(m.group(1))
        objet = re.sub(r"[\s,.\-]*Voie\s+M[ée]tropole\.?\s*$", "", objet, flags=re.I).strip(" ,.-")
    ex.objet = anonymize(objet) or None

    body = RX_PAGE_NOISE.sub("", text)
    articles = split_articles(body)
    if not articles:
        ex.avertissements.append("Aucun article trouvé dans le document")
    measures, deviation_txt = [], []
    for num, t in articles:
        if num == "dernier":
            continue
        if re.search(r"d[ée]viation", t, re.I) and not re.search(r"(circulation|stationnement)[^.]{0,80}(interdit|alternat|demi)", t, re.I):
            deviation_txt.append(t)
        elif _is_measure_article(t):
            measures.append(t)
    mtext = "\n".join(measures)
    ex.mesures_texte = anonymize(one_line(mtext))[:3000]

    # Demandeur : particulier ou entreprise ?
    head = one_line(text[: text.find(articles[0][1])] if articles else text)
    demande = re.search(r"demande formul[ée]e(?: le [^,]{0,25})? par\s+(.{0,160})", head, re.I)
    first_art = one_line(measures[0] if measures else "")
    scope = (demande.group(1) if demande else "") + " " + first_art[:300]
    consid = re.search(r"CONSID[ÉE]RANT(.{0,300})", head, re.I)
    scope2 = scope + " " + (consid.group(1) if consid else "")
    comp = RX_COMPANY.search(scope2) or RX_COMPANY_BARE.search(scope2)
    if comp:
        name = one_line(comp.group(1)).strip(" ,.;-")
        name = re.sub(r"\s+(domicil\w*|sise?)\b.*$", "", name, flags=re.I)
        if not RX_PERSON.search(name) and len(name) >= 2:
            ex.entreprise = name
    if not ex.entreprise and re.search(r"(Ville|commune) de Corbas", scope):
        ex.entreprise = "Ville de Corbas"
    if not ex.entreprise:
        m_assoc = re.search(r"l['’]association\s+([^,;]{2,60})", scope, re.I)
        if m_assoc:
            ex.entreprise = "Association " + m_assoc.group(1).strip()
    ex.particulier = bool(RX_PERSON.search(scope)) and not ex.entreprise

    ex.categorie = classify(objet, mtext)
    ex.impacts = detect_impacts(mtext)
    if deviation_txt and "deviation" not in ex.impacts:
        ex.impacts.append("deviation")

    cal_text = mtext or objet
    ex.calendrier = extract_calendar(cal_text, ref).to_dict()
    if not ex.calendrier["debut"] and objet:
        ex.calendrier = extract_calendar(objet, ref).to_dict()

    # Localisation : articles (sans adresses de domiciliation), puis objet en complément.
    loc_text = remove_postal_addresses(mtext)
    locs, cited = extract_locations(loc_text, idx)
    if not locs:
        o_locs, o_cited = extract_locations(remove_postal_addresses(objet), idx)
        if o_locs:
            locs = o_locs
        cited = cited or o_cited
    if locs:
        uniq = {repr(sorted(l.items(), key=str)): l for l in locs}
        ex.localisations = list(uniq.values())
    elif cited:
        ex.localisations = [{"kind": "street", "street": s} for s in cited]
    else:
        places = [p for p in idx.find_places(norm(loc_text + " " + objet)) if norm(p[0]) not in GENERIC_PLACES]
        ex.localisations = [{"kind": "place", "name": n} for n, _ in places[:3]]
    for t in deviation_txt:
        _, dev = extract_locations(t, idx)
        ex.deviation.extend(d for d in dev if d not in ex.deviation)

    ex.lieu_texte = _lieu_texte(objet, ex.localisations)
    if not ex.localisations:
        ex.avertissements.append("Lieu non identifié")
    if not ex.calendrier["debut"]:
        ex.avertissements.append("Dates non identifiées")
    return ex


def _lieu_texte(objet: str, locs: list[dict]) -> str | None:
    def a(spec):
        if spec["type"] == "address":
            n = spec["num"] + (f"-{spec['num2']}" if spec.get("num2") else "")
            return f"{n} {spec['street']}"
        return f"angle de {spec['streets'][0]} et {spec['streets'][1]}"

    parts = []
    for l in locs:
        if l["kind"] == "street":
            parts.append(l["street"])
        elif l["kind"] == "place":
            parts.append(l["name"])
        elif l["kind"] == "point":
            parts.append(a(l["anchor"]))
        elif l["kind"] == "segment":
            f, t = l["from"], l["to"]
            if f["type"] == t["type"] == "address" and f["street"] == t["street"]:
                parts.append(f"du {f['num']} au {t['num']} {l['street']}")
            elif f["type"] == t["type"] == "intersection" and l.get("street"):
                o1 = [s for s in f["streets"] if s != l["street"]][0]
                o2 = [s for s in t["streets"] if s != l["street"]][0]
                parts.append(f"{l['street']}, entre {o1} et {o2}")
            else:
                parts.append(f"de {a(f)} à {a(t)}")
    return " ; ".join(dict.fromkeys(parts)) or None
