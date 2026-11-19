"""Lot 4 - Historique des contrôles (SQLite) et comparaison entre deux contrôles.

Après chaque exploration, l'état de chaque commune d'un département est enregistré (lien trouvé ou non, pages, état de
lecture). La comparaison des deux derniers contrôles donne les liens nouveaux, disparus et déplacés. Un lien disparu
n'est « confirmé » que si le site (ou la page PanneauPocket) a été lu sans y retrouver le lien ; sinon (site illisible,
exploration limitée…) il est « à vérifier » : une erreur réseau ne doit pas passer pour une disparition.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import coverage
import results
from layout import DeptPaths

ABSENCE_CONFIRMED = {"site lu en entier, aucun lien", "page PanneauPocket lue, aucun lien"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS controles (id INTEGER PRIMARY KEY, departement TEXT NOT NULL, date TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS etats (
    controle_id INTEGER NOT NULL REFERENCES controles(id), code_insee TEXT NOT NULL, nom_commune TEXT,
    etat TEXT, lien INTEGER NOT NULL, page TEXT, pages TEXT, PRIMARY KEY (controle_id, code_insee));
"""


def db_path(root: Path) -> Path:
    return root / "data" / "historique.sqlite"


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def record(paths: DeptPaths, db: Path, date: str | None = None) -> int:
    """Enregistre l'état actuel du département (couverture + liens) comme nouveau contrôle. Retourne son numéro."""
    states = coverage.build(paths)
    try:
        best = {r["code_insee"]: r["url_page"] for r in results.collect(paths)}
        pages: dict[str, list[str]] = {}
        for r in results.collect(paths, mode="toutes_les_pages"):
            pages.setdefault(r["code_insee"], []).append(r["url_page"])
    except FileNotFoundError:                                  # aucune exploration encore : aucun lien
        best, pages = {}, {}
    with closing(connect(db)) as conn, conn:
        cid = conn.execute("INSERT INTO controles (departement, date) VALUES (?, ?)",
                           (paths.dept, date or datetime.now(timezone.utc).isoformat(timespec="seconds"))).lastrowid
        conn.executemany("INSERT INTO etats VALUES (?, ?, ?, ?, ?, ?, ?)",
                         [(cid, s["code_insee"], s["nom_commune"], s["etat"], int(s["code_insee"] in best),
                           best.get(s["code_insee"], ""), " | ".join(pages.get(s["code_insee"], []))) for s in states])
    return cid


def controls(db: Path, dept: str) -> list[dict]:
    """Contrôles du département, du plus récent au plus ancien."""
    if not db.exists():
        return []
    with closing(connect(db)) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT c.id, c.date, COUNT(e.code_insee) AS communes, COALESCE(SUM(e.lien), 0) AS avec_lien "
            "FROM controles c LEFT JOIN etats e ON e.controle_id = c.id WHERE c.departement = ? "
            "GROUP BY c.id ORDER BY c.id DESC", (dept,))]


def _states(conn, cid: int) -> dict[str, dict]:
    return {r["code_insee"]: dict(r) for r in conn.execute("SELECT * FROM etats WHERE controle_id = ?", (cid,))}


def changes(db: Path, dept: str) -> dict:
    """Différences entre les deux derniers contrôles du département."""
    ctl = controls(db, dept)
    out = {"courant": ctl[0] if ctl else None, "precedent": ctl[1] if len(ctl) > 1 else None,
           "nouveaux": [], "disparus_confirmes": [], "disparus_a_verifier": [], "deplaces": []}
    if len(ctl) < 2:
        return out
    with closing(connect(db)) as conn:
        cur, prev = _states(conn, ctl[0]["id"]), _states(conn, ctl[1]["id"])
    for code, now in sorted(cur.items()):
        before = prev.get(code)
        if now["lien"] and not (before and before["lien"]):
            out["nouveaux"].append(now)
        elif now["lien"] and before and not set(now["pages"].split(" | ")) & set(before["pages"].split(" | ")):
            out["deplaces"].append({**now, "page_precedente": before["page"]})
    for code, before in sorted(prev.items()):
        now = cur.get(code)
        if before["lien"] and not (now and now["lien"]):
            row = {**before, "etat_actuel": now["etat"] if now else "commune absente du contrôle"}
            confirmed = now is not None and now["etat"] in ABSENCE_CONFIRMED
            out["disparus_confirmes" if confirmed else "disparus_a_verifier"].append(row)
    return out


def summary_line(ch: dict) -> str:
    if ch["precedent"] is None:
        return "premier contrôle enregistré : comparaison possible au prochain"
    d = "/".join(reversed(ch["precedent"]["date"][:10].split("-")))
    return (f"depuis le {d} : {len(ch['nouveaux'])} nouveau(x) lien(s), "
            f"{len(ch['disparus_confirmes'])} disparu(s) confirmé(s), {len(ch['disparus_a_verifier'])} disparu(s) à vérifier, "
            f"{len(ch['deplaces'])} déplacé(s)")
