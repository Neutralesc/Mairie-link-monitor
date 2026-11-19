"""Application web : lancer une analyse, suivre son avancement, consulter comptes rendus, CSV et changements.

Bibliothèque standard uniquement. Une analyse à la fois, exécutée dans un processus séparé (le programme en ligne de
commande habituel) ; son journal est écrit dans data/web/derniere_analyse.log.
Sécurité : par défaut l'application n'écoute que sur ce poste (127.0.0.1). Pour la partager sur le réseau, un mot de
passe est obligatoire (authentification HTTP Basic) ; les formulaires refusent les requêtes venant d'un autre site.
"""
from __future__ import annotations

import base64
import hmac
import logging
import os
import re
import subprocess
import sys
import threading
import time
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import history
import report
from layout import DeptPaths, normalize_dept

log = logging.getLogger("webapp")
DEPT_RE = re.compile(r"^(\d{2,3}|2[AB])$")
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
# Ressources DSFR servies sans mot de passe (le reste du site garde l'authentification).
STATIC_URL = "/static/dsfr/"
STATIC_DIR = report.STATIC_DSFR
STATIC_MIME = {".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".woff2": "font/woff2",
               ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon",
               ".webmanifest": "application/manifest+json"}


def static_file(rel: str) -> Path | None:
    """Fichier de src/static/dsfr demandé, ou None (chemin suspect, type non servi, absent)."""
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
    return sorted(p.name for p in (root / "reports").glob("*") if p.is_dir() and DEPT_RE.match(p.name))


def home(root: Path, runner: Runner, message: str = "") -> str:
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
    rows = []
    db = history.db_path(root)
    for d in known_depts(root):
        ch = history.changes(db, d)
        cur = ch["courant"]
        sr = f'<span class="fr-sr-only"> du département {escape(d)}</span>'
        csv_link = f'<a class="fr-link" href="/csv/{d}">CSV{sr}</a>' if DeptPaths(root, d).result_csv.exists() else ""
        rows.append([f"<b>{escape(d)}</b>", escape(report.fr_date(cur["date"])) if cur else "—",
                     str(cur["avec_lien"]) if cur else "—",
                     escape(history.summary_line(ch)) if cur else "—",
                     f'<a class="fr-link" href="/rapport/{d}">Compte rendu{sr}</a> {csv_link}'])
    parts.append("<h2>Résultats par département</h2>")
    parts.append(report.table(["Département", "Dernier contrôle", "Communes avec lien", "Changements", "Ouvrir"], rows,
                              "Aucun département analysé pour l'instant.", caption="Résultats par département"))
    if (root / "resultats" / "liens_tous_departements.csv").exists():
        parts.append('<p><a class="fr-link" href="/csv/tous">Télécharger le CSV de tous les départements</a></p>')
    return report.page("Liens Vos questions", "\n".join(parts))


def make_handler(root: Path, runner: Runner, password: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            log.info("%s %s", self.address_string(), fmt % args)

        def send(self, status: int, body: str | bytes, ctype: str = "text/html; charset=utf-8", extra: dict | None = None):
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(status)
            for k, v in {"Content-Type": ctype, "Content-Length": str(len(data)), "X-Content-Type-Options": "nosniff",
                         "Cache-Control": "no-store", **(extra or {})}.items():
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
                return self.send(200, home(root, runner))
            m = re.fullmatch(r"/rapport/([0-9AB]{2,3})", path)
            if m and m.group(1) in known_depts(root):
                return self.send(200, report.render(DeptPaths(root, m.group(1)), history.db_path(root)))
            m = re.fullmatch(r"/csv/([0-9AB]{2,3}|tous)", path)
            if m:
                name = "tous_departements" if m.group(1) == "tous" else m.group(1)
                f = root / "resultats" / f"liens_{name}.csv"
                if f.exists():
                    return self.send(200, f.read_bytes(), "text/csv; charset=utf-8",
                                     {"Content-Disposition": f'attachment; filename="{f.name}"'})
            self.send(404, report.page("Introuvable", '<h1>Page introuvable</h1><p><a href="/">Retour</a></p>'))

        def do_POST(self):  # noqa: N802
            if not self.authorized():
                return
            if not self.same_origin():
                return self.send(403, report.page("Refusé", "<h1>Requête refusée</h1>"))
            length = min(int(self.headers.get("Content-Length") or 0), 10_000)
            form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode("utf-8", "replace")).items()}
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


def serve(root: Path, host: str, port: int, password: str, runner: Runner | None = None) -> ThreadingHTTPServer:
    if host not in LOCAL_HOSTS and not password:
        raise ValueError("Pour partager l'application sur le réseau, définissez un mot de passe "
                         "(config.toml : mot_de_passe_web) ; sans mot de passe, elle n'écoute que sur ce poste.")
    return ThreadingHTTPServer((host, port), make_handler(root, runner or Runner(root), password))
