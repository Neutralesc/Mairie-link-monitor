#!/usr/bin/env python3
"""Analyse dans GitHub Actions : lance le crawler, journalise, publie dans Postgres et tient la ligne `analyses` à jour.

  python src/ci_analyse.py run --analysis-id N --depts "60 80" [--reprise] [--root DOSSIER]
  python src/ci_analyse.py fin --analysis-id N --statut annulee|echec

Variables d'environnement : DATABASE_URL (obligatoire), GITHUB_RUN_ID (run_id), MLM_CONTACT (facultatif : --contact).
Codes retour : 0 terminee (ou rien à lancer) ; 1 échec ; 2 arguments ou configuration invalides ; 130 annulée.
Prévu pour GitHub Actions (POSIX) : le crawler tourne dans son propre groupe de processus pour que son arrêt emporte
aussi ses processus d'exploration.
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import store_pg
from publications import FileSource
from webapp import parse_depts

ROOT = Path(__file__).resolve().parents[1]
JOURNAL_INTERVALLE = 20.0  # secondes entre deux écritures du journal
ARRET_DELAI = 10.0         # secondes entre l'arrêt demandé (SIGTERM) et SIGKILL
CODE_ANNULEE = 130

# Écrit par le gestionnaire de signal, lu par la boucle principale. Le gestionnaire ne prend aucun verrou.
_SIGNAL = {"recu": 0}


def _sur_signal(signum, _frame) -> None:
    _SIGNAL["recu"] = signum


def _signal_recu() -> int:
    return _SIGNAL["recu"]


def _database_url() -> str | None:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        print("DATABASE_URL absente : définissez le secret DATABASE_URL (ou la variable de l'environnement).",
              file=sys.stderr, flush=True)
        return None
    return url


def commande_crawler(root: Path, depts: list[str], reprise: bool, contact: str) -> list[str]:
    """Même ligne de commande que l'interface web (webapp.Runner), avec le contact facultatif."""
    cmd = [sys.executable, str(root / "src" / "mairie_link_monitor.py"), "--root", str(root), "run", "--dept", *depts]
    if depts == ["all"]:
        cmd.append("--confirm-all")
    if reprise:
        cmd.append("--resume")
    if contact:
        cmd.append(f"--contact={contact}")
    return cmd


def _annuler(conn) -> None:
    try:
        conn.rollback()
    except Exception:  # noqa: BLE001 : la connexion peut être déjà morte
        pass


def _essayer(conn, action) -> bool:
    try:
        action()
        return True
    except Exception as exc:  # noqa: BLE001
        _annuler(conn)
        print(f"[ci_analyse] {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return False


class _Journal:
    """Lignes du crawler en attente d'écriture. `lire` tourne dans un thread ; `vider` est appelé par le thread principal
    avec sa propre connexion (aucune connexion partagée entre threads)."""

    def __init__(self, conn, analysis_id: int):
        self.conn = conn
        self.analysis_id = analysis_id
        self._lignes: list[str] = []
        self._verrou = threading.Lock()

    def _ajouter(self, texte: str) -> None:
        with self._verrou:
            self._lignes.append(texte)

    def lire(self, flux) -> None:
        for ligne in flux:
            self._ajouter(ligne)
            sys.stdout.write(ligne)
            sys.stdout.flush()

    def vider(self) -> None:
        with self._verrou:
            if not self._lignes:
                return
            texte = "".join(self._lignes)
            self._lignes.clear()
        try:
            store_pg.analysis_log(self.conn, self.analysis_id, texte)
        except Exception as exc:  # noqa: BLE001 : un journal en échec ne doit pas interrompre l'analyse
            _annuler(self.conn)
            with self._verrou:
                self._lignes.insert(0, texte)  # nouvelle tentative au prochain vidage
            print(f"[ci_analyse] journal non écrit, nouvelle tentative plus tard : {exc}", file=sys.stderr, flush=True)

    def note(self, texte: str) -> None:
        print(texte, flush=True)
        self._ajouter(texte + "\n")


def _signaler(proc: subprocess.Popen, sig: int) -> None:
    try:
        os.killpg(proc.pid, sig)  # pgid = pid du crawler (start_new_session)
    except OSError:  # groupe déjà disparu
        pass


def _arreter(proc: subprocess.Popen) -> None:
    """SIGTERM au groupe du crawler, puis SIGKILL après ARRET_DELAI s'il est encore là (ses processus d'exploration aussi)."""
    _signaler(proc, signal.SIGTERM)
    limite = time.monotonic() + ARRET_DELAI
    while proc.poll() is None and time.monotonic() < limite:
        time.sleep(0.2)
    _signaler(proc, signal.SIGKILL)


def _lancer(journal: _Journal, cmd: list[str]) -> tuple[int | None, bool]:
    """Exécute le crawler et suit sa sortie. Retourne (code retour, ou None si non lancé ; interrompu)."""
    if _signal_recu():
        return None, True
    env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}  # le crawler n'a pas accès à la base
    env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            env=env, text=True, encoding="utf-8", errors="replace", bufsize=1,
                            start_new_session=True)
    lecteur = threading.Thread(target=journal.lire, args=(proc.stdout,), daemon=True)
    lecteur.start()
    dernier = time.monotonic()
    while lecteur.is_alive() and not _signal_recu():
        lecteur.join(timeout=1.0)
        if time.monotonic() - dernier >= JOURNAL_INTERVALLE:
            journal.vider()
            dernier = time.monotonic()
    interrompu = bool(_signal_recu())
    if interrompu:
        journal.note(f"[ci_analyse] signal {signal.Signals(_signal_recu()).name} reçu : arrêt du crawler")
        _arreter(proc)
    rc = proc.wait()
    lecteur.join(timeout=30)  # un processus d'exploration orphelin pourrait garder la sortie ouverte
    journal.vider()
    return rc, interrompu


