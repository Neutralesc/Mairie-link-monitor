"""Stockage Postgres des publications (analyse GitHub Actions -> base -> interface Vercel).

`publish` écrit, pour chaque département, le résultat de `FileSource` dans la base ; `PostgresSource` le relit et
implémente le protocole `publications.PublicationSource`. Le suivi des analyses (`analysis_*`) sert à l'étape suivante.

psycopg (v3) n'est importé que dans ce module, et uniquement à la demande : `_psycopg()` lève une erreur claire si le
module est absent. Aucune URL ni secret n'est écrit ici : l'URL vient toujours de l'appelant.
"""
from __future__ import annotations

import json
import threading
import time
from typing import TYPE_CHECKING, Any

from datetime import datetime

from publications import (CSV_TOUS, Publication, PublicationSource, liens_confirmes, selection_liens,
                          trier_liens)
from synthese import resume as resume_synthese

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
    liens_json TEXT NOT NULL DEFAULT '[]',
    synthese_json TEXT NOT NULL DEFAULT '{}',
    resume_json TEXT NOT NULL DEFAULT '{}',
    publie_le TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE publications ADD COLUMN IF NOT EXISTS liens_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE publications ADD COLUMN IF NOT EXISTS synthese_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE publications ADD COLUMN IF NOT EXISTS resume_json TEXT NOT NULL DEFAULT '{}';
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
CREATE TABLE IF NOT EXISTS liens_vus (
    departement TEXT NOT NULL,
    code_insee TEXT NOT NULL,
    url_page_source TEXT NOT NULL,
    url_cible TEXT NOT NULL DEFAULT '',
    commune TEXT NOT NULL DEFAULT '',
    premiere_vue TIMESTAMPTZ NOT NULL DEFAULT now(),
    nouveau BOOLEAN NOT NULL DEFAULT TRUE,
    PRIMARY KEY (departement, code_insee, url_page_source, url_cible)
);
CREATE INDEX IF NOT EXISTS liens_vus_nouveaux ON liens_vus (premiere_vue) WHERE nouveau;
"""
# Première apparition des liens confirmés. Insertion seule (ON CONFLICT DO NOTHING) : un lien déjà connu garde sa date.
_INSERT_LIEN_VU = ("INSERT INTO liens_vus (departement, code_insee, url_page_source, url_cible, commune) "
                   "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING")
# Amorçage à la création de la table : résultats déjà publiés, marqués non nouveaux, datés de leur publication.
_INSERT_LIEN_BASE = ("INSERT INTO liens_vus (departement, code_insee, url_page_source, url_cible, commune, premiere_vue, "
                     "nouveau) VALUES (%s, %s, %s, %s, %s, %s, FALSE) ON CONFLICT DO NOTHING")

JOURNAL_MAX = 4000
CONNECT_TIMEOUT_S = 5  # délai de connexion : une base injoignable ne bloque pas une requête web ou le runner

_UPSERT_DEPT = """
INSERT INTO publications (dept, controle_date, communes, avec_lien, report_html, csv_bytes, csv_present,
                          resume_changements, liens_json, synthese_json, resume_json, publie_le)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
ON CONFLICT (dept) DO UPDATE SET
    controle_date = EXCLUDED.controle_date,
    communes = EXCLUDED.communes,
    avec_lien = EXCLUDED.avec_lien,
    report_html = EXCLUDED.report_html,
    csv_bytes = EXCLUDED.csv_bytes,
    csv_present = EXCLUDED.csv_present,
    resume_changements = EXCLUDED.resume_changements,
    liens_json = EXCLUDED.liens_json,
    synthese_json = EXCLUDED.synthese_json,
    resume_json = EXCLUDED.resume_json,
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
    return psycopg.connect(url, autocommit=False, connect_timeout=CONNECT_TIMEOUT_S)


def _lignes_liens(dept: str, synth: dict | None) -> list[tuple]:
    return [(l["dept"], l["code_insee"], l["url_page_source"], l["url_cible"], l["nom_commune"])
            for l in (liens_confirmes(dept, synth) if synth is not None else [])]


