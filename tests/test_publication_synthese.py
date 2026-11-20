"""Publication de la synthèse : FileSource (fichiers) et PostgresSource (base) doivent donner les mêmes réponses ;
une base ancienne, sans les colonnes synthese_json / resume_json, est mise à niveau sans perte.

Sans Postgres (MLM_TEST_DATABASE_URL absente ou injoignable), seules les classes FileSource tournent.
"""
import os
import shutil
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support_publications as sp  # noqa: E402  (chemins src/ et tests/)
import store_pg  # noqa: E402
from publications import FileSource  # noqa: E402


class FileSourceSyntheseTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        sp.construire_racine(self.root)
        self.src = FileSource(self.root)

    def test_synthese_d_un_departement_publie(self):
        s = self.src.synthese("60")
        self.assertIsNotNone(s)
        self.assertEqual(set(s), {"departement", "campagne", "totaux", "communes"})
        self.assertEqual(s["departement"], {"code": "60", "nom": "Oise"})
        self.assertEqual({c["code_insee"]: c["statut"] for c in s["communes"]}, {"60001": "lien_confirme"})

    def test_synthese_sans_inventaire_ou_inconnue_donne_none(self):
        self.assertIsNone(self.src.synthese("02"))                  # rapport seul, pas d'inventaire
        self.assertIsNone(self.src.synthese("99"))

    def test_resumes_un_par_departement_trie(self):
        codes = [r["code"] for r in self.src.resumes()]
        self.assertEqual(codes, ["60", "80"])                       # 02 sans synthèse : absent
        self.assertEqual({r["code"]: r["nb_communes_avec_lien"] for r in self.src.resumes()}, {"60": 1, "80": 0})

    def test_analyses_vides_sur_fichiers(self):
        self.assertEqual(self.src.analyses(10), [])
        self.assertIsNone(self.src.analyse(1))


@unittest.skipIf(bool(sp.RAISON_PG), sp.RAISON_PG)
class PublicationPostgresTests(sp.PgSchemaTestCase):
    def setUp(self):
        self.vider_tables()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        sp.construire_racine(self.root)
        self.src = FileSource(self.root)
        conn = store_pg.connect(self.url)
        try:
            self.publie = store_pg.publish(self.src, conn)
        finally:
            conn.close()
        self.pg = store_pg.PostgresSource(self.url)

    def test_publish_renvoie_les_departements(self):
        self.assertEqual(self.publie["departements"], ["02", "60", "80"])   # 02 : rapport seul, publié sans synthèse
        self.assertTrue(self.publie["csv_global"])

    def test_synthese_identique_a_fichiers(self):
        for dept in ("60", "80"):
            with self.subTest(dept=dept):
                self.assertEqual(self.pg.synthese(dept), self.src.synthese(dept))

    def test_synthese_absente_donne_none(self):
        self.assertIsNone(self.pg.synthese("02"))
        self.assertIsNone(self.pg.synthese("99"))

    def test_resumes_identiques_a_fichiers(self):
        self.assertEqual(self.pg.resumes(), self.src.resumes())

    def test_republier_remplace_la_synthese(self):
        # Une nouvelle publication du même département remplace la synthèse précédente (pas de cumul).
        s1 = self.pg.synthese("60")
        conn = store_pg.connect(self.url)
        try:
            store_pg.publish(self.src, conn, only=["60"])
        finally:
            conn.close()
        self.assertEqual(self.pg.synthese("60"), s1)


@unittest.skipIf(bool(sp.RAISON_PG), sp.RAISON_PG)
class MiseAJourBaseAncienneTests(unittest.TestCase):
    """Base créée avant les colonnes synthese_json / resume_json : ensure_schema les ajoute sans perdre les lignes."""

    def setUp(self):
        import psycopg
        self.base = os.environ[sp.URL_ENV].strip()
        self.schema = "mlm_old_" + uuid.uuid4().hex[:10]
        with psycopg.connect(self.base, autocommit=True) as conn:
            conn.execute(f"CREATE SCHEMA {self.schema}")
        self.addCleanup(sp._supprimer_schema, self.base, self.schema)
        sep = "&" if "?" in self.base else "?"
        self.url = self.base + sep + "options=-csearch_path%3D" + self.schema
        with psycopg.connect(self.url, autocommit=True) as conn:
            conn.execute("""CREATE TABLE publications (dept TEXT PRIMARY KEY, controle_date TEXT NOT NULL DEFAULT '',
                communes INTEGER NOT NULL DEFAULT 0, avec_lien INTEGER NOT NULL DEFAULT 0,
                report_html TEXT NOT NULL DEFAULT '', csv_bytes BYTEA NOT NULL DEFAULT ''::bytea,
                csv_present BOOLEAN NOT NULL DEFAULT FALSE, resume_changements TEXT NOT NULL DEFAULT '',
                liens_json TEXT NOT NULL DEFAULT '[]', publie_le TIMESTAMPTZ NOT NULL DEFAULT now())""")
            conn.execute("INSERT INTO publications (dept, controle_date, communes, report_html, csv_bytes, csv_present) "
                         "VALUES ('77', 'ancien', 3, '<h1>Ancien rapport 77</h1>', %s, TRUE)", (b"code;url\r\n",))
            conn.execute("INSERT INTO publications (dept, controle_date, communes) VALUES ('60', 'ancien', 3)")

    def colonnes(self) -> set[str]:
        import psycopg
        with psycopg.connect(self.base) as conn:
            return {r[0] for r in conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema = %s AND table_name = 'publications'",
                (self.schema,)).fetchall()}

    def test_colonnes_ajoutees_et_mise_a_niveau_idempotente(self):
        conn = store_pg.connect(self.url)
        try:
            store_pg.ensure_schema(conn)
            store_pg.ensure_schema(conn)
        finally:
            conn.close()
        self.assertTrue({"synthese_json", "resume_json"} <= self.colonnes())

    def test_lignes_existantes_conservees(self):
        conn = store_pg.connect(self.url)
        try:
            store_pg.ensure_schema(conn)
        finally:
            conn.close()
        pg = store_pg.PostgresSource(self.url)
        ancien = pg.get("77", report=True, csv=True)
        self.assertIsNotNone(ancien)
        self.assertEqual(ancien.controle_date, "ancien")
        self.assertEqual(ancien.report_html, "<h1>Ancien rapport 77</h1>")
        self.assertEqual(ancien.csv_bytes, b"code;url\r\n")
        self.assertIsNone(pg.synthese("77"))                       # pas de synthèse pour une ligne ancienne

    def test_publication_apres_mise_a_niveau_conserve_l_ancien_et_ajoute_la_synthese(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        sp.construire_racine(root)
        conn = store_pg.connect(self.url)
        try:
            resultat = store_pg.publish(FileSource(root), conn)
        finally:
            conn.close()
        self.assertEqual(resultat["departements"], ["02", "60", "80"])
        pg = store_pg.PostgresSource(self.url)
        self.assertEqual(pg.synthese("60"), FileSource(root).synthese("60"))
        self.assertEqual(pg.get("77", report=True, csv=True).report_html, "<h1>Ancien rapport 77</h1>")   # sans perte
        self.assertEqual(pg.get("60", report=False, csv=False).controle_date, FileSource(root).synthese("60")["campagne"]["fin"])


if __name__ == "__main__":
    unittest.main()