def _publier(root: Path, conn, journal: _Journal) -> bool:
    try:
        res = store_pg.publish(FileSource(root), conn)
    except Exception as exc:  # noqa: BLE001
        _annuler(conn)
        journal.note(f"[ci_analyse] publication en échec : {type(exc).__name__}: {exc}")
        return False
    journal.note(f"[ci_analyse] publié : {', '.join(res['departements']) or '(aucun département)'} ; "
                 f"CSV global : {'oui' if res['csv_global'] else 'non'}")
    return True


def _executer(url: str, conn, root: Path, analysis_id: int, depts: list[str], reprise: bool, run_id: str,
              contact: str) -> int:
    if not store_pg.analysis_attach_run(conn, analysis_id, run_id):
        print(f"Analyse {analysis_id} : absente ou déjà finale (annulée avant le démarrage) : rien à lancer.", flush=True)
        return 0
    journal_conn = store_pg.connect(url)
    try:
        journal = _Journal(journal_conn, analysis_id)
        rc, interrompu = _lancer(journal, commande_crawler(root, depts, reprise, contact))
        publie = _publier(root, conn, journal)  # même en cas d'échec ou d'arrêt : un résultat partiel vaut mieux que rien
        if interrompu:
            statut, code = "annulee", CODE_ANNULEE
        elif rc == 0 and publie:
            statut, code = "terminee", 0
        else:
            statut, code = "echec", 1
        journal.note(f"[ci_analyse] code retour du crawler : {rc} ; statut : {statut}")
        journal.vider()
        if not store_pg.analysis_end(conn, analysis_id, statut):
            print(f"Analyse {analysis_id} : statut déjà final, inchangé.", flush=True)
        return code
    finally:
        journal_conn.close()


def cmd_run(args) -> int:
    depts = parse_depts(args.depts)
    if depts is None:
        print("Département(s) invalide(s) : utilisez des codes comme 60, 01, 2A, 971, ou « tous ».",
              file=sys.stderr, flush=True)
        return 2
    url = _database_url()
    if url is None:
        return 2
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, _sur_signal)
    try:
        conn = store_pg.connect(url)
    except Exception as exc:  # noqa: BLE001
        print(f"Connexion à la base impossible : {exc}", file=sys.stderr, flush=True)
        return 1
    try:
        return _executer(url, conn, args.root, args.analysis_id, depts, args.reprise,
                         os.environ.get("GITHUB_RUN_ID", ""), os.environ.get("MLM_CONTACT", "").strip())
    except Exception as exc:  # noqa: BLE001 : journalisé, analyse marquée en échec, code 1
        message = f"[ci_analyse] erreur inattendue : {type(exc).__name__}: {exc}"
        print(message, file=sys.stderr, flush=True)
        _essayer(conn, lambda: store_pg.analysis_log(conn, args.analysis_id, message + "\n"))
        _essayer(conn, lambda: store_pg.analysis_end(conn, args.analysis_id, "echec"))
        return 1
    finally:
        conn.close()


def cmd_fin(args) -> int:
    url = _database_url()
    if url is None:
        return 2
    conn = store_pg.connect(url)
    try:
        if store_pg.analysis_end(conn, args.analysis_id, args.statut):
            print(f"Analyse {args.analysis_id} : statut « {args.statut} ».", flush=True)
        else:
            print(f"Analyse {args.analysis_id} : déjà finale ou absente, inchangée.", flush=True)
        return 0
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Analyse GitHub Actions : crawler, journal, publication.")
    sub = ap.add_subparsers(dest="commande", required=True)
    r = sub.add_parser("run", help="exécute une analyse et publie son résultat")
    r.add_argument("--analysis-id", type=int, required=True)
    r.add_argument("--depts", required=True, help='codes séparés par des espaces, ex. "60 80", ou "tous"')
    r.add_argument("--reprise", action="store_true", help="ne refait que le travail manquant")
    r.add_argument("--root", type=Path, default=ROOT, help="racine du projet (défaut : dossier parent de src/)")
    r.set_defaults(func=cmd_run)
    f = sub.add_parser("fin", help="marque une analyse annulée ou en échec (sans effet si déjà finale)")
    f.add_argument("--analysis-id", type=int, required=True)
    f.add_argument("--statut", required=True, choices=("annulee", "echec"))
    f.set_defaults(func=cmd_fin)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
