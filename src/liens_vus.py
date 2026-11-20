"""Mémoire des liens confirmés : date de première apparition de chaque lien (FileSource, base SQLite locale).

Table `liens_vus` dans data/historique.sqlite, clé (departement, code_insee, url_page_source, url_cible). Une ligne n'est
jamais supprimée ni réécrite : un lien disparu puis réapparu garde sa première date.

Amorçage : la table est créée à la première synchronisation. Les liens déjà présents à ce moment reçoivent
nouveau = 0 (ils ne sont pas comptés comme nouveaux) et la date du dernier contrôle de leur département. Les liens
trouvés ensuite reçoivent nouveau = 1 et la date de la synchronisation qui les voit (en local, la première lecture
après le contrôle, faute de hook dans le moteur de crawl).
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS liens_vus (
    departement TEXT NOT NULL, code_insee TEXT NOT NULL, url_page_source TEXT NOT NULL, url_cible TEXT NOT NULL,
    commune TEXT NOT NULL, premiere_vue TEXT NOT NULL, nouveau INTEGER NOT NULL,
    PRIMARY KEY (departement, code_insee, url_page_source, url_cible));
"""
_INSERT = ("INSERT OR IGNORE INTO liens_vus (departement, code_insee, url_page_source, url_cible, commune, "
           "premiere_vue, nouveau) VALUES (?, ?, ?, ?, ?, ?, ?)")
_LIRE = "SELECT departement, code_insee, url_page_source, url_cible, premiere_vue, nouveau FROM liens_vus"


def cle(lien: dict) -> tuple[str, str, str, str]:
    return (lien["dept"], lien["code_insee"], lien["url_page_source"], lien["url_cible"])


def synchroniser(db: Path, lignes: list[dict], dates_base: dict[str, str], maintenant: str) -> dict[tuple, tuple[str, bool]]:
    """Enregistre les liens inconnus et renvoie {cle: (premiere_vue, nouveau)} pour les `lignes` fournies.

    `dates_base` : date du dernier contrôle par département, utilisée seulement pour l'amorçage.
    """
    db.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db)) as conn, conn:
        amorcage = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'liens_vus'").fetchone() is None
        conn.executescript(SCHEMA)
        if amorcage:
            params = [(*cle(l), l["nom_commune"], dates_base.get(l["dept"]) or maintenant, 0) for l in lignes]
        else:
            params = [(*cle(l), l["nom_commune"], maintenant, 1) for l in lignes]
        conn.executemany(_INSERT, params)
        connus = {row[:4]: (row[4], bool(row[5])) for row in conn.execute(_LIRE)}
    return {cle(l): connus[cle(l)] for l in lignes if cle(l) in connus}


def oublier(db: Path) -> None:
    """Supprime la table (migration de test : la prochaine synchronisation refait l'amorçage)."""
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("DROP TABLE IF EXISTS liens_vus")
