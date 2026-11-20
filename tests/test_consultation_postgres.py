"""Espace « consultation » servi depuis Postgres (PostgresSource) : même contrat que test_consultation.py.

Sans MLM_TEST_DATABASE_URL (ou base injoignable), la classe est ignorée ; le contrat reste couvert par les tests fichiers.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support_publications as sp  # noqa: E402
import store_pg  # noqa: E402
import webapp  # noqa: E402
from publications import FileSource  # noqa: E402
from test_consultation import ContratLecture, ServeurLecture  # noqa: E402


@unittest.skipIf(bool(sp.RAISON_PG), sp.RAISON_PG)
class LecturePostgres(ContratLecture, ServeurLecture, sp.PgSchemaTestCase):
    lecture = True

    def preparer(self, root: Path) -> None:
        sp.construire_racine(root)
        self.vider_tables()
        conn = store_pg.connect(self.url)
        try:
            store_pg.publish(FileSource(root), conn)
        finally:
            conn.close()

    def creer_source(self):
        return store_pg.PostgresSource(self.url)

    def test_donnees_lues_dans_postgres(self):
        self.assertIn("Oise", self.html("/"))
        self.assertIn("Oise (60)", self.html("/departement/60"))

    def test_csv_identique_a_la_source_fichiers(self):
        attendu = webapp.csv_communes(FileSource(self.root).synthese("60")).decode("utf-8")
        self.assertEqual(self.appel("GET", "/departement/60.csv")[2], attendu)


if __name__ == "__main__":
    unittest.main()
