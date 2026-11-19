"""Point d'entrée Vercel de l'interface web Mairie Link Monitor.

Vercel (framework Python) appelle `app`, application WSGI définie en bas de ce fichier : elle rejoue chaque requête
dans `handler` (classe dérivée de BaseHTTPRequestHandler, construite par webapp.make_handler). Pas de réécriture dans
vercel.json : le mode framework dirige toutes les requêtes non statiques vers `app`, avec leur chemin d'origine.
Les ressources DSFR sont servies par Vercel depuis public/ ; le crawl n'a pas lieu ici.

Configuration (variables d'environnement, jamais de valeur dans les pages ni dans les journaux) :
  obligatoires : DATABASE_URL, MLM_PASSWORD, MLM_GH_TOKEN, MLM_GH_REPO (« propriétaire/dépôt »)
  facultatives : MLM_GH_REF (défaut « main »), MLM_GH_WORKFLOW (défaut « analyse.yml »)
Si une variable obligatoire manque (ou si la configuration est invalide), toute requête reçoit 503 avec la liste des
noms des variables manquantes : l'interface n'est jamais servie sans mot de passe.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
from html import escape
from http.server import BaseHTTPRequestHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import report  # noqa: E402
import store_pg  # noqa: E402
import webapp  # noqa: E402
from actions_runner import ActionsRunner  # noqa: E402
from publications import SourceIndisponible  # noqa: E402

log = logging.getLogger("api")
OBLIGATOIRES = ("DATABASE_URL", "MLM_PASSWORD", "MLM_GH_TOKEN", "MLM_GH_REPO")


def _env(nom: str, defaut: str = "") -> str:
    """Valeur de la variable, ou `defaut` si elle est absente ou ne contient que des espaces."""
    valeur = os.environ.get(nom, "")
    return valeur if valeur.strip() else defaut


def _manquantes() -> list[str]:
    return [nom for nom in OBLIGATOIRES if not _env(nom)]


def _avertir(operation: str, exc: BaseException) -> None:
    # Seul le type de l'erreur est journalisé : le message peut contenir des détails de connexion.
    log.warning("api.%s : base de données indisponible (%s)", operation, type(exc).__name__)


class _SchemaUnique:
    """Applique store_pg.ensure_schema au premier accès réel à la base, puis plus jamais dans cette instance.

    Si la base est inaccessible, rien n'est marqué comme fait : la tentative reprendra au prochain accès.
    """

    def __init__(self, url: str):
        self._url = url
        self._fait = False
        self._verrou = threading.Lock()

    def assurer(self) -> None:
        """Ne fait rien une fois le schéma vérifié ; sinon une connexion dédiée (fermée aussitôt)."""
        if self._fait:
            return
        with self._verrou:
            if self._fait:
                return
            conn = store_pg.connect(self._url)
            try:
                store_pg.ensure_schema(conn)
                self._fait = True
            finally:
                conn.close()

    def connect(self):
        self.assurer()
        return store_pg.connect(self._url)


class _StoreAvecSchema:
    """Substitut de store_pg pour ActionsRunner : les connexions passent par _SchemaUnique."""

    def __init__(self, schema: _SchemaUnique):
        self._schema = schema

    def connect(self, url: str):  # l'URL est celle de _SchemaUnique ; le paramètre reste pour l'interface
        return self._schema.connect()

    def __getattr__(self, nom: str):
        return getattr(store_pg, nom)


class _Lecture:
    """Source de l'interface : PostgresSource, schéma vérifié une fois.

    Une panne de base est signalée par SourceIndisponible (503 sur les pages de données, alerte sur l'accueil) ;
    l'erreur d'origine n'est journalisée que par son type.
    """

    def __init__(self, url: str, schema: _SchemaUnique):
        self._source = store_pg.PostgresSource(url)
        self._schema = schema

    def _appeler(self, operation: str, fonction, *args, **kwargs):
        try:
            self._schema.assurer()
            return fonction(*args, **kwargs)
        except Exception as exc:
            _avertir(operation, exc)
            raise SourceIndisponible(operation) from None

    def depts(self) -> list[str]:
        return self._appeler("depts", self._source.depts)

    def get(self, dept: str, *, report: bool = True, csv: bool = True):
        return self._appeler("get", self._source.get, dept, report=report, csv=csv)

    def liens(self, dept: str | None = None) -> list[dict]:
        return self._appeler("liens", self._source.liens, dept)

    def csv_all(self) -> bytes | None:
        return self._appeler("csv_all", self._source.csv_all)

    def has_csv_all(self) -> bool:
        return self._appeler("has_csv_all", self._source.has_csv_all)


def _handler_indisponible(titre: str, lignes: list[str]):
    """Classe qui répond 503 à toute méthode avec une page DSFR (contenu fixe, sans valeur de configuration)."""
    items = "".join(f"<li><code>{escape(ligne)}</code></li>" for ligne in lignes)
    corps = report.page("Configuration incomplète", (
        "<h1>Configuration incomplète</h1>"
        f"<p>{escape(titre)}</p>"
        + (f"<ul>{items}</ul>" if items else "")
        + "<p>Renseignez ces variables dans les réglages du projet Vercel, puis redéployez.</p>"
    )).encode("utf-8")

    class Indisponible(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            log.info("%s", fmt % args)

        def _repondre(self):
            self.send_response(503)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(corps)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(corps)

        do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _repondre

    return Indisponible


def _construire():
    manquantes = _manquantes()
    if manquantes:
        return _handler_indisponible("Variables manquantes :", manquantes)
    url = _env("DATABASE_URL")
    try:
        schema = _SchemaUnique(url)
        source = _Lecture(url, schema)
        runner = ActionsRunner(url, _env("MLM_GH_REPO"), _env("MLM_GH_TOKEN"),
                               _env("MLM_GH_REF", "main"), _env("MLM_GH_WORKFLOW", "analyse.yml"),
                               store=_StoreAvecSchema(schema))
    except (RuntimeError, ValueError) as exc:  # messages sans secret (psycopg absent, dépôt invalide…)
        return _handler_indisponible("Configuration invalide :", [str(exc)])
    return webapp.make_handler(ROOT, runner, _env("MLM_PASSWORD"), source)


handler = _construire()


# ----------------------------------------------------------------- adaptateur WSGI
# Vercel (mode « framework Python ») attend un `app` WSGI écrit explicitement dans le fichier. Il ne lit pas les
# requêtes sur une socket : on rejoue chaque requête dans le gestionnaire existant, avec une fausse socket en mémoire,
# sans changer la logique de webapp.py. Le chemin utilisé est celui de la requête d'origine (PATH_INFO).
import io  # noqa: E402
from urllib.parse import quote  # noqa: E402

TAILLE_MAX_CORPS = 100_000


class _FausseSocket:
    def __init__(self, donnees: bytes):
        self._entree = io.BytesIO(donnees)
        self.sortie = io.BytesIO()

    def makefile(self, mode="r", *_args, **_kwargs):
        return self._entree if "r" in mode else self.sortie

    def sendall(self, donnees):
        self.sortie.write(donnees)

    def settimeout(self, _t):
        pass

    def setsockopt(self, *_args):
        pass

    def close(self):
        pass


class _FauxServeur:
    server_name = "vercel"
    server_port = 443


def _requete_brute(environ) -> bytes:
    methode = environ.get("REQUEST_METHOD", "GET")
    chemin = quote(environ.get("PATH_INFO", "/") or "/", safe="/%:@!$&'()*+,;=-._~")
    if environ.get("QUERY_STRING"):
        chemin += "?" + environ["QUERY_STRING"]
    en_tetes = []
    for cle, valeur in environ.items():
        if cle.startswith("HTTP_"):
            en_tetes.append((cle[5:].replace("_", "-").title(), str(valeur)))
        elif cle in ("CONTENT_TYPE", "CONTENT_LENGTH") and valeur:
            en_tetes.append((cle.replace("_", "-").title(), str(valeur)))
    try:
        taille = max(0, int(environ.get("CONTENT_LENGTH") or 0))
    except ValueError:
        taille = 0
    corps = environ["wsgi.input"].read(min(taille, TAILLE_MAX_CORPS)) if taille and "wsgi.input" in environ else b""
    tete = f"{methode} {chemin} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in en_tetes) + "\r\n"
    return tete.encode("latin-1", "replace") + corps


def app(environ, start_response):
    """Application WSGI : rejoue la requête dans `handler` (BaseHTTPRequestHandler) et renvoie sa réponse."""
    socket = _FausseSocket(_requete_brute(environ))
    adresse = (environ.get("HTTP_X_FORWARDED_FOR", "").split(",")[0].strip() or environ.get("REMOTE_ADDR", "0.0.0.0"), 0)
    try:
        handler(socket, adresse, _FauxServeur())
    except Exception as exc:  # noqa: BLE001 - jamais de trace ni de message dans la réponse
        _avertir("requete", exc)
        start_response("500 Internal Server Error", [("Content-Type", "text/plain; charset=utf-8")])
        return [b"Erreur interne."]
    brut = socket.sortie.getvalue()
    tete, _, corps = brut.partition(b"\r\n\r\n")
    lignes = tete.decode("latin-1").split("\r\n")
    try:
        statut = lignes[0].split(" ", 1)[1]
    except IndexError:
        start_response("502 Bad Gateway", [("Content-Type", "text/plain; charset=utf-8")])
        return [b"Reponse invalide."]
    entetes = [tuple(ligne.split(": ", 1)) for ligne in lignes[1:] if ": " in ligne]
    start_response(statut, entetes)
    return [corps]
