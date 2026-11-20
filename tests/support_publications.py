"""Outils partagés des tests de publication : racine de fixtures, base Postgres de test, faux crawler.

Base Postgres : URL lue dans MLM_TEST_DATABASE_URL. Sans variable, base injoignable ou psycopg absent, les classes qui
l'utilisent sont ignorées (unittest.skip). Chaque classe travaille dans son propre schéma temporaire.
Aucun réseau externe.
"""
from __future__ import annotations

import os
import sys
import unittest
import uuid
from pathlib import Path

RACINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RACINE / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import csvio  # noqa: E402
import history  # noqa: E402
from layout import DeptPaths  # noqa: E402

URL_ENV = "MLM_TEST_DATABASE_URL"
BOM = b"\xef\xbb\xbf"
CSV_TOUS_OCTETS = "﻿code_insee;url_page\r\n60001;https://a.fr/é\r\n".encode("utf-8")


def raison_sans_postgres() -> str:
    """Chaîne vide si la base de test est utilisable, sinon la raison du saut. Seul le type d'erreur est affiché."""
    url = os.environ.get(URL_ENV, "").strip()
    if not url:
        return f"{URL_ENV} absente : test Postgres ignoré"
    try:
        import psycopg
    except ImportError:
        return "psycopg non installé : test Postgres ignoré"
    try:
        psycopg.connect(url, connect_timeout=5).close()
    except Exception as exc:  # noqa: BLE001 : l'URL peut contenir des informations de connexion, jamais affichées
        return f"base de test injoignable ({type(exc).__name__}) : test Postgres ignoré"
    return ""


RAISON_PG = raison_sans_postgres()


def _supprimer_schema(url_base: str, schema: str) -> None:
    import psycopg
    with psycopg.connect(url_base, autocommit=True) as conn:
        conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")


class PgSchemaTestCase(unittest.TestCase):
    """Un schéma temporaire par classe, créé avec le schéma de l'application.

    Le schéma est supprimé par `addClassCleanup`, donc aussi lorsque la mise en place (setUpClass) échoue.
    `setUp` vide les tables pour que chaque test parte de zéro.
    """

    url_base = ""
    schema = ""
    url = ""

    @classmethod
    def setUpClass(cls):
        import psycopg
        import store_pg

        super().setUpClass()
        cls.url_base = os.environ[URL_ENV].strip()
        cls.schema = "mlm_test_" + uuid.uuid4().hex[:12]
        sep = "&" if "?" in cls.url_base else "?"
        cls.url = cls.url_base + sep + "options=-csearch_path%3D" + cls.schema
        with psycopg.connect(cls.url_base, autocommit=True) as conn:
            conn.execute(f"CREATE SCHEMA {cls.schema}")
        cls.addClassCleanup(_supprimer_schema, cls.url_base, cls.schema)
        conn = store_pg.connect(cls.url)
        try:
            store_pg.ensure_schema(conn)
        finally:
            conn.close()

    def vider_tables(self) -> None:
        import psycopg
        with psycopg.connect(self.url, autocommit=True) as conn:
            conn.execute("TRUNCATE publications, publication_globale, analyses, liens_vus RESTART IDENTITY")


def construire_racine(root: Path) -> None:
    """Racine de test : 60 complet (contrôle + CSV), 80 contrôlé sans CSV, 02 rapport seul, CSV global avec BOM."""
    p = DeptPaths(root, "60")
    p.reports_dir.mkdir(parents=True)
    csvio.write_rows(p.inventory_communes, [{"code_insee": "60001", "nom_commune": "A", "nb_urls_valides": "1"}])
    csvio.write_rows(p.links_found, [{"code_insee": "60001", "site_url": "https://a.fr/", "page_url": "https://a.fr/",
                                      "type_lien": "lien"}])
    csvio.write_rows(p.result_csv, [{"code_insee": "60001", "url_page": "https://a.fr/"}])
    history.record(p, history.db_path(root))
    p80 = DeptPaths(root, "80")
    p80.reports_dir.mkdir(parents=True)
    csvio.write_rows(p80.inventory_communes, [{"code_insee": "80001", "nom_commune": "B", "nb_urls_valides": "0"}])
    history.record(p80, history.db_path(root))
    (root / "reports" / "02").mkdir(parents=True)
    (root / "resultats").mkdir(exist_ok=True)
    (root / "resultats" / "liens_tous_departements.csv").write_bytes(CSV_TOUS_OCTETS)


# Faux crawler pour les tests de ci_analyse : écrit des marqueurs dans la racine, puis sort selon FAUX_MODE.
#   ok      : crée reports/60 et le CSV global, sort 0
#   echec   : sort 1 après un message d'erreur
#   attente : écrit pret.txt (avec son pid) puis dort 30 s (la durée réelle est limitée par le SIGTERM du test)
FAUX_CRAWLER = '''
import os, sys, time
from pathlib import Path

args = sys.argv[1:]
root = Path(args[args.index("--root") + 1])
print("faux crawler : " + " ".join(args), flush=True)
(root / "demarre.txt").write_text("1", encoding="utf-8")
(root / "reports" / "60").mkdir(parents=True, exist_ok=True)
(root / "resultats").mkdir(exist_ok=True)
(root / "resultats" / "liens_tous_departements.csv").write_bytes("code_insee;url_page\\r\\n60001;https://a.fr/\\r\\n".encode())
mode = os.environ.get("FAUX_MODE", "ok")
if mode == "attente":
    (root / "pret.txt").write_text(str(os.getpid()), encoding="utf-8")
    time.sleep(30)
if mode == "echec":
    print("erreur simulee", file=sys.stderr, flush=True)
    sys.exit(1)
sys.exit(0)
'''