def ensure_schema(conn) -> None:
    """Crée les tables manquantes. À la création de liens_vus, amorce la table avec les résultats déjà publiés
    (nouveau = FALSE, datés de leur publication) : l'existant avant la fonctionnalité n'est pas compté comme nouveau."""
    existait = conn.execute("SELECT to_regclass('liens_vus') IS NOT NULL").fetchone()[0]
    conn.execute(SCHEMA_SQL)
    if not existait:
        base = []
        for dept, brut, publie in conn.execute("SELECT dept, synthese_json, publie_le FROM publications").fetchall():
            base.extend(lien + (publie,) for lien in _lignes_liens(dept, _objet_json(brut)))
        if base:
            with conn.cursor() as cur:
                cur.executemany(_INSERT_LIEN_BASE, [(d, c, p, t, n, when) for d, c, p, t, n, when in base])
    conn.commit()


def publish(source: PublicationSource, conn, only: list[str] | None = None) -> dict[str, Any]:
    """Écrit chaque département de `source` (une transaction par département) puis le CSV global s'il existe.

    `only` : si fourni et différent de ['all'], ne publie que ces départements (ceux de `source` qui y figurent).
    Le CSV global est publié dans tous les cas s'il existe.

    Les départements déjà publiés et absents de la source ne sont pas supprimés.
    """
    ensure_schema(conn)       # idempotent : une base publiée avant l'ajout d'une colonne est mise à niveau ici
    restreint = None if only is None or list(only) == ["all"] else set(only)
    published: list[str] = []
    for dept in source.depts():
        if restreint is not None and dept not in restreint:
            continue
        pub = source.get(dept)
        if pub is None:
            continue
        liens_json = json.dumps([{"code_insee": r["code_insee"], "nom_commune": r["nom_commune"],
                                  "url_page": r["url_page"]} for r in source.liens(dept)], ensure_ascii=False)
        synth = source.synthese(dept)
        synthese_json = json.dumps(synth, ensure_ascii=False) if synth is not None else "{}"
        resume_json = json.dumps(resume_synthese(synth), ensure_ascii=False) if synth is not None else "{}"
        try:
            conn.execute(_UPSERT_DEPT, (pub.dept, pub.controle_date, pub.communes, pub.avec_lien, pub.report_html,
                                        pub.csv_bytes, pub.csv_present, pub.resume_changements, liens_json,
                                        synthese_json, resume_json))
            with conn.cursor() as cur:  # même transaction : première apparition des liens nouvellement confirmés
                cur.executemany(_INSERT_LIEN_VU, _lignes_liens(dept, synth))
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
        self._liens: dict[str, tuple[float, list[dict]]] = {}  # liens confirmés par département ('' = tous)
        _psycopg()  # erreur claire dès la construction si psycopg manque

    def _connect(self):
        return _psycopg().connect(self.url, connect_timeout=CONNECT_TIMEOUT_S)

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

    def liens(self, dept: str | None = None) -> list[dict]:
        """Communes avec lien, relues de liens_json (une requête). JSON invalide ou non-liste : département ignoré."""
        with self._connect() as conn:
            if dept is None:
                rows = conn.execute("SELECT dept, liens_json FROM publications ORDER BY dept").fetchall()
            else:
                rows = conn.execute("SELECT dept, liens_json FROM publications WHERE dept = %s ORDER BY dept",
                                    (dept,)).fetchall()
        sortie: list[dict] = []
        for d, brut in rows:
            try:
                entrees = json.loads(brut)
            except (TypeError, ValueError):
                continue
            if not isinstance(entrees, list):
                continue
            for e in entrees:
                if not isinstance(e, dict):
                    continue
                sortie.append({"dept": d, "code_insee": str(e.get("code_insee") or ""),
                               "nom_commune": str(e.get("nom_commune") or ""), "url_page": str(e.get("url_page") or "")})
        return trier_liens(sortie)

    def _liens_confirmes(self, dept: str | None) -> list[dict]:
        """Liens confirmés de synthese_json (seule colonne lue : pas de compte rendu ni de CSV), gardés CACHE_LEGER_S s.

        Une ligne sans synthèse (« {} », JSON invalide) est ignorée sans erreur. Chaque lien reçoit premiere_vue et
        nouveau, lus de liens_vus (None et False si le lien n'y figure pas).
        """
        cle = dept or ""
        with self._verrou:
            entree = self._liens.get(cle)
            if entree is not None and time.monotonic() - entree[0] < CACHE_LEGER_S:
                return entree[1]
        with self._connect() as conn:
            if dept is None:
                rows = conn.execute("SELECT dept, synthese_json FROM publications ORDER BY dept").fetchall()
                vues = conn.execute("SELECT departement, code_insee, url_page_source, url_cible, premiere_vue, nouveau "
                                    "FROM liens_vus").fetchall()
            else:
                rows = conn.execute("SELECT dept, synthese_json FROM publications WHERE dept = %s",
                                    (dept,)).fetchall()
                vues = conn.execute("SELECT departement, code_insee, url_page_source, url_cible, premiere_vue, nouveau "
                                    "FROM liens_vus WHERE departement = %s", (dept,)).fetchall()
        connus = {(v[0], v[1], v[2], v[3]): (v[4], bool(v[5])) for v in vues}
        lignes: list[dict] = []
        for d, brut in rows:
            synth = _objet_json(brut)
            if synth is not None:
                lignes.extend(liens_confirmes(d, synth))
        for lien in lignes:
            vu = connus.get((lien["dept"], lien["code_insee"], lien["url_page_source"], lien["url_cible"]))
            lien["premiere_vue"] = vu[0].isoformat() if vu else None
            lien["nouveau"] = vu[1] if vu else False
        with self._verrou:
            self._liens[cle] = (time.monotonic(), lignes)
        return lignes

    def tous_les_liens(self, q: str, dept: str | None, limit: int, offset: int) -> tuple[list[dict], int]:
        """Une ligne par lien confirmé, filtrée (q, dept), triée, paginée en mémoire sur le cache court."""
        return selection_liens(self._liens_confirmes(dept), q, limit, offset)

    def nouveaux_liens(self, depuis: datetime, limit: int) -> tuple[list[dict], int]:
        """Liens nouveaux depuis `depuis`, plus récents d'abord : LIMIT/OFFSET et COUNT côté base (index partiel)."""
        with self._connect() as conn:
            lignes = conn.execute(
                "SELECT departement, code_insee, commune, url_page_source, url_cible, premiere_vue FROM liens_vus "
                "WHERE nouveau AND premiere_vue >= %s "
                "ORDER BY premiere_vue DESC, departement, commune, code_insee, url_page_source, url_cible LIMIT %s",
                (depuis, int(limit))).fetchall()
            total = conn.execute("SELECT count(*) FROM liens_vus WHERE nouveau AND premiere_vue >= %s",
                                 (depuis,)).fetchone()[0]
        return [{"dept": r[0], "code_insee": r[1], "nom_commune": r[2], "url_page_source": r[3], "url_cible": r[4],
                 "premiere_vue": r[5].isoformat(), "nouveau": True} for r in lignes], int(total)

    def csv_all(self) -> bytes | None:
        with self._connect() as conn:
            row = conn.execute("SELECT contenu FROM publication_globale WHERE nom = %s", (CSV_TOUS,)).fetchone()
        return None if row is None else bytes(row[0])

    def has_csv_all(self) -> bool:
        """Existence du CSV global sans transférer son contenu (BYTEA non lu)."""
        with self._connect() as conn:
            row = conn.execute("SELECT EXISTS(SELECT 1 FROM publication_globale WHERE nom = %s)", (CSV_TOUS,)).fetchone()
        return bool(row[0])

    def synthese(self, dept: str) -> dict | None:
        """Synthèse publiée du département ; None si absente, vide ou JSON invalide."""
        with self._connect() as conn:
            row = conn.execute("SELECT synthese_json FROM publications WHERE dept = %s", (dept,)).fetchone()
        return None if row is None else _objet_json(row[0])

    def resumes(self) -> list[dict]:
        """Un résumé par département publié, lu en une seule requête (pas de synthèse complète). Trié par code."""
        with self._connect() as conn:
            rows = conn.execute("SELECT dept, resume_json FROM publications ORDER BY dept").fetchall()
        sortie: list[dict] = []
        for dept, brut in rows:
            resume = _objet_json(brut)
            if resume is not None:
                resume.setdefault("code", dept)
                sortie.append(resume)
        return sorted(sortie, key=lambda r: r["code"])

    def analyses(self, limit: int = 50) -> list[dict]:
        with self._connect() as conn:
            lignes = analysis_list(conn, int(limit))
        return [_analyse_publique(ligne) for ligne in lignes]

    def analyse(self, analysis_id: int) -> dict | None:
        with self._connect() as conn:
            brut = analysis_get(conn, int(analysis_id))
        if brut is None:
            return None
        ligne = {**brut, "departements": _liste_departements(brut.get("departements"))}
        return {**_analyse_publique(ligne), "journal": brut.get("journal") or ""}


