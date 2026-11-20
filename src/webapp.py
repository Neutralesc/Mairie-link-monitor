"""Application web : tableau de bord, lancement et suivi d'une analyse, comptes rendus, CSV, résultats et historique.

Bibliothèque standard et Jinja2. Une analyse à la fois : le lancement passe par `runner` (sous-processus local, ou
GitHub Actions en ligne). Les pages sont des gabarits de src/templates/web (échappement automatique). Les données
viennent d'un PublicationSource (FileSource en local, store_pg en ligne).
Sécurité : par défaut l'application n'écoute que sur ce poste (127.0.0.1). Pour la partager, un mot de passe est
obligatoire (authentification HTTP Basic) ; les formulaires refusent les requêtes venant d'un autre site. Aucune
ressource externe n'est chargée.
"""
from __future__ import annotations

import base64
import csv
import functools
import gzip
import hmac
import html
import io
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import unicodedata
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

from jinja2 import Environment, FileSystemLoader
from markupsafe import Markup

import report
from layout import normalize_dept
from publications import DEPT_RE, FileSource, PublicationSource, SourceIndisponible, instant_iso

log = logging.getLogger("webapp")
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
# Fichiers statiques servis sans mot de passe : public/static (DSFR sous dsfr/, et app.js).
STATIC_URL = "/static/"
STATIC_RACINE = report.STATIC_DSFR.parent
STATIC_MIME = {".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".woff2": "font/woff2",
               ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon",
               ".webmanifest": "application/manifest+json"}
# En-têtes de sécurité de toutes les réponses. Le DSFR est servi depuis le même hôte : aucune ressource externe.
POLITIQUE_CONTENU = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                     "img-src 'self' data:; font-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'")
EN_TETES_SECURITE = {"X-Frame-Options": "DENY", "Referrer-Policy": "same-origin",
                     "Content-Security-Policy": POLITIQUE_CONTENU}
POST_MAX_OCTETS = 10_000         # taille maximale d'un formulaire
CSV_GZIP_MIN_OCTETS = 64 * 1024  # au-delà, un CSV est compressé si le client accepte gzip
TEMPLATES = Path(__file__).resolve().parent / "templates"
CODE_URL = r"[0-9AB]{2,3}"       # code de département dans une adresse
RESULTATS_PAR_PAGE = 100
RECHERCHE_MAX = 80               # caractères retenus pour la recherche libre
RECHERCHE_PUBLIQUE_MAX = 100     # recherche de l'accueil public (départements et communes), tronquée au-delà
LIENS_PAR_PAGE = 100             # tableau « Tous les liens trouvés » de l'accueil public
LIENS_CSV_MAX = 200_000          # plafond du CSV « tous les liens » ; au-delà, l'export est tronqué et l'en-tête le signale
ANCRE_LIENS = "#tous-les-liens"
CHAMPS_TOTAUX = ("nb_communes", "nb_communes_avec_lien", "nb_communes_a_verifier", "nb_communes_sans_lien",
                 "nb_communes_non_analysables")
CHAMPS_LIENS_CSV = ["departement_code", "departement", "code_insee", "commune", "page_source", "url_cible", "apparu_le"]
FENETRE_NOUVEAUX_JOURS = 30      # un lien est « nouveau » pendant ce nombre de jours après sa première apparition
NOUVEAUX_AFFICHES_MAX = 50       # lignes de la section « Nouveaux liens » de l'accueil public
LIBELLE_RESULTATS = "Communes dont le site contient un lien vers Vos Questions"
PROGRESSION_RE = re.compile(r"total (\d+)/(\d+)")  # « [13] 25/130 sites ; total 25/130 » : dernière occurrence
MSG_ECHEC = "L'analyse s'est terminée en échec : consultez le journal."

# statut de commune -> (libellé, modificateur de badge)
STATUTS = {
    "lien_confirme": ("Lien trouvé", "success"),
    "lien_incertain": ("Lien probable", "new"),
    "aucun_lien_complet": ("Aucun lien (site exploré entièrement)", "info"),
    "aucun_lien_partiel": ("Aucun lien trouvé (exploration partielle)", "warning"),
    "inaccessible": ("Site inaccessible ou bloqué", "error"),
    "non_exploree": ("Plateforme non explorée", ""),
    "sans_site": ("Aucun site identifié", ""),
    "non_analysee": ("Non analysée", ""),
}
# statut d'une analyse (table des analyses) -> (libellé, modificateur de badge)
STATUTS_ANALYSE = {
    "file": ("En file d'attente", "info"),
    "en_cours": ("En cours", "info"),
    "terminee": ("Terminée", "success"),
    "echec": ("Échec", "error"),
    "annulee": ("Interrompue", "warning"),
}
# statut affiché sur la page de suivi et renvoyé par /api/etat
ETATS_ANALYSE = {"inactif": "Aucune analyse en cours", "en_cours": "En cours", "termine": "Terminée",
                 "echec": "Échec", "annule": "Arrêtée"}
ETAT_DE_ANALYSE = {"terminee": "termine", "echec": "echec", "annulee": "annule"}
MIME_CSV = "text/csv; charset=utf-8"
# Lecture publique (MLM_LECTURE_PUBLIQUE) : liste blanche des seules routes ouvertes sans mot de passe (GET et HEAD).
EN_TETES_PUBLICS = {"X-Robots-Tag": "noindex, nofollow", "Vary": "Authorization"}
MSG_PUBLIC_INDISPONIBLE = "Les résultats ne sont pas disponibles pour le moment. Réessayez plus tard."
CAMPAGNE_NUM = r"[0-9]{1,9}"     # numéro d'analyse dans une adresse : borné (hors bigint, la base répondrait en erreur)
PUBLIC_CAMPAGNES_MAX = 200       # analyses lues pour l'historique public, puis filtrées sur les analyses terminées


def static_file(rel: str) -> Path | None:
    """Fichier de public/static demandé, ou None (chemin suspect, type non servi, absent)."""
    rel = unquote(rel)
    if not rel or "\x00" in rel or "\\" in rel or any(part in ("", ".", "..") for part in rel.split("/")):
        return None
    try:
        f = (STATIC_RACINE / rel).resolve()
    except (OSError, ValueError):
        return None
    if f.suffix.lower() not in STATIC_MIME or not f.is_file() or not f.is_relative_to(STATIC_RACINE):
        return None
    return f


