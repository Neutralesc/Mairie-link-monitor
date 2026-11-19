"""Application web : lancer une analyse, suivre son avancement, consulter comptes rendus, CSV et changements.

Bibliothèque standard uniquement. Une analyse à la fois, exécutée dans un processus séparé (le programme en ligne de
commande habituel) ; son journal est écrit dans data/web/derniere_analyse.log.
Sécurité : par défaut l'application n'écoute que sur ce poste (127.0.0.1). Pour la partager sur le réseau, un mot de
passe est obligatoire (authentification HTTP Basic) ; les formulaires refusent les requêtes venant d'un autre site.
"""
from __future__ import annotations

import base64
import gzip
import hmac
import logging
import os
import re
import subprocess
import sys
import threading
import time
import unicodedata
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

import report
from layout import normalize_dept
from publications import DEPT_RE, FileSource, PublicationSource, SourceIndisponible

log = logging.getLogger("webapp")
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
# Ressources DSFR servies sans mot de passe (le reste du site garde l'authentification).
STATIC_URL = "/static/dsfr/"
STATIC_DIR = report.STATIC_DSFR
STATIC_MIME = {".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".woff2": "font/woff2",
               ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon",
               ".webmanifest": "application/manifest+json"}
# En-têtes de sécurité de toutes les réponses. Le DSFR est servi depuis le même hôte : aucune ressource externe.
POLITIQUE_CONTENU = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                     "img-src 'self' data:; font-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'")
EN_TETES_SECURITE = {"X-Frame-Options": "DENY", "Referrer-Policy": "same-origin",
                     "Content-Security-Policy": POLITIQUE_CONTENU}
POST_MAX_OCTETS = 10_000       # taille maximale d'un formulaire
CSV_GZIP_MIN_OCTETS = 64 * 1024  # au-delà, un CSV est compressé si le client accepte gzip


def static_file(rel: str) -> Path | None:
    """Fichier de public/static/dsfr demandé, ou None (chemin suspect, type non servi, absent)."""
    rel = unquote(rel)
    if not rel or "\x00" in rel or "\\" in rel or any(part in ("", ".", "..") for part in rel.split("/")):
        return None
    try:
        f = (STATIC_DIR / rel).resolve()
    except (OSError, ValueError):
        return None
    if f.suffix.lower() not in STATIC_MIME or not f.is_file() or not f.is_relative_to(STATIC_DIR):
        return None
    return f


class Runner:
    """Une analyse à la fois, dans un processus séparé."""

    def __init__(self, root: Path, command=None):
        self.root = root
        self.log_path = root / "data" / "web" / "derniere_analyse.log"
        self.command = command or (lambda args: [sys.executable, str(root / "src" / "mairie_link_monitor.py"),
                                                 "--root", str(root), "run", *args])
        self.proc: subprocess.Popen | None = None
        self.started = 0.0
        self.label = ""
        self.lock = threading.Lock()

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


def known_depts(root: Path) -> list[str]:
    return FileSource(root).depts()