def _objet_json(brut) -> dict | None:
    """Objet JSON non vide ; None pour '{}', une valeur non objet ou un JSON invalide."""
    try:
        valeur = json.loads(brut)
    except (TypeError, ValueError):
        return None
    return valeur if isinstance(valeur, dict) and valeur else None


def _liste_departements(texte: str | None) -> list[str]:
    return [d for d in (texte or "").split(",") if d]


def _analyse_publique(ligne: dict) -> dict:
    """Forme publique d'une analyse : id, statut, departements (liste), debut et fin en ISO (fin None si en cours)."""
    debut, fin = ligne.get("debut"), ligne.get("fin")
    return {"id": int(ligne["id"]), "statut": ligne["statut"], "departements": list(ligne["departements"]),
            "debut": debut.isoformat() if debut else "", "fin": fin.isoformat() if fin else None}


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


def analysis_cancel_unstarted(conn, analysis_id: int) -> bool:
    """Annule une analyse jamais démarrée côté GitHub (statut 'file', sans run_id). True si une ligne a changé.

    Si le CI s'est attaché entre-temps (run_id renseigné), la condition ne correspond plus : False, et l'appelant
    doit relire l'analyse pour annuler le run GitHub.
    """
    cur = conn.execute(
        "UPDATE analyses SET statut = 'annulee', fin = now() WHERE id = %s AND statut = 'file' AND run_id = ''",
        (analysis_id,),
    )
    conn.commit()
    return cur.rowcount == 1


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