class Runner:
    """Une analyse à la fois, dans un processus séparé (usage local)."""

    def __init__(self, root: Path, command=None):
        self.root = root
        self.log_path = root / "data" / "web" / "derniere_analyse.log"
        self.command = command or (lambda args: [sys.executable, str(root / "src" / "mairie_link_monitor.py"),
                                                 "--root", str(root), "run", *args])
        self.proc: subprocess.Popen | None = None
        self.started = 0.0
        self.label = ""
        self.lock = threading.Lock()

    @property
    def analysis_id(self) -> int | None:
        """Pas d'identifiant d'analyse en local (les analyses ne sont pas enregistrées en base)."""
        return None

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, depts: list[str], resume: bool) -> str:
        with self.lock:
            if self.running():
                return "Une analyse est déjà en cours : attendez la fin ou arrêtez-la."
            args = ["--dept", *depts] + (["--confirm-all"] if depts == ["all"] else []) + (["--resume"] if resume else [])
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
            with self.log_path.open("w", encoding="utf-8") as out:
                self.proc = subprocess.Popen(self.command(args), cwd=self.root, stdout=out, stderr=subprocess.STDOUT,
                                             stdin=subprocess.DEVNULL, env=env)
            self.started, self.label = time.time(), " ".join(depts) + (" (reprise)" if resume else "")
            return ""

    def stop(self) -> None:
        # shortcut: arrêt brutal ; les sites lus avant le dernier point de sauvegarde (toutes les 100 communes) sont
        # conservés et une relance en reprise complète le reste.
        with self.lock:
            if self.running():
                self.proc.terminate()

    def tail(self, lines: int = 25) -> str:
        try:
            return "\n".join(self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
        except OSError:
            return ""


def parse_depts(text: str) -> list[str] | None:
    parts = [p for p in re.split(r"[\s,;]+", text.strip()) if p]
    if [p.lower() for p in parts] in (["tous"], ["all"]):
        return ["all"]
    depts = list(dict.fromkeys(normalize_dept(p) for p in parts))
    return depts if depts and all(DEPT_RE.match(d) for d in depts) else None


def sans_accents(texte: str) -> str:
    """Minuscules sans accents : comparaison insensible à la casse et aux accents."""
    decompose = unicodedata.normalize("NFD", texte)
    return "".join(c for c in decompose if unicodedata.category(c) != "Mn").casefold()


def url_resultats(dept: str, q: str, page_num: int) -> str:
    """Lien vers /resultats en conservant le filtre de département et la recherche (encodés, donc sûrs)."""
    params = {"page": str(page_num)}
    if dept:
        params["dept"] = dept
    if q:
        params["q"] = q
    return "/resultats?" + urlencode(params)


def pagination(dept: str, q: str, courante: int, total: int) -> str:
    """Barre de pagination DSFR. Chaque valeur saisie est échappée (le résultat est du HTML sûr)."""
    def lien(p: int, libelle: str, classe: str = "", attributs: str = "") -> str:
        href = html.escape(url_resultats(dept, q, p), quote=True)
        return f'<li><a class="fr-pagination__link{classe}" href="{href}"{attributs}>{libelle}</a></li>'

    items = []
    if courante > 1:
        items.append(lien(courante - 1, "Précédent", " fr-pagination__link--prev fr-pagination__link--lg-label"))
    trou = False
    for p in range(1, total + 1):
        if p in (1, total) or abs(p - courante) <= 2:
            trou = False
            if p == courante:
                items.append(lien(p, f'<span class="fr-sr-only">Page </span>{p}', attributs=' aria-current="page"'))
            else:
                items.append(lien(p, f'<span class="fr-sr-only">Page </span>{p}'))
        elif not trou:
            trou = True
            items.append("<li>…</li>")
    if courante < total:
        items.append(lien(courante + 1, "Suivant", " fr-pagination__link--next fr-pagination__link--lg-label"))
    return ('<nav class="fr-pagination" role="navigation" aria-label="Pagination">'
            f'<ul class="fr-pagination__list">{"".join(items)}</ul></nav>')


# ----------------------------------------------------------------- gabarits

def _est_http(url) -> bool:
    return isinstance(url, str) and urlsplit(url).scheme.lower() in ("http", "https")


def _liste(v) -> list[str]:
    return [] if not v else [v] if isinstance(v, str) else list(v)


def _tronquer(valeur, longueur: int = 70) -> str:
    """Affichage raccourci d'une URL longue ; la valeur complète reste dans l'attribut href."""
    texte = str(valeur or "")
    return texte if len(texte) <= longueur else texte[:longueur - 1] + "…"


def _libelle_depts(valeur) -> str:
    return ", ".join("toute la France" if c == "all" else c for c in _liste(valeur))


def _echappe_valeur(v) -> Markup:
    """Pour un attribut value="" : échappement complet (guillemets inclus), marqué sûr pour le gabarit."""
    return Markup(html.escape(str(v or ""), quote=True))


_ENV = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=True)
_ENV.filters["est_http"] = _est_http
_ENV.filters["date"] = lambda v: str(v or "")[:16].replace("T", " ")
_ENV.filters["depts"] = _libelle_depts
_ENV.filters["echappe"] = _echappe_valeur
_ENV.filters["tronquer"] = _tronquer
_ENV.globals.update(STATUTS=STATUTS, STATUTS_ANALYSE=STATUTS_ANALYSE, ETATS_ANALYSE=ETATS_ANALYSE)


def _rendre(modele: str, **ctx) -> str:
    return _ENV.get_template(f"web/{modele}.html.j2").render(**ctx)


def _rendre_public(modele: str, **ctx) -> str:
    """Pages publiques (lecture seule) : gabarits web/public, menu adaptatif selon `admin`."""
    return _ENV.get_template(f"web/public/{modele}.html.j2").render(**ctx)


def route_publique(chemin: str) -> tuple[str, str] | None:
    """Liste blanche des routes publiques : (genre, code) ou None. Tout chemin absent de la liste est protégé.

    Les rapports (/rapports...) et l'historique (/campagnes...) sont servis en vue assainie ; les vues d'administration
    sont sous /admin/ (protégées). /rapport/<dép> (ancienne adresse) redirige vers /rapports/<dép>.
    """
    if chemin == "/":
        return ("accueil", "")
    if chemin == "/a-propos":
        return ("apropos", "")
    if chemin == "/resultats.csv":
        return ("resultats_csv", "")
    if m := re.fullmatch(rf"/departement/({CODE_URL})(\.csv)?", chemin):
        return ("csv" if m.group(2) else "departement", m.group(1))
    if chemin == "/rapports":
        return ("rapports", "")
    if m := re.fullmatch(rf"/rapports/({CODE_URL})", chemin):
        return ("rapport", m.group(1))
    if m := re.fullmatch(rf"/rapports/({CODE_URL})/(communes\.csv|export\.html)", chemin):
        return ("rapport_csv" if m.group(2) == "communes.csv" else "rapport_html", m.group(1))
    if m := re.fullmatch(rf"/rapport/({CODE_URL})", chemin):
        return ("rapport_ancienne", m.group(1))
    if chemin == "/campagnes":
        return ("campagnes", "")
    if m := re.fullmatch(rf"/campagnes/({CAMPAGNE_NUM})", chemin):
        return ("campagne", m.group(1))
    return None


