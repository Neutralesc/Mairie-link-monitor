"""Stockage Postgres des publications (analyse GitHub Actions -> base -> interface Vercel).

`publish` écrit, pour chaque département, le résultat de `FileSource` dans la base ; `PostgresSource` le relit et
implémente le protocole `publications.PublicationSource`. Le suivi des analyses (`analysis_*`) sert à l'étape suivante.

psycopg (v3) n'est importé que dans ce module, et uniquement à la demande : `_psycopg()` lève une erreur claire si le
module est absent. Aucune URL ni secret n'est écrit ici : l'URL vient toujours de l'appelant.
"""
from __future__ import annotations

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


def publish(source: PublicationSource, conn) -> dict[str, Any]:
    """Écrit chaque département de `source` (une transaction par département) puis le CSV global s'il existe.

    Les départements déjà publiés et absents de la source ne sont pas supprimés.
    """
    published: list[str] = []
    for dept in source.depts():
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


class PostgresSource:
    """Lecture seule des publications dans Postgres ; une connexion courte par appel."""

    def __init__(self, url: str):
        self.url = url
        _psycopg()  # erreur claire dès la construction si psycopg manque

    def _connect(self):
        return _psycopg().connect(self.url)

    def depts(self) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT dept FROM publications").fetchall()
        return sorted(r[0] for r in rows)

    def get(self, dept: str, *, report: bool = True, csv: bool = True) -> Publication | None:
        cols = ("dept, controle_date, communes, avec_lien, "
                + ("report_html" if report else "''::text AS report_html") + ", "
                + ("csv_bytes" if csv else "''::bytea AS csv_bytes") + ", "
                + "csv_present, resume_changements")
        with self._connect() as conn:
            row = conn.execute(f"SELECT {cols} FROM publications WHERE dept = %s", (dept,)).fetchone()
        if row is None:
            return None
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


def analysis_log(conn, analysis_id: int, text: str) -> None:
    conn.execute(
        "UPDATE analyses SET journal = right(journal || %s, %s) WHERE id = %s",
        (text, JOURNAL_MAX, analysis_id),
    )
    conn.commit()


def analysis_end(conn, analysis_id: int, statut: str) -> None:
    conn.execute("UPDATE analyses SET statut = %s, fin = now() WHERE id = %s", (statut, analysis_id))
    conn.commit()


def analysis_current(conn) -> dict | None:
    cur = conn.execute("SELECT * FROM analyses ORDER BY debut DESC, id DESC LIMIT 1")
    row = cur.fetchone()
    names = [d.name for d in cur.description]
    conn.commit()
    return None if row is None else dict(zip(names, row))