def analysis_end_by_run(conn, run_id: str, statut: str) -> bool:
    """Comme analysis_end, pour l'analyse active rattachée à ce run GitHub (lancement direct sans numéro connu)."""
    if not run_id:
        return False
    cur = conn.execute(
        "UPDATE analyses SET statut = %s, fin = now() WHERE run_id = %s AND statut IN ('file','en_cours')",
        (statut, run_id),
    )
    conn.commit()
    return cur.rowcount >= 1


def analysis_get(conn, analysis_id: int) -> dict | None:
    cur = conn.execute("SELECT * FROM analyses WHERE id = %s", (analysis_id,))
    row = cur.fetchone()
    names = [d.name for d in cur.description]
    conn.commit()
    return None if row is None else dict(zip(names, row))


def analysis_list(conn, limit: int = 50) -> list[dict]:
    """Analyses du plus récent au plus ancien, sans le journal ; `departements` converti en liste."""
    cur = conn.execute(
        "SELECT id, departements, reprise, statut, debut, fin, run_id FROM analyses "
        "ORDER BY debut DESC, id DESC LIMIT %s",
        (int(limit),),
    )
    names = [d.name for d in cur.description]
    rows = cur.fetchall()
    conn.commit()
    return [{**dict(zip(names, row)), "departements": _liste_departements(row[1])} for row in rows]


def analysis_current(conn) -> dict | None:
    cur = conn.execute("SELECT * FROM analyses ORDER BY debut DESC, id DESC LIMIT 1")
    row = cur.fetchone()
    names = [d.name for d in cur.description]
    conn.commit()
    return None if row is None else dict(zip(names, row))