def _synthese():
    import synthese  # module écrit par l'agent « synthèse » ; import tardif
    return synthese


def _departements():
    import departements  # DEPARTEMENTS (code -> nom) ; import tardif
    return departements


def _pl(n: int, singulier: str, pluriel: str) -> str:
    return f"{n} {singulier if n == 1 else pluriel}"


def _phrase(n: int, x: int, y: int, z: int, w: int, s: int) -> str:
    n_a, n_ont = "n'a", "n'ont"
    t = (f"Sur {_pl(n, 'commune', 'communes')}, {x} {'a' if x == 1 else 'ont'} un lien vers Vos questions, "
         f"{y} {'est' if y == 1 else 'sont'} à vérifier, {z} {n_a if z == 1 else n_ont} pas de lien sur leur site, "
         f"{w} {n_a if w == 1 else n_ont} pas pu être {'analysée' if w == 1 else 'analysées'}.")
    return t + (f" {_pl(s, 'commune', 'communes')} sans site identifié." if s else "")


def vue_compte_rendu(synth: dict) -> dict:
    """Regroupe les communes en sections lisibles ; les valeurs brutes ne vont que dans les détails techniques."""
    syn = _synthese()
    cs = synth["communes"]
    liens, ecartes, verifier, groupes, aucun, sans_site = [], [], [], {}, [], []
    for c in cs:
        st = c["statut"]
        for l in c.get("liens_confirmes") or []:
            e = {"commune": c["nom"], "code_insee": c["code_insee"], "site_a_verifier": c.get("site_a_verifier"),
                 "page": l["url_page_source"], "texte": l["texte"] or l["url_cible"],
                 "verification": l.get("verification"), "remarque": l.get("remarque") or ""}
            if e["verification"] == "faux":
                ecartes.append(e)
            elif st == "lien_confirme":
                liens.append(e)
        if st == "lien_incertain":
            verifier.append({"commune": c["nom"], "raison": "Lien probable, à confirmer à la main : "
                             + _pl(c["nb_liens_incertains"], "élément", "éléments") + " sur le site."})
        elif st == "aucun_lien_partiel":
            raisons = [syn.libelle_limite(x) for s in c["sites"] for x in _liste(s.get("limites"))]
            if not raisons and any(_liste(s.get("erreurs")) for s in c["sites"]):
                raisons = ["certaines pages du site n'ont pas pu être lues"]
            verifier.append({"commune": c["nom"], "raison": "Aucun lien trouvé, mais la lecture du site est incomplète ("
                             + " ; ".join(dict.fromkeys(raisons or ["site non lu entièrement"])) + ")."})
        elif st in ("inaccessible", "non_exploree", "non_analysee"):
            cause = c.get("cause_principale") or next((s.get("cause") for s in c["sites"] if s.get("cause")), None)
            cause = {"non_exploree": "plateforme_non_exploree", "non_analysee": "sans_resultat"}.get(st, cause)
            groupes.setdefault(cause if cause in syn.CAUSES else "inconnue", []).append(c)
        elif st == "aucun_lien_complet":
            aucun.append(c["nom"])
        elif st == "sans_site":
            sans_site.append(c["nom"])
    ordre = list(syn.CAUSES)
    non_analysables = [{**syn.libelle_cause(k), "communes": groupes[k]} for k in sorted(groupes, key=ordre.index)]
    nb_non = sum(len(g["communes"]) for g in non_analysables)
    nb_liens = sum(c["statut"] == "lien_confirme" for c in cs)
    return {
        "n": len(cs), "nb_liens": nb_liens, "nb_verifier": len(verifier), "nb_aucun": len(aucun),
        "nb_non": nb_non, "nb_sans_site": len(sans_site),
        "phrase": _phrase(len(cs), nb_liens, len(verifier), len(aucun), nb_non, len(sans_site)),
        "liens": liens, "ecartes": ecartes, "verifier": verifier, "non_analysables": non_analysables,
        "aucun": aucun, "sans_site": sans_site,
        "communes_techniques": {
            "liens": [c for c in cs if c["statut"] == "lien_confirme"],
            "verifier": [c for c in cs if c["statut"] in ("lien_incertain", "aucun_lien_partiel")],
            "aucun": [c for c in cs if c["statut"] == "aucun_lien_complet"]},
    }


@functools.cache
def _css_autonome() -> str:
    """CSS DSFR inline pour l'export : polices retirées (lecture hors ligne), url() externes neutralisées."""
    css = (report.STATIC_DSFR / "dsfr" / "dsfr.min.css").read_text(encoding="utf-8")
    css = re.sub(r"@font-face\{[^}]*\}", "", css)
    # url() externes (icônes relatives) -> none ; data: et ancres #id (dans les SVG intégrés) conservés
    return re.sub(r"url\((?!\s*['\"]?(?:data:|#))[^)]*\)", "none", css)


def _neutraliser(valeur) -> str:
    """Valeur de cellule CSV : une formule (=, +, -, @, tabulation, retour) est précédée d'une apostrophe."""
    texte = "" if valeur is None else str(valeur)
    return "'" + texte if texte[:1] in ("=", "+", "-", "@", "\t", "\r") else texte


def csv_communes(synth: dict) -> bytes:
    """CSV des communes d'un département : séparateur « ; », UTF-8 avec BOM (Excel)."""
    tampon = io.StringIO()
    ecrivain = csv.writer(tampon, delimiter=";", lineterminator="\r\n")
    ecrivain.writerow(["Code INSEE", "Commune", "Statut", "Liens trouvés", "Détail"])
    for c in synth["communes"]:
        liens = " | ".join(l.get("url_cible") or "" for l in c.get("liens_confirmes") or [])
        ecrivain.writerow([_neutraliser(c.get("code_insee")), _neutraliser(c.get("nom")),
                           _neutraliser(STATUTS.get(c.get("statut"), (c.get("statut"), ""))[0]),
                           _neutraliser(liens), _neutraliser(c.get("detail"))])
    return tampon.getvalue().encode("utf-8-sig")


def horloge() -> datetime:
    """Heure courante (UTC). Fonction à part pour que les tests fixent le moment (fenêtre des nouveaux liens)."""
    return datetime.now(timezone.utc)


def est_recent(lien: dict, depuis: datetime) -> bool:
    """Lien (ou page) nouveau dont la première apparition est à `depuis` ou après. Les liens « de base » ne le sont jamais."""
    return bool(lien.get("nouveau")) and bool(lien.get("premiere_vue")) and instant_iso(lien["premiere_vue"]) >= depuis


