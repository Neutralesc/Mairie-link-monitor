"""Point d'entrée Vercel (runtime Python « handler ») de l'interface web Mairie Link Monitor.

Vercel appelle la classe `handler` (dérivée de BaseHTTPRequestHandler) pour chaque requête non statique (voir
vercel.json). Les ressources DSFR sont servies par Vercel depuis public/ ; le crawl n'a pas lieu ici.

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
    """Source de l'interface : PostgresSource, schéma vérifié une fois, pannes de base absorbées.

    L'accueil reste utilisable (liste vide) si la base est en panne : aucune exception ne remonte à la page.
    """

    def __init__(self, url: str, schema: _SchemaUnique):
        self._source = store_pg.PostgresSource(url)
        self._schema = schema

    def depts(self) -> list[str]:
        try:
            self._schema.assurer()
            return self._source.depts()
        except Exception as exc:
            _avertir("depts", exc)
            return []

    def get(self, dept: str, *, report: bool = True, csv: bool = True):
        try:
            self._schema.assurer()
            return self._source.get(dept, report=report, csv=csv)
        except Exception as exc:
            _avertir("get", exc)
            return None

    def csv_all(self) -> bytes | None:
        try:
            self._schema.assurer()
            return self._source.csv_all()
        except Exception as exc:
            _avertir("csv_all", exc)
            return None


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
