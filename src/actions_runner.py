"""Runner d'analyse via GitHub Actions : déclenche et annule un workflow, l'état vient de la table `analyses`.

Remplace, sur Vercel, le sous-processus local de `webapp.Runner` (même interface pour l'interface web).
Aucune URL de connexion, aucun jeton n'est écrit dans les messages ni dans les journaux.
psycopg n'est pas importé ici : `store_pg` (import paresseux) ne le charge qu'à la connexion.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import quote

log = logging.getLogger("actions_runner")

API = "https://api.github.com"
MSG_DEJA = "Une analyse est déjà en cours : attendez la fin ou arrêtez-la."
MSG_DB = "Base de données indisponible : réessayez plus tard."
MSG_RESEAU = "Impossible de joindre GitHub : réessayez plus tard."
PEREMPTION_S = 7 * 3600          # analyse en cours (en_cours) : runner présumé mort au-delà
PEREMPTION_FILE_S = 30 * 60      # analyse en file jamais démarrée : abandonnée au-delà
CACHE_S = 2.0
ACTIFS = ("file", "en_cours")

_SEGMENT_RE = re.compile(r"[\w.-]+", re.ASCII)


def _segments_ok(texte: str) -> bool:
    """Chaque segment est sûr : caractères ASCII autorisés, pas de « . » ni « .. » (chemin d'URL)."""
    return all(_SEGMENT_RE.fullmatch(seg) and seg not in (".", "..") for seg in texte.split("/"))


def _http_defaut(method: str, url: str, headers: dict, body: bytes | None) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        try:
            data = exc.read()
        except OSError:
            data = b""
        return exc.code, data


class ActionsRunner:
    def __init__(self, database_url: str, repo: str, token: str, ref: str = "main", workflow: str = "analyse.yml",
                 *, http=None, store=None):
        if not isinstance(repo, str) or repo.count("/") != 1 or not _segments_ok(repo):
            raise ValueError("Dépôt GitHub invalide : attendu « propriétaire/dépôt ».")
        if not isinstance(workflow, str) or not _segments_ok(workflow) or "/" in workflow:
            raise ValueError("Nom de workflow invalide : caractères alphanumériques, « . », « - » ou « _ » seulement.")
        if not isinstance(ref, str) or not _segments_ok(ref) or ".." in ref or ref.startswith("/") or ref.endswith("/"):
            raise ValueError("Branche (ref) invalide : caractères sûrs seulement, sans « .. ».")
        if store is None:
            import store_pg as store
        self._url = database_url
        self._repo = repo
        self._token = token
        self._ref = ref
        self._workflow = workflow
        self._http = http or _http_defaut
        self._store = store
        self._verrou = threading.Lock()
        self._cache: tuple[float, dict | None] | None = None

    # ------------------------------------------------------------ lecture de l'état

    def _lire(self, frais: bool = False) -> dict | None:
        """Analyse courante ; mise en cache 2 s sauf si `frais`. Lève une exception si la base est en panne."""
        if not frais:
            with self._verrou:
                if self._cache is not None and time.monotonic() - self._cache[0] < CACHE_S:
                    return self._cache[1]
        conn = self._store.connect(self._url)
        try:
            row = self._store.analysis_current(conn)
        finally:
            conn.close()
        with self._verrou:
            self._cache = (time.monotonic(), row)
        return row

    def _invalider(self) -> None:
        with self._verrou:
            self._cache = None

    @staticmethod
    def _active(row: dict | None) -> bool:
        return row is not None and row.get("statut") in ACTIFS

    @staticmethod
    def _perimee(row: dict) -> bool:
        debut = row.get("debut")
        seuil = PEREMPTION_FILE_S if row.get("statut") == "file" else PEREMPTION_S
        return debut is not None and time.time() - debut.timestamp() > seuil

    def _terminer(self, analysis_id: int, statut: str) -> None:
        conn = self._store.connect(self._url)
        try:
            self._store.analysis_end(conn, analysis_id, statut)
        finally:
            conn.close()
        self._invalider()

    def _avertir(self, operation: str, exc: BaseException) -> None:
        # Seul le type de l'erreur est journalisé : le message peut contenir des détails de connexion.
        log.warning("ActionsRunner.%s : base de données ou réseau indisponible (%s)", operation, type(exc).__name__)

    def running(self) -> bool:
        try:
            row = self._lire()
            if not self._active(row):
                return False
            if self._perimee(row):
                # Un runner mort ne doit pas bloquer indéfiniment.
                self._terminer(row["id"], "echec")
                return False
            return True
        except Exception as exc:
            self._avertir("running", exc)
            return False

    @property
    def started(self) -> float:
        try:
            row = self._lire()
        except Exception as exc:
            self._avertir("started", exc)
            return 0.0
        if row is None or row.get("debut") is None:
            return 0.0
        return row["debut"].timestamp()

    @property
    def label(self) -> str:
        try:
            row = self._lire()
        except Exception as exc:
            self._avertir("label", exc)
            return ""
        if row is None:
            return ""
        texte = " ".join((row.get("departements") or "").split(","))
        return texte + (" (reprise)" if row.get("reprise") else "")

    def tail(self, lines: int = 25) -> str:
        if lines <= 0:
            return ""
        try:
            row = self._lire()
        except Exception as exc:
            self._avertir("tail", exc)
            return ""
        if row is None:
            return ""
        return "\n".join((row.get("journal") or "").splitlines()[-lines:])

    # ------------------------------------------------------------ démarrage et arrêt

    def _mettre_en_file(self, depts: list[str], resume: bool) -> int | None:
        conn = self._store.connect(self._url)
        try:
            # Une analyse active mais trop vieille est close avant de tester la file (sinon elle bloquerait tout).
            row = self._store.analysis_current(conn)
            if self._active(row) and self._perimee(row):
                self._store.analysis_end(conn, row["id"], "echec")
            return self._store.analysis_queue(conn, depts, resume)
        finally:
            conn.close()
            self._invalider()

    def _api(self, method: str, chemin: str, corps: bytes | None = None) -> tuple[int, bytes]:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "mairie-link-monitor",
        }
        if corps is not None:
            headers["Content-Type"] = "application/json"
        return self._http(method, API + chemin, headers, corps)

    def start(self, depts: list[str], resume: bool) -> str:
        depts = list(depts)
        resume = bool(resume)
        try:
            analysis_id = self._mettre_en_file(depts, resume)
        except Exception as exc:
            self._avertir("start", exc)
            return MSG_DB
        if analysis_id is None:
            return MSG_DEJA

        corps = json.dumps({
            "ref": self._ref,
            "inputs": {
                "depts": " ".join(depts),
                "reprise": "true" if resume else "false",
                "analysis_id": str(analysis_id),
            },
        }).encode("utf-8")
        chemin = f"/repos/{self._repo}/actions/workflows/{quote(self._workflow, safe='')}/dispatches"
        try:
            status, _ = self._api("POST", chemin, corps)
        except Exception as exc:
            self._avertir("start/dispatch", exc)
            status = None
        if status == 204:
            return ""

        try:
            self._terminer(analysis_id, "echec")
        except Exception as exc:
            self._avertir("start/echec", exc)
        if status is None:
            return MSG_RESEAU
        return f"Impossible de lancer l'analyse (GitHub : HTTP {status})."

    def _annuler_non_demarree(self, analysis_id: int) -> bool:
        """Annule une analyse 'file' sans run_id ; False si le CI vient de s'y attacher (ou si elle a changé)."""
        conn = self._store.connect(self._url)
        try:
            annulee = self._store.analysis_cancel_unstarted(conn, analysis_id)
        finally:
            conn.close()
        self._invalider()
        return annulee

    def stop(self) -> None:
        try:
            row = self._lire(frais=True)
            if not self._active(row):
                return
            analysis_id = row["id"]
            if not row.get("run_id") and row.get("statut") == "file":
                # Pas encore démarrée côté GitHub : annulation conditionnelle (course avec l'attachement du CI).
                if self._annuler_non_demarree(analysis_id):
                    return
                row = self._lire(frais=True)
                if not self._active(row) or row["id"] != analysis_id:
                    return
            run_id = str(row.get("run_id") or "")
            if not run_id:
                # En cours sans run_id : rien à annuler côté GitHub.
                self._terminer(analysis_id, "annulee")
                return
            status, _ = self._api("POST", f"/repos/{self._repo}/actions/runs/{quote(run_id, safe='')}/cancel")
            if status != 202:
                log.warning("ActionsRunner.stop : annulation refusée par GitHub (HTTP %s)", status)
            self._invalider()
        except Exception as exc:
            self._avertir("stop", exc)