def csv_liens(communes: list[dict]) -> bytes:
    """CSV des communes à lien confirmé : une ligne par COMMUNE (page source et cible retenues). Séparateur « ; »,
    UTF-8 avec BOM, chaque champ neutralisé contre les formules."""
    tampon = io.StringIO()
    ecrivain = csv.writer(tampon, delimiter=";", lineterminator="\r\n")
    ecrivain.writerow(CHAMPS_LIENS_CSV)
    noms = _departements().DEPARTEMENTS
    for c in communes:
        ecrivain.writerow([_neutraliser(c["dept"]), _neutraliser(noms.get(c["dept"], "")), _neutraliser(c["code_insee"]),
                           _neutraliser(c["nom_commune"]), _neutraliser(c["page"]), _neutraliser(c.get("cible") or ""),
                           _neutraliser(c.get("apparu_le") or "")])
    return tampon.getvalue().encode("utf-8-sig")


def parametres_liens(params: dict[str, list[str]], codes: list[str]) -> tuple[str, str, str, int]:
    """(q, dep, alerte, page) de l'accueil public. `dep` doit être un code publié, sinon il est ignoré (avec alerte) ;
    `page` est un entier positif, sinon 1. La recherche est tronquée à RECHERCHE_PUBLIQUE_MAX caractères."""
    q = params.get("q", [""])[0][:RECHERCHE_PUBLIQUE_MAX].strip()
    dep, alerte = params.get("dep", [""])[0].strip().upper(), ""
    if dep and not (re.fullmatch(CODE_URL, dep) and dep in codes):
        dep, alerte = "", "Département inconnu : le filtre par département n'est pas appliqué."
    brut = params.get("page", ["1"])[0].strip()
    page = int(brut) if re.fullmatch(r"[0-9]{1,6}", brut) else 1
    return q, dep, alerte, max(page, 1)


def url_liens(q: str, dep: str, page_num: int) -> str:
    """Adresse de l'accueil public avec les filtres et l'ancre du tableau (valeurs encodées)."""
    params = {"page": str(page_num)}
    if dep:
        params["dep"] = dep
    if q:
        params["q"] = q
    return "/?" + urlencode(params) + ANCRE_LIENS


def url_csv_liens(q: str, dep: str) -> str:
    params = {k: v for k, v in (("q", q), ("dep", dep)) if v}
    return "/resultats.csv" + ("?" + urlencode(params) if params else "")


def pagination_liens(q: str, dep: str, courante: int, total: int) -> str:
    """Pagination DSFR du tableau public : libellés accessibles, rel prev/next, ancre sur le titre de section."""
    def lien(p: int, texte: str, classes: str, attributs: str) -> str:
        href = html.escape(url_liens(q, dep, p), quote=True)
        return f'<li><a class="fr-pagination__link{classes}" href="{href}"{attributs}>{texte}</a></li>'

    items = []
    if courante > 1:
        items.append(lien(courante - 1, "Précédent", " fr-pagination__link--prev fr-pagination__link--lg-label",
                          f' rel="prev" aria-label="Page précédente, page {courante - 1}"'))
    trou = False
    for p in range(1, total + 1):
        if p in (1, total) or abs(p - courante) <= 2:
            trou = False
            attributs = f' aria-label="Page {p}"' + (' aria-current="page"' if p == courante else "")
            items.append(lien(p, str(p), "", attributs))
        elif not trou:
            trou = True
            items.append("<li>…</li>")
    if courante < total:
        items.append(lien(courante + 1, "Suivant", " fr-pagination__link--next fr-pagination__link--lg-label",
                          f' rel="next" aria-label="Page suivante, page {courante + 1}"'))
    return ('<nav class="fr-pagination" aria-label="Pagination des liens">'
            f'<ul class="fr-pagination__list">{"".join(items)}</ul></nav>')