def home(root: Path, runner: Runner, message: str = "", source: PublicationSource | None = None) -> str:
    source = source or FileSource(root)
    run = runner.running()
    parts = ["<h1>Liens vers « Vos questions » sur les sites des mairies</h1>",
             '<p class="fr-text--lead">Recherche des mairies dont le site ou la page PanneauPocket renvoie vers '
             "vosquestions.ecologie-territoires.gouv.fr</p>"]
    if message:
        parts.append(f'<div class="fr-alert fr-alert--error fr-mb-4w" role="alert"><p>{escape(message)}</p></div>')
    if run:
        minutes = int((time.time() - runner.started) // 60)
        parts.append(f'<section class="fr-mb-4w" aria-live="polite"><h2>Analyse en cours : {escape(runner.label)}</h2>'
                     f"<p>Démarrée il y a {minutes} min. Utilisez « Actualiser » pour voir l'avancement.</p>"
                     f'<pre class="mlm-journal">{escape(runner.tail())}</pre>'
                     '<form method="post" action="/arreter"><button class="fr-btn fr-btn--secondary" type="submit">'
                     "Arrêter l'analyse</button></form>"
                     '<p class="fr-hint-text">Une analyse arrêtée peut être reprise : cochez « Reprendre » au prochain lancement.</p>'
                     '<p><a class="fr-btn fr-btn--secondary" href="/">Actualiser</a></p>'
                     '<div class="fr-checkbox-group"><input id="actualiser-auto" type="checkbox">'
                     '<label class="fr-label" for="actualiser-auto">Actualiser automatiquement toutes les 10 secondes</label></div>'
                     '<script>(function(){var c=document.getElementById("actualiser-auto"),t=null;'
                     "var k=\"mlm-actualiser\";try{c.checked=sessionStorage.getItem(k)===\"1\";}catch(e){}"
                     "function m(){clearInterval(t);t=null;try{sessionStorage.setItem(k,c.checked?\"1\":\"0\");}catch(e){}"
                     "if(c.checked){t=setInterval(function(){location.reload();},10000);}}"
                     'c.addEventListener("change",m);m();})();</script>'
                     "</section>")
    else:
        parts.append('<section class="fr-mb-4w"><h2>Lancer une analyse</h2>'
                     '<form method="post" action="/lancer">'
                     '<div class="fr-input-group"><label for="dept"><span class="fr-label">Département(s)</span></label>'
                     '<input class="fr-input" id="dept" name="dept" type="text" required '
                     'placeholder="ex : 60   ou   60 80 2A" aria-describedby="aide-dept">'
                     '<span id="aide-dept" class="fr-hint-text">Codes séparés par des espaces. « tous » = France entière (très long).</span>'
                     "</div>"
                     '<div class="fr-checkbox-group"><input id="reprise" name="reprise" type="checkbox" value="1">'
                     '<label class="fr-label" for="reprise">Reprendre l\'analyse précédente '
                     "(ne refait que ce qui manque ou a échoué : plus rapide)</label></div>"
                     '<div class="fr-checkbox-group"><input id="confirmer_tous" name="confirmer_tous" type="checkbox" value="1">'
                     '<label class="fr-label" for="confirmer_tous">Je confirme l\'analyse de toute la '
                     "France (si « tous »)</label></div>"
                     '<button class="fr-btn" type="submit">Lancer</button></form>'
                     '<p class="fr-hint-text">Durée indicative : de quelques minutes à une heure par département.</p></section>')
        if runner.tail():
            parts.append(f'<details class="fr-mb-4w"><summary>Journal de la dernière analyse</summary>'
                         f'<pre class="mlm-journal">{escape(runner.tail(60))}</pre></details>')
    rows, csv_global, indisponible = [], False, False
    try:
        for d in source.depts():
            pub = source.get(d, report=False, csv=False)
            if pub is None:  # département disparu entre la liste et la lecture
                continue
            has_control = bool(pub.controle_date)
            sr = f'<span class="fr-sr-only"> du département {escape(d)}</span>'
            csv_link = f'<a class="fr-link" href="/csv/{d}">CSV{sr}</a>' if pub.csv_present else ""
            rows.append([f"<b>{escape(d)}</b>", escape(report.fr_date(pub.controle_date)) if has_control else "—",
                         str(pub.avec_lien) if has_control else "—",
                         escape(pub.resume_changements) if has_control else "—",
                         f'<a class="fr-link" href="/rapport/{d}">Compte rendu{sr}</a> {csv_link}'])
        csv_global = source.has_csv_all()
    except SourceIndisponible:
        rows, csv_global, indisponible = [], False, True
    parts.append("<h2>Résultats par département</h2>")
    if indisponible:
        parts.append('<div class="fr-alert fr-alert--warning fr-mb-4w"><p>Base de données indisponible : les départements '
                     "ne peuvent pas être affichés pour le moment.</p></div>")
    else:
        parts.append(report.table(["Département", "Dernier contrôle", "Communes avec lien", "Changements", "Ouvrir"], rows,
                                  "Aucun département analysé pour l'instant.", caption="Résultats par département"))
    if csv_global:
        parts.append('<p><a class="fr-link" href="/csv/tous">Télécharger le CSV de tous les départements</a></p>')
    return report.page("Liens Vos questions", "\n".join(parts), active="analyses")


RESULTATS_PAR_PAGE = 100
RECHERCHE_MAX = 80  # caractères retenus pour la recherche libre
LIBELLE_RESULTATS = "Communes dont le site contient un lien vers Vos Questions"


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
    def lien(p: int, libelle: str, classe: str = "", attributs: str = "") -> str:
        href = escape(url_resultats(dept, q, p), quote=True)
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
    tranche = lignes[debut:debut + RESULTATS_PAR_PAGE]

    options = '<option value="">Tous les départements</option>' + "".join(
        f'<option value="{escape(d, quote=True)}"{" selected" if d == dept else ""}>{escape(d)}</option>'
        for d in source.depts())
    formulaire = ('<form method="get" action="/resultats" role="search" class="fr-mb-4w">'
                  '<div class="fr-grid-row fr-grid-row--gutters fr-grid-row--bottom">'
                  '<div class="fr-col-12 fr-col-md-3"><div class="fr-select-group">'
                  '<label class="fr-label" for="dept">Département</label>'
                  f'<select class="fr-select" id="dept" name="dept">{options}</select></div></div>'
                  '<div class="fr-col-12 fr-col-md-5"><div class="fr-input-group">'
                  '<label class="fr-label" for="q">Rechercher une commune ou un code INSEE</label>'
                  f'<input class="fr-input" id="q" name="q" type="text" value="{escape(q, quote=True)}"></div></div>'
                  '<div class="fr-col-12 fr-col-md-4">'
                  '<button class="fr-btn" type="submit">Rechercher</button> '
                  '<a class="fr-link" href="/resultats">Réinitialiser</a></div>'
                  "</div></form>")

    n = len(lignes)
    parts = ["<h1>Résultats</h1>",
             f'<p class="fr-text--lead">{LIBELLE_RESULTATS}</p>',
             formulaire,
             f'<p role="status"><strong>{n} commune{"s" if n > 1 else ""} avec lien</strong></p>']
    if not base and dept:
        parts.append('<div class="fr-alert fr-alert--info fr-mb-4w" role="status"><p>Aucune commune avec lien '
                     f"publiée pour le département {escape(dept)}.</p></div>")
    elif not base:
        parts.append('<div class="fr-alert fr-alert--info fr-mb-4w" role="status"><p>Aucun résultat publié pour le '
                     "moment : lancez une analyse depuis le menu Analyses.</p></div>")
    elif not lignes:
        parts.append('<div class="fr-alert fr-alert--info fr-mb-4w" role="status"><p>Aucune commune ne correspond à '
                     "votre recherche.</p></div>")
    else:
        rows = [[escape(ligne["dept"]), escape(ligne["nom_commune"]), escape(ligne["code_insee"]),
                 report.link(ligne["url_page"])] for ligne in tranche]
        parts.append(report.table(["Département", "Commune", "Code INSEE", "Page du lien"], rows, "",
                                  caption=LIBELLE_RESULTATS))
        if total > 1:
            parts.append(pagination(dept, q, numero, total))
    return report.page("Résultats", "\n".join(parts), active="resultats")


def make_handler(root: Path, runner: Runner, password: str, source: PublicationSource | None = None):
    source = source or FileSource(root)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            log.info("%s %s", self.address_string(), fmt % args)

        def send(self, status: int, body: str | bytes, ctype: str = "text/html; charset=utf-8", extra: dict | None = None):
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(status)
            for k, v in {"Content-Type": ctype, "Content-Length": str(len(data)), "X-Content-Type-Options": "nosniff",
                         "Cache-Control": "no-store", **EN_TETES_SECURITE, **(extra or {})}.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def authorized(self) -> bool:
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
            self.send(401, report.page("Accès protégé", "<h1>Mot de passe requis</h1>"),
                      extra={"WWW-Authenticate": 'Basic realm="Liens Vos questions", charset="UTF-8"'})
            return False

        def same_origin(self) -> bool:
            """Refuse un formulaire envoyé depuis un autre site (le navigateur joindrait le mot de passe)."""
            origin = self.headers.get("Origin") or self.headers.get("Referer") or ""
            return bool(origin) and urlsplit(origin).netloc == self.headers.get("Host", "")

        def redirect(self, location: str = "/"):
            self.send(303, "", extra={"Location": location})

        def indisponible(self):
            return self.send(503, report.page("Service indisponible", "<h1>Service indisponible</h1>"
                                              "<p>Base de données indisponible : réessayez plus tard.</p>"
                                              '<p><a href="/">Retour</a></p>'))

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

        def do_GET(self):  # noqa: N802
            path = urlsplit(self.path).path
            if path.startswith(STATIC_URL):  # sans mot de passe : ressources DSFR uniquement
                return self.serve_static(path)
            if not self.authorized():
                return
            if path == "/":
                return self.send(200, home(root, runner, source=source))
            if path == "/resultats":
                try:
                    return self.send(200, resultats(source, parse_qs(urlsplit(self.path).query, keep_blank_values=True)))
                except SourceIndisponible:
                    return self.indisponible()
            m = re.fullmatch(r"/rapport/([0-9AB]{2,3})", path)
            if m:
                try:
                    pub = source.get(m.group(1), csv=False)
                except SourceIndisponible:
                    return self.indisponible()
                if pub is not None:
                    return self.send(200, pub.report_html)
            m = re.fullmatch(r"/csv/([0-9AB]{2,3}|tous)", path)
            if m:
                try:
                    if m.group(1) == "tous":
                        data, name = source.csv_all(), "liens_tous_departements.csv"
                    else:
                        pub = source.get(m.group(1), report=False)
                        data, name = (pub.csv_bytes if pub and pub.csv_present else None), f"liens_{m.group(1)}.csv"
                except SourceIndisponible:
                    return self.indisponible()
                if data is not None:
                    extra = {"Content-Disposition": f'attachment; filename="{name}"'}
                    if len(data) > CSV_GZIP_MIN_OCTETS and self.accepte_gzip():
                        return self.send(200, gzip.compress(data, mtime=0), "text/csv; charset=utf-8",
                                         {**extra, "Content-Encoding": "gzip", "Vary": "Accept-Encoding"})
                    return self.send(200, data, "text/csv; charset=utf-8", extra)
            self.send(404, report.page("Introuvable", '<h1>Page introuvable</h1><p><a href="/">Retour</a></p>'))

        def do_POST(self):  # noqa: N802
            if not self.authorized():
                return
            if not self.same_origin():
                return self.send(403, report.page("Refusé", "<h1>Requête refusée</h1>"))
            longueur = self.headers.get("Content-Length", "0")
            if not re.fullmatch(r"[0-9]{1,5}", longueur) or int(longueur) > POST_MAX_OCTETS:
                # Refus avant toute lecture du corps.
                return self.send(400, report.page("Requête refusée", "<h1>Requête refusée</h1>"
                                                  "<p>Taille de formulaire invalide.</p>"))
            form = {k: v[0] for k, v in parse_qs(self.rfile.read(int(longueur)).decode("utf-8", "replace")).items()}
            path = urlsplit(self.path).path
            if path == "/arreter":
                runner.stop()
                return self.redirect()
            if path == "/lancer":
                depts = parse_depts(form.get("dept", ""))
                if depts is None:
                    return self.send(400, home(root, runner, "Département(s) invalide(s) : utilisez des codes comme 60, 01, 2A, 971."))
                if depts == ["all"] and form.get("confirmer_tous") != "1":
                    return self.send(400, home(root, runner, "Pour analyser toute la France, cochez la case de confirmation."))
                error = runner.start(depts, form.get("reprise") == "1")
                return self.send(409, home(root, runner, error)) if error else self.redirect()
            self.send(404, report.page("Introuvable", "<h1>Page introuvable</h1>"))

    return Handler


def serve(root: Path, host: str, port: int, password: str, runner: Runner | None = None,
          source: PublicationSource | None = None) -> ThreadingHTTPServer:
    if host not in LOCAL_HOSTS and not password:
        raise ValueError("Pour partager l'application sur le réseau, définissez un mot de passe "
                         "(config.toml : mot_de_passe_web) ; sans mot de passe, elle n'écoute que sur ce poste.")
    return ThreadingHTTPServer((host, port), make_handler(root, runner or Runner(root), password, source))
