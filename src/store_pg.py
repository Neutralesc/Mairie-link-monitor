"""Stockage Postgres des publications (analyse GitHub Actions -> base -> interface Vercel).

`publish` écrit, pour chaque département, le résultat de `FileSource` dans la base ; `PostgresSource` le relit et
implémente le protocole `publications.PublicationSource`. Le suivi des analyses (`analysis_*`) sert à l'étape suivante.

psycopg (v3) n'est importé que dans ce module, et uniquement à la demande : `_psycopg()` lève une erreur claire si le
module est absent. Aucune URL ni secret n'est écrit ici : l'URL vient toujours de l'appelant.
"""
from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Any

from publications import CSV_TOUS, Publication, PublicationSource

if TYPE_CHECKING:  # pragma: no cover
    import psycopg

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS publications (
    dept TEXT PRIMARY KEY,
    controle_date TEXT NOT NULL DEFAULT '',
    communes INTEGER NOT NULL DEFAULT 0,
    avec_lien INTEGER NOT NULL DEFAULT 0,
    report_html TEXT NOT NULL DEFAULT '',
    csv_bytes BYTEA NOT NULL DEFAULT ''::bytea,
    csv_present BOOLEAN NOT NULL DEFAULT FALSE,
    resume_changements TEXT NOT NULL DEFAULT '',
    publie_le TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS publication_globale (
    nom TEXT PRIMARY KEY,
    contenu BYTEA NOT NULL,
    publie_le TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS analyses (
    id BIGSERIAL PRIMARY KEY,
    departements TEXT NOT NULL,
    reprise BOOLEAN NOT NULL DEFAULT FALSE,
    statut TEXT NOT NULL CHECK (statut IN ('file','en_cours','terminee','echec','annulee')),
    debut TIMESTAMPTZ NOT NULL DEFAULT now(),
    fin TIMESTAMPTZ,
    journal TEXT NOT NULL DEFAULT '',
    run_id TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS analyses_une_active ON analyses ((TRUE)) WHERE statut IN ('file','en_cours');
"""

JOURNAL_MAX = 4000

_UPSERT_DEPT = """
INSERT INTO publications (dept, controle_date, communes, avec_lien, report_html, csv_bytes, csv_present,
                          resume_changements, publie_le)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
ON CONFLICT (dept) DO UPDATE SET
    controle_date = EXCLUDED.controle_date,
    communes = EXCLUDED.communes,
    avec_lien = EXCLUDED.avec_lien,
    report_html = EXCLUDED.report_html,
    csv_bytes = EXCLUDED.csv_bytes,
    csv_present = EXCLUDED.csv_present,
    resume_changements = EXCLUDED.resume_changements,
    publie_le = now()
"""

_UPSERT_GLOBAL = """
INSERT INTO publication_globale (nom, contenu, publie_le) VALUES (%s, %s, now())
ON CONFLICT (nom) DO UPDATE SET contenu = EXCLUDED.contenu, publie_le = now()
"""


def _psycopg():
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError('Le module psycopg est requis pour Postgres : pip install "psycopg[binary]"') from exc
    return psycopg


def connect(url: str) -> psycopg.Connection:
    psycopg = _psycopg()
    return psycopg.connect(url, autocommit=False)


def ensure_schema(conn) -> None:
    conn.execute(SCHEMA_SQL)
    conn.commit()


def publish(source: PublicationSource, conn, only: list[str] | None = None) -> dict[str, Any]:
    """Écrit chaque département de `source` (une transaction par département) puis le CSV global s'il existe.

    `only` : si fourni et différent de ['all'], ne publie que ces départements (ceux de `source` qui y figurent).
    Le CSV global est publié dans tous les cas s'il existe.

    Les départements déjà publiés et absents de la source ne sont pas supprimés.
    """
    restreint = None if only is None or list(only) == ["all"] else set(only)
    published: list[str] = []
    for dept in source.depts():
        if restreint is not None and dept not in restreint:
            continue
        pub = source.get(dept)
        if pub is None:
            continue
        try:
            conn.execute(_UPSERT_DEPT, (pub.dept, pub.controle_date, pub.communes, pub.avec_lien, pub.report_html,
                                        pub.csv_bytes, pub.csv_present, pub.resume_changements))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        published.append(pub.dept)

    contenu = source.csv_all()
    csv_global = contenu is not None
    if csv_global:
        try:
            conn.execute(_UPSERT_GLOBAL, (CSV_TOUS, contenu))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return {"departements": published, "csv_global": csv_global}


CACHE_LEGER_S = 5.0  # durée de validité du cache des champs légers (accueil)

_COLS_LEGERES = ("dept, controle_date, communes, avec_lien, ''::text AS report_html, ''::bytea AS csv_bytes, "
                 "csv_present, resume_changements")


class PostgresSource:
    """Lecture seule des publications dans Postgres ; une connexion courte par appel.

    `depts()` lit en une requête les champs légers de tous les départements (sans compte rendu ni CSV) et les garde
    5 s : `get(d, report=False, csv=False)` sert alors depuis ce cache, sans connexion. `get` avec `report` ou `csv`
    demandés fait une requête ciblée sur ce seul département.
    """

    def __init__(self, url: str):
        self.url = url
        self._verrou = threading.Lock()
        self._leger: tuple[float, dict[str, Publication]] | None = None
        _psycopg()  # erreur claire dès la construction si psycopg manque

    def _connect(self):
        return _psycopg().connect(self.url)

    @staticmethod
    def _publication(row) -> Publication:
        return Publication(
            dept=row[0],
            controle_date=row[1],
            communes=row[2],
            avec_lien=row[3],
            report_html=row[4],
            csv_bytes=bytes(row[5]),
            csv_present=row[6],
            resume_changements=row[7],
        )

    def _cache_frais(self) -> dict[str, Publication] | None:
        with self._verrou:
            if self._leger is not None and time.monotonic() - self._leger[0] < CACHE_LEGER_S:
                return self._leger[1]
        return None

    def depts(self) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(f"SELECT {_COLS_LEGERES} FROM publications").fetchall()
        leger = {r[0]: self._publication(r) for r in rows}
        with self._verrou:
            self._leger = (time.monotonic(), leger)
        return sorted(leger)

    def get(self, dept: str, *, report: bool = True, csv: bool = True) -> Publication | None:
        if not report and not csv:
            pub = (self._cache_frais() or {}).get(dept)
            if pub is not None:
                return pub
        cols = ("dept, controle_date, communes, avec_lien, "
                + ("report_html" if report else "''::text AS report_html") + ", "
                + ("csv_bytes" if csv else "''::bytea AS csv_bytes") + ", "
                + "csv_present, resume_changements")
        with self._connect() as conn:
            row = conn.execute(f"SELECT {cols} FROM publications WHERE dept = %s", (dept,)).fetchone()
        return None if row is None else self._publication(row)

    def csv_all(self) -> bytes | None:
        with self._connect() as conn:
            row = conn.execute("SELECT contenu FROM publication_globale WHERE nom = %s", (CSV_TOUS,)).fetchone()
        return None if row is None else bytes(row[0])


# ----------------------------------------------------------------- suivi des analyses

def analysis_start(conn, depts: list[str], reprise: bool, run_id: str = "") -> int:
    row = conn.execute(
        "INSERT INTO analyses (departements, reprise, statut, run_id) VALUES (%s, %s, 'en_cours', %s) RETURNING id",
        (",".join(depts), reprise, run_id),
    ).fetchone()
    conn.commit()
    return int(row[0])


def analysis_queue(conn, depts: list[str], reprise: bool) -> int | None:
    """Ajoute une analyse en file d'attente ; None si une analyse est déjà active (index unique analyses_une_active)."""
    try:
        row = conn.execute(
            "INSERT INTO analyses (departements, reprise, statut) VALUES (%s, %s, 'file') RETURNING id",
            (",".join(depts), reprise),
        ).fetchone()
        conn.commit()
    except _psycopg().errors.UniqueViolation:
        conn.rollback()
        return None
    return int(row[0])


def analysis_attach_run(conn, analysis_id: int, run_id: str) -> bool:
    """Rattache le run GitHub à l'analyse : 'file' -> 'en_cours' ; 'en_cours' -> run_id seul ; statut final -> False."""
    row = conn.execute(
        "UPDATE analyses SET run_id = %s, statut = CASE WHEN statut = 'file' THEN 'en_cours' ELSE statut END "
        "WHERE id = %s AND statut IN ('file','en_cours') RETURNING id",
        (run_id, analysis_id),
    ).fetchone()
    conn.commit()
    return row is not None


def analysis_log(conn, analysis_id: int, text: str) -> None:
    conn.execute(
        "UPDATE analyses SET journal = right(journal || %s, %s) WHERE id = %s",
        (text, JOURNAL_MAX, analysis_id),
    )
    conn.commit()


def analysis_end(conn, analysis_id: int, statut: str) -> bool:
    """Passe l'analyse à `statut` seulement si elle n'est pas déjà finale. True si une ligne a changé."""
    cur = conn.execute(
        "UPDATE analyses SET statut = %s, fin = now() WHERE id = %s AND statut IN ('file','en_cours')",
        (statut, analysis_id),
    )
    conn.commit()
    return cur.rowcount == 1


def analysis_get(conn, analysis_id: int) -> dict | None:
    cur = conn.execute("SELECT * FROM analyses WHERE id = %s", (analysis_id,))
    row = cur.fetchone()
    names = [d.name for d in cur.description]
    conn.commit()
    return None if row is None else dict(zip(names, row))


def analysis_current(conn) -> dict | None:
    cur = conn.execute("SELECT * FROM analyses ORDER BY debut DESC, id DESC LIMIT 1")
    row = cur.fetchone()
    names = [d.name for d in cur.description]
    conn.commit()
    return None if row is None else dict(zip(names, row))