def tranche_liens(source: PublicationSource, q: str, dep: str, numero: int) -> tuple[list[dict], int, int, int]:
    """(communes de la page, nombre de communes, page retenue, nombre de pages). Une page au-delà de la fin renvoie
    la dernière. La pagination compte des communes."""
    limite = LIENS_PAR_PAGE
    lignes, total = source.tous_les_liens(q, dep or None, limite, (numero - 1) * limite)
    pages = max(1, -(-total // limite))
    if numero > pages:
        numero = pages
        lignes, total = source.tous_les_liens(q, dep or None, limite, (numero - 1) * limite)
    return lignes, total, numero, pages


# ----------------------------------------------------------------- suivi (API /api/etat)

def _statut_analyse(source: PublicationSource, aid: int) -> str:
    try:
        row = source.analyse(aid)
    except SourceIndisponible:
        return ""
    return (row or {}).get("statut", "")


def _statut_local(runner) -> str:
    """Statut d'un sous-processus local terminé : 0 = terminée, arrêt demandé (signal ou 130) = arrêtée, sinon échec."""
    proc = getattr(runner, "proc", None)
    if proc is None:
        return "inactif"
    code = proc.returncode
    if code is None:
        return "en_cours"
    if code == 0:
        return "termine"
    return "annule" if code == 130 or code < 0 else "echec"


def etat_analyse(runner, source: PublicationSource) -> dict:
    """État de l'analyse en cours (ou de la dernière) pour /suivi et /api/etat. Fonctionne avec Runner et ActionsRunner."""
    actif = bool(runner.running())
    aid = getattr(runner, "analysis_id", None)
    traites = total = None
    for m in PROGRESSION_RE.finditer(runner.tail(200)):  # la dernière correspondance est la plus récente
        traites, total = int(m.group(1)), int(m.group(2))
    debut = float(getattr(runner, "started", 0.0) or 0.0)
    ecoule = restant = None
    if actif and debut:
        ecoule_brut = time.time() - debut
        ecoule = max(0, round(ecoule_brut))
        if traites and total is not None:
            restant = max(0, round(ecoule_brut / traites * (total - traites)))
    if actif:
        statut = "en_cours"
    elif aid is not None:
        statut = ETAT_DE_ANALYSE.get(_statut_analyse(source, aid), "inactif")
    else:
        statut = _statut_local(runner)
    message = f"Analyse en cours : {getattr(runner, 'label', '')}".strip() if actif else ""
    return {"actif": actif, "genre": "campagne", "statut": statut, "campagne_id": aid,
            "progression": {"sites_traites": traites, "sites_total": total, "pages_visitees": None,
                            "liens_confirmes": None, "ecoule_s": ecoule, "restant_estime_s": restant},
            "message": message, "erreur": MSG_ECHEC if statut == "echec" else None, "fichiers": []}


# ----------------------------------------------------------------- pages de données

def _chiffres(resumes: list[dict] | None) -> list[tuple[str, str]]:
    """Chiffres de l'accueil ; « — » quand les données sont indisponibles."""
    def somme(cle: str) -> int:
        return sum(int(r.get(cle) or 0) for r in resumes)

    if resumes is None:
        valeurs = ["—", "—", "—", "—"]
    else:
        valeurs = [len(resumes), somme("nb_communes"), somme("nb_communes_avec_lien"), somme("nb_communes_a_verifier")]
    return list(zip(valeurs, ["départements publiés", "communes", "communes avec un lien", "communes à vérifier"]))


def filtrer_departements(resumes: list[dict], q: str) -> list[dict]:
    """Départements dont le code ou le nom contient `q` (sans accents ni casse)."""
    cle = sans_accents(q)
    return [r for r in resumes if not cle or cle in sans_accents(str(r["code"])) or cle in sans_accents(str(r.get("nom", "")))]


def derniere_publication(resumes: list[dict]) -> str:
    """Date (ISO) la plus récente parmi les départements publiés ; '' s'il n'y en a aucune."""
    return max((str(r.get("date") or "") for r in resumes), default="")


def resumes_publies(source: PublicationSource) -> list[dict]:
    """Résumés des départements publiés (code conforme à CODE_URL), triés par code."""
    return [r for r in source.resumes() if re.fullmatch(CODE_URL, str(r.get("code", "")))]


# ----------------------------------------------------------------- historique public des analyses

def campagnes_terminees(source: PublicationSource, limite: int) -> list[dict]:
    """Analyses terminées uniquement : en file, en cours, annulées ou en échec, elles ne sont jamais publiques."""
    return [a for a in source.analyses(limite) if a.get("statut") == "terminee"]


def campagne_publique(a: dict) -> dict:
    """Forme assainie d'une analyse : numéro, dates et périmètre. Ni journal, ni identifiant de run, ni acteur."""
    return {"id": int(a["id"]), "debut": a.get("debut") or "", "fin": a.get("fin") or "",
            "departements": _liste(a.get("departements"))}


def depts_rattaches(a: dict, publies: list[dict]) -> list[dict]:
    """Départements publiés rattachés à une analyse : dans son périmètre et dont la dernière publication tombe dans sa
    période (jours inclusifs). Les synthèses ne portent pas d'identifiant d'analyse : le rattachement se fait par dates."""
    perimetre = _liste(a.get("departements"))
    debut = str(a.get("debut") or "")[:10]
    fin = str(a.get("fin") or a.get("debut") or "")[:10]
    return [r for r in publies
            if ("all" in perimetre or r["code"] in perimetre) and debut <= str(r.get("date") or "")[:10] <= fin]


def totaux_par_statut(source: PublicationSource, depts: list[dict]) -> dict[str, int]:
    """Nombre de communes par statut, additionné sur les départements rattachés (une synthèse par département)."""
    totaux = dict.fromkeys(STATUTS, 0)
    for r in depts:
        for statut, n in ((source.synthese(r["code"]) or {}).get("totaux") or {}).items():
            if statut in totaux:
                totaux[statut] += int(n or 0)
    return totaux


def resultats(source: PublicationSource, params: dict[str, list[str]]) -> str:
    """Page « Résultats » : communes avec lien, filtrées par département et recherche, paginées."""
    dept = params.get("dept", [""])[0].strip()
    if dept and not DEPT_RE.match(dept):
        dept = ""
    q = params.get("q", [""])[0][:RECHERCHE_MAX].strip()
    try:
        numero = int(params.get("page", ["1"])[0])
    except ValueError:
        numero = 1
    numero = max(numero, 1)

    base = source.liens(dept or None)
    cle = sans_accents(q)
    lignes = [ligne for ligne in base
              if not cle or cle in sans_accents(ligne["nom_commune"]) or cle in sans_accents(ligne["code_insee"])]
    total = max(1, -(-len(lignes) // RESULTATS_PAR_PAGE))
    numero = min(numero, total)
    debut = (numero - 1) * RESULTATS_PAR_PAGE
    return _rendre("resultats", actif="resultats", libelle=LIBELLE_RESULTATS, dept=dept, q=q, depts=source.depts(),
                   n=len(lignes), base_vide=not base, lignes=lignes,
                   tranche=lignes[debut:debut + RESULTATS_PAR_PAGE],
                   pagination=pagination(dept, q, numero, total) if total > 1 else "")


# ----------------------------------------------------------------- serveur

def make_handler(root: Path, runner, password: str, source: PublicationSource | None = None,
                 lecture_publique: bool = False):
    source = source or FileSource(root)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            log.info("%s %s", self.address_string(), fmt % args)

        publique = False  # vrai pour une route de la liste blanche, lecture publique activée

        def parse_request(self) -> bool:
            """Routes publiques : 405 pour tout verbe autre que GET et HEAD (aucun formulaire, même avec Origin valide)."""
            if not super().parse_request():
                return False
            self.publique = lecture_publique and route_publique(urlsplit(self.path).path) is not None
            if self.publique and self.command not in ("GET", "HEAD"):
                self.close_connection = True
                self.send(405, "Méthode non autorisée.", "text/plain; charset=utf-8", {"Allow": "GET, HEAD"})
                return False
            return True

        # --- réponses ---

        def send(self, status: int, body: str | bytes, ctype: str = "text/html; charset=utf-8", extra: dict | None = None):
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(status)
            entetes = {"Content-Type": ctype, "Content-Length": str(len(data)), "X-Content-Type-Options": "nosniff",
                       "Cache-Control": "no-store", **EN_TETES_SECURITE}
            if self.publique:
                entetes.update(EN_TETES_PUBLICS)  # noindex ; réponse dépendant de l'Authorization (pas de cache partagé)
            entetes.update(extra or {})
            for k, v in entetes.items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def page(self, modele: str, status: int = 200, **ctx):
            self.send(status, _rendre(modele, **ctx))

        def erreur(self, status: int, titre: str, message: str, extra: dict | None = None):
            self.send(status, _rendre("erreur", code=status, titre=titre, message=message, actif=""), extra=extra)

        def indisponible(self):
            self.erreur(503, "Service indisponible", "Base de données indisponible : réessayez plus tard.")

        def redirect(self, location: str):
            self.send(303, "", "text/plain; charset=utf-8", {"Location": location})

        def csv_reponse(self, data: bytes, nom: str, en_tetes: dict | None = None):
            extra = {"Content-Disposition": f'attachment; filename="{nom}"', **(en_tetes or {})}
            if len(data) > CSV_GZIP_MIN_OCTETS and self.accepte_gzip():
                # Publique : la réponse garde « Vary: Authorization » en plus d'Accept-Encoding (pas de cache partagé).
                vary = "Accept-Encoding, Authorization" if self.publique else "Accept-Encoding"
                return self.send(200, gzip.compress(data, mtime=0), MIME_CSV,
                                 {**extra, "Content-Encoding": "gzip", "Vary": vary})
            return self.send(200, data, MIME_CSV, extra)

        # --- contrôles communs ---

        def identifiants_valides(self) -> bool:
            """Vrai sans mot de passe configuré, ou si l'en-tête Authorization porte le bon mot de passe."""
            if not password:
                return True
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Basic "):
                try:
                    given = base64.b64decode(auth[6:]).decode("utf-8").split(":", 1)[1]
                except (ValueError, IndexError):
                    given = ""
                if hmac.compare_digest(given.encode(), password.encode()):
                    return True
            return False

        def authorized(self) -> bool:
            if self.identifiants_valides():
                return True
            self.erreur(401, "Accès protégé", "Mot de passe requis.",
                        extra={"WWW-Authenticate": 'Basic realm="Liens Vos questions", charset="UTF-8"'})
            return False

        def same_origin(self) -> bool:
            """Refuse un formulaire envoyé depuis un autre site (le navigateur joindrait le mot de passe)."""
            origin = self.headers.get("Origin") or self.headers.get("Referer") or ""
            return bool(origin) and urlsplit(origin).netloc == self.headers.get("Host", "")

        def accepte_gzip(self) -> bool:
            """Vrai si Accept-Encoding propose gzip (ou x-gzip) avec un q différent de 0."""
            for element in self.headers.get("Accept-Encoding", "").split(","):
                nom, _, params = element.partition(";")
                if nom.strip().lower() in ("gzip", "x-gzip") and not re.search(r"q\s*=\s*0(\.0*)?\s*$", params.strip()):
                    return True
            return False

        def serve_static(self, path: str):
            f = static_file(path[len(STATIC_URL):])
            if f is None:
                return self.send(404, "Introuvable", "text/plain; charset=utf-8")
            return self.send(200, f.read_bytes(), STATIC_MIME[f.suffix.lower()],
                             {"Cache-Control": "public, max-age=86400"})

        # --- GET ---

        def do_GET(self):  # noqa: N802
            chemin = urlsplit(self.path).path
            if chemin.startswith(STATIC_URL):  # sans mot de passe : ressources publiques uniquement
                return self.serve_static(chemin)
            if self.publique:  # liste blanche : sans mot de passe ; un identifiant valide donne la vue d'administration
                return self.traiter_publique(chemin, parse_qs(urlsplit(self.path).query, keep_blank_values=True))
            if not self.authorized():
                return
            try:
                self.routes_get(chemin, parse_qs(urlsplit(self.path).query, keep_blank_values=True))
            except SourceIndisponible:
                self.indisponible()
            except Exception:  # noqa: BLE001 - jamais de trace Python côté navigateur
                log.exception("erreur sur GET %s", chemin)
                self.erreur(500, "Erreur interne", "Une erreur interne s'est produite. Consultez le journal.")

        def routes_get(self, chemin: str, params: dict):
            if chemin == "/":
                return self.accueil()
            if chemin == "/tableau-de-bord":  # ancien tableau de bord de la racine, protégé (lecture publique ou non)
                return self.accueil(actif="tableau-de-bord")
            if chemin == "/analyse":
                return self.page_analyse()
            if chemin == "/suivi":
                return self.page("suivi", actif="suivi", etat=etat_analyse(runner, source))
            if chemin == "/api/etat":
                return self.send(200, json.dumps(etat_analyse(runner, source)), "application/json; charset=utf-8",
                                 {"Cache-Control": "no-store"})
            # Sans lecture publique, ces adresses sont protégées et montrent la vue d'administration complète. Avec la
            # lecture publique, /rapports et /campagnes sont servies par traiter_publique ; /admin/ reste ici.
            if chemin == "/rapports":
                return self.page("rapports", actif="rapports", resumes=source.resumes())
            if chemin == "/resultats":
                return self.send(200, resultats(source, params))
            if chemin in ("/campagnes", "/admin/campagnes"):
                return self.page("campagnes", actif="campagnes", analyses=source.analyses(50))
            if m := re.fullmatch(rf"/(?:admin/)?campagnes/({CAMPAGNE_NUM})", chemin):
                return self.page_analyse_detail(int(m.group(1)))
            if m := re.fullmatch(rf"/admin/rapports/({CODE_URL})", chemin):  # vue d'administration complète
                return self.compte_rendu(m.group(1), None)
            if m := re.fullmatch(rf"/rapports/({CODE_URL})(?:/(export\.html|communes\.csv))?", chemin):
                return self.compte_rendu(m.group(1), m.group(2))
            if m := re.fullmatch(rf"/rapport/({CODE_URL})", chemin):  # ancienne adresse
                if m.group(1) in source.depts():
                    return self.redirect(f"/rapports/{m.group(1)}")
            if m := re.fullmatch(rf"/csv/({CODE_URL}|tous)", chemin):
                return self.csv_publie(m.group(1))
            self.erreur(404, "Page introuvable", "Cette page n'existe pas.")

        def accueil(self, actif: str = "accueil"):
            chiffres, derniere, alerte = _chiffres(None), None, ""
            try:
                chiffres = _chiffres(source.resumes())
                derniere = (source.analyses(1) or [None])[0]
            except SourceIndisponible:
                alerte = "Base de données indisponible : les chiffres ne peuvent pas être affichés pour le moment."
            self.page("accueil", actif=actif, chiffres=chiffres, derniere=derniere, alerte=alerte,
                      analyse_en_cours=runner.running())

        def page_analyse(self, status: int = 200, erreur: str = ""):
            alerte, resumes, reprises = "", {}, []
            try:
                resumes = {r["code"]: r for r in source.resumes()}
                reprises = [a for a in source.analyses(50) if a.get("statut") in ("annulee", "echec")]
            except SourceIndisponible:
                alerte = ("Base de données indisponible : le nombre de communes par département et les analyses "
                          "interrompues ne peuvent pas être affichés.")
            departements = [{"code": c, "nom": nom, "nb_communes": (resumes.get(c) or {}).get("nb_communes")}
                            for c, nom in sorted(_departements().DEPARTEMENTS.items())]
            self.page("analyse", status, actif="analyse", erreur=erreur, alerte=alerte,
                      departements=departements, reprises=reprises)

        def page_analyse_detail(self, aid: int):
            a = source.analyse(aid)
            if a is None:
                return self.erreur(404, "Page introuvable", "Aucune analyse ne porte ce numéro.")
            noms = _departements().DEPARTEMENTS
            depts = []
            for c in _liste(a.get("departements")):
                if c == "all":
                    depts.append({"code": c, "nom": "", "libelle": "Toute la France", "lien": False})
                else:
                    depts.append({"code": c, "nom": noms.get(c, ""), "libelle": c,
                                  "lien": bool(re.fullmatch(CODE_URL, c))})
            self.page("campagne", actif="campagnes", a=a, depts=depts)

        def compte_rendu(self, code: str, suffixe: str | None):
            synth = source.synthese(code)
            if synth is None:
                return self.erreur(404, "Page introuvable", "Aucune analyse pour ce département.")
            if suffixe == "communes.csv":
                return self.csv_reponse(csv_communes(synth), f"communes_{code}.csv")
            if suffixe == "export.html":
                corps = _rendre("export", synth=synth, vue=vue_compte_rendu(synth), css=_css_autonome(), autonome=True)
                return self.send(200, corps, extra={"Content-Disposition": f"attachment; filename=compte-rendu_{code}.html"})
            self.page("departement", actif="rapports", synth=synth, vue=vue_compte_rendu(synth), autonome=False,
                      filtre=True, url_html=f"/rapports/{code}/export.html", url_csv=f"/rapports/{code}/communes.csv")

        def csv_publie(self, code: str):
            try:
                if code == "tous":
                    data, nom = source.csv_all(), "liens_tous_departements.csv"
                else:
                    pub = source.get(code, report=False)
                    data, nom = (pub.csv_bytes if pub and pub.csv_present else None), f"liens_{code}.csv"
            except SourceIndisponible:
                return self.indisponible()
            if data is None:
                return self.erreur(404, "Page introuvable", "Aucun fichier CSV pour ce département.")
            return self.csv_reponse(data, nom)

        # --- pages publiques (lecture seule) ---

        def do_HEAD(self):  # noqa: N802
            chemin = urlsplit(self.path).path
            if not self.publique:
                return self.send_error(501, f"Unsupported method ({self.command!r})")
            self.traiter_publique(chemin, parse_qs(urlsplit(self.path).query, keep_blank_values=True))

        def traiter_publique(self, chemin: str, params: dict):
            admin = self.identifiants_valides()
            genre, code = route_publique(chemin)
            try:
                if genre == "accueil":  # mêmes résultats pour tous ; l'identifiant valide ajoute le menu complet
                    return self.public_accueil(params, admin)
                if genre == "apropos":
                    return self.public_apropos(admin)
                if genre == "resultats_csv":
                    return self.public_liens_csv(params)
                if genre in ("departement", "csv"):
                    return self.public_departement(code, admin, csv=genre == "csv")
                if genre == "rapports":
                    return self.public_rapports(admin)
                if genre == "rapport_ancienne":
                    return self.public_rapport_ancienne(code, admin)
                if genre in ("rapport", "rapport_csv", "rapport_html"):
                    return self.public_rapport(code, admin, genre)
                if genre == "campagnes":
                    return self.public_campagnes(admin)
                return self.public_campagne(int(code), admin)
            except SourceIndisponible:
                self.public_erreur(503, "Service indisponible", MSG_PUBLIC_INDISPONIBLE, admin)
            except Exception:  # noqa: BLE001 - jamais de trace Python côté visiteur
                log.exception("erreur sur page publique %s", chemin)
                self.public_erreur(500, "Erreur interne", "Une erreur interne s'est produite.", admin)

        def page_publique(self, modele: str, admin: bool, status: int = 200, **ctx):
            self.send(status, _rendre_public(modele, admin=admin, **ctx))

        def public_accueil(self, params: dict, admin: bool):
            publies = [r for r in source.resumes() if re.fullmatch(CODE_URL, str(r.get("code", "")))]
            q, dep, alerte, numero = parametres_liens(params, [r["code"] for r in publies])
            lignes, total_liens, numero, pages = tranche_liens(source, q, dep, numero)
            depuis = horloge() - timedelta(days=FENETRE_NOUVEAUX_JOURS)
            nouveaux, total_nouveaux = source.nouveaux_liens(depuis, NOUVEAUX_AFFICHES_MAX)  # communes
            noms = _departements().DEPARTEMENTS
            totaux = {cle: sum(int(r.get(cle) or 0) for r in publies) for cle in CHAMPS_TOTAUX}
            self.page_publique(
                "accueil", admin, actif="accueil", total=len(publies), resumes=filtrer_departements(publies, q),
                q=q, derniere=derniere_publication(publies), totaux=totaux,
                nouveaux=[{"apparu_le": c["apparu_le"], "departement": f"{noms.get(c['dept'], '')} ({c['dept']})",
                           "code_insee": c["code_insee"], "commune": c["nom_commune"], "page": c["page"]}
                          for c in nouveaux],
                total_nouveaux=total_nouveaux, fenetre=FENETRE_NOUVEAUX_JOURS, nouveaux_max=NOUVEAUX_AFFICHES_MAX,
                liens=[{"departement": f"{noms.get(c['dept'], '')} ({c['dept']})", "code_insee": c["code_insee"],
                        "commune": c["nom_commune"], "page": c["page"], "cible": c["cible"],
                        "apparu_le": c["apparu_le"],
                        "recent": est_recent({"nouveau": c["nouveau"], "premiere_vue": c["apparu_le"]}, depuis)}
                       for c in lignes],
                liens_total=total_liens, liens_page=numero, liens_pages=pages, dep=dep, alerte_liens=alerte,
                liens_pagination=Markup(pagination_liens(q, dep, numero, pages)) if pages > 1 else "",
                depts_choix=[{"code": r["code"], "libelle": f"{r['code']} {noms.get(r['code'], r.get('nom', ''))}"}
                             for r in publies],
                url_csv=url_csv_liens(q, dep))

        def public_liens_csv(self, params: dict):
            q, dep, _alerte, _numero = parametres_liens(params, source.depts())
            lignes, total = source.tous_les_liens(q, dep or None, LIENS_CSV_MAX, 0)
            en_tetes = {"X-Resultats-Tronques": "oui"} if total > LIENS_CSV_MAX else {}
            return self.csv_reponse(csv_liens(lignes), "liens_confirmes.csv", en_tetes)

        def public_apropos(self, admin: bool):
            try:
                derniere = derniere_publication(source.resumes())
            except SourceIndisponible:
                derniere = None  # la page À propos reste lisible ; la date est signalée comme indisponible
            self.page_publique("apropos", admin, actif="apropos", derniere=derniere)

        def public_departement(self, code: str, admin: bool, csv: bool):
            synth = source.synthese(code)
            if synth is None:
                return self.public_erreur(404, "Page introuvable", "Aucun résultat publié pour ce département.", admin)
            if csv:
                return self.csv_reponse(csv_communes(synth), f"communes_{code}.csv")
            self.page_publique("departement", admin, actif="", synth=synth, vue=vue_compte_rendu(synth), autonome=True,
                               lecture=True, url_csv=f"/departement/{code}.csv", url_html=None,
                               retour=("/", "Retour à la liste des départements"), sans_exploitation=False)

        def public_rapports(self, admin: bool):
            publies = resumes_publies(source)
            self.page_publique("rapports", admin, actif="rapports", resumes=publies, derniere=derniere_publication(publies),
                               sans_exploitation=True)

        def public_rapport_ancienne(self, code: str, admin: bool):
            """/rapport/<dép> : ancienne adresse, redirigée vers /rapports/<dép> quand le département est publié."""
            if code in source.depts():
                return self.redirect(f"/rapports/{code}")
            return self.public_erreur(404, "Page introuvable", "Aucun résultat publié pour ce département.", admin)

        def public_rapport(self, code: str, admin: bool, genre: str):
            """Rapport d'un département (vue assainie, identique pour tous) ; CSV et export HTML autonome."""
            synth = source.synthese(code)
            if synth is None:
                return self.public_erreur(404, "Page introuvable", "Aucun résultat publié pour ce département.", admin)
            if genre == "rapport_csv":
                return self.csv_reponse(csv_communes(synth), f"communes_{code}.csv")
            if genre == "rapport_html":
                corps = _rendre("export", synth=synth, vue=vue_compte_rendu(synth), css=_css_autonome(), autonome=True,
                                lecture=True)
                return self.send(200, corps, extra={"Content-Disposition": f"attachment; filename=compte-rendu_{code}.html"})
            self.page_publique("departement", admin, actif="rapports", synth=synth, vue=vue_compte_rendu(synth), autonome=True,
                               lecture=True, url_csv=f"/rapports/{code}/communes.csv",
                               url_html=f"/rapports/{code}/export.html",
                               retour=("/rapports", "Retour à la liste des rapports"), sans_exploitation=True)

        def public_campagnes(self, admin: bool):
            terminees = campagnes_terminees(source, PUBLIC_CAMPAGNES_MAX)
            self.page_publique("campagnes", admin, actif="campagnes", analyses=[campagne_publique(a) for a in terminees],
                               derniere=derniere_publication(resumes_publies(source)), sans_exploitation=True)

        def public_campagne(self, aid: int, admin: bool):
            a = source.analyse(aid)
            if a is None or a.get("statut") != "terminee":  # en file, en cours, annulée ou en échec : invisible
                return self.public_erreur(404, "Page introuvable", "Aucune analyse terminée ne porte ce numéro.", admin)
            publies = resumes_publies(source)
            rattaches = depts_rattaches(a, publies)
            self.page_publique("campagne", admin, actif="campagnes", a=campagne_publique(a), rattaches=rattaches,
                               nb_communes=sum(int(r.get("nb_communes") or 0) for r in rattaches),
                               totaux=totaux_par_statut(source, rattaches), derniere=derniere_publication(publies),
                               sans_exploitation=True)

        def public_erreur(self, status: int, titre: str, message: str, admin: bool):
            self.page_publique("erreur", admin, status, code=status, titre=titre, message=message, actif="", derniere=None)

        # --- POST ---

        def do_POST(self):  # noqa: N802
            path = urlsplit(self.path).path
            if not self.authorized():
                return
            if not self.same_origin():
                return self.erreur(403, "Requête refusée", "Requête venant d'un autre site : formulaire refusé.")
            longueur = self.headers.get("Content-Length", "0")
            if not re.fullmatch(r"[0-9]{1,5}", longueur) or int(longueur) > POST_MAX_OCTETS:
                # Refus avant toute lecture du corps.
                return self.erreur(400, "Requête refusée", "Taille de formulaire invalide.")
            form = parse_qs(self.rfile.read(int(longueur)).decode("utf-8", "replace"))
            try:
                self.post(path, form)
            except SourceIndisponible:
                self.indisponible()
            except Exception:  # noqa: BLE001
                log.exception("erreur sur POST %s", path)
                self.erreur(500, "Erreur interne", "Une erreur interne s'est produite. Consultez le journal.")

        def post(self, path: str, form: dict):
            if path == "/arreter":
                runner.stop()
                return self.redirect("/suivi")
            if path in ("/analyse", "/lancer"):  # « /lancer » : ancienne adresse du formulaire, encore acceptée
                return self.lancer(form)
            if m := re.fullmatch(r"/reprendre/(\d+)", path):
                return self.reprendre(int(m.group(1)))
            self.erreur(404, "Page introuvable", "Cette action n'existe pas.")

        def lancer(self, form: dict):
            texte = "tous" if "tous" in form else " ".join(form.get("departement", []) + form.get("dept", []))
            if not texte.strip():
                return self.page_analyse(400, "Choisissez au moins un département.")
            depts = parse_depts(texte)
            if depts is None:
                return self.page_analyse(400, "Département(s) invalide(s) : utilisez des codes comme 60, 01, 2A, 971.")
            if depts == ["all"] and (form.get("confirmer_tous") or [""])[0] != "1":
                return self.page_analyse(400, "Pour analyser toute la France, cochez la case de confirmation.")
            self.demarrer(depts, (form.get("reprise") or [""])[0] == "1")

        def reprendre(self, aid: int):
            row = source.analyse(aid)
            if row is None or row.get("statut") not in ("annulee", "echec"):
                return self.erreur(404, "Page introuvable", "Aucune analyse interrompue avec cet identifiant.")
            depts = parse_depts(" ".join(_liste(row.get("departements"))))
            if depts is None:
                return self.erreur(400, "Requête refusée", "Les départements de cette analyse ne sont pas valides.")
            self.demarrer(depts, True)

        def demarrer(self, depts: list[str], reprise: bool):
            erreur = runner.start(depts, reprise)
            if erreur:
                return self.page_analyse(409, erreur)
            self.redirect("/suivi")

    return Handler


def serve(root: Path, host: str, port: int, password: str, runner: Runner | None = None,
          source: PublicationSource | None = None, lecture_publique: bool = False) -> ThreadingHTTPServer:
    if host not in LOCAL_HOSTS and not password:
        raise ValueError("Pour partager l'application sur le réseau, définissez un mot de passe "
                         "(config.toml : mot_de_passe_web) ; sans mot de passe, elle n'écoute que sur ce poste.")
    return ThreadingHTTPServer((host, port), make_handler(root, runner or Runner(root), password, source,
                                                          lecture_publique=lecture_publique))
