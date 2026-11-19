"""Stockage Postgres des publications et suivi des analyses (store_pg). Ignoré sans MLM_TEST_DATABASE_URL."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support_publications as sp  # noqa: E402
from publications import FileSource  # noqa: E402

import store_pg  # noqa: E402


@unittest.skipIf(bool(sp.RAISON_PG), sp.RAISON_PG)
class SchemaPgTests(sp.PgSchemaTestCase):
    def test_ensure_schema_idempotent(self):
        conn = store_pg.connect(self.url)
        try:
            store_pg.ensure_schema(conn)
            store_pg.ensure_schema(conn)           # second appel : aucune erreur, aucun changement
            tables = {r[0] for r in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()").fetchall()}
        finally:
            conn.close()
        self.assertTrue({"publications", "publication_globale", "analyses"} <= tables, tables)


@unittest.skipIf(bool(sp.RAISON_PG), sp.RAISON_PG)
class PublicationPgTests(sp.PgSchemaTestCase):
    def setUp(self):
        self.vider_tables()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        sp.construire_racine(self.root)
        self.conn = store_pg.connect(self.url)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def compter(self, table: str) -> int:
        return self.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]

    def publier(self):
        return store_pg.publish(FileSource(self.root), self.conn)

    def test_publish_puis_lecture_identique_a_filesource(self):
        res = self.publier()
        self.assertEqual(res, {"departements": ["02", "60", "80"], "csv_global": True})
        fichiers, pg = FileSource(self.root), store_pg.PostgresSource(self.url)
        self.assertEqual(pg.depts(), fichiers.depts())
        self.assertEqual(pg.csv_all(), fichiers.csv_all())
        for dept in fichiers.depts():
            with self.subTest(dept=dept):
                self.assertEqual(pg.get(dept), fichiers.get(dept))   # HTML et octets du CSV compris

    def test_get_leger_identique_sans_rapport_ni_csv(self):
        self.publier()
        fichiers, pg = FileSource(self.root), store_pg.PostgresSource(self.url)
        leger = pg.get("60", report=False, csv=False)
        self.assertEqual(leger.report_html, "")
        self.assertEqual(leger.csv_bytes, b"")
        ref = fichiers.get("60", report=False, csv=False)
        self.assertEqual(leger, ref)

    def test_get_departement_non_publie_none(self):
        self.publier()
        self.assertIsNone(store_pg.PostgresSource(self.url).get("99"))
        self.assertIsNone(store_pg.PostgresSource(self.url).get("99", report=False, csv=False))

    def test_publier_deux_fois_ne_duplique_rien(self):
        self.publier()
        self.publier()
        self.assertEqual(self.compter("publications"), 3)
        self.assertEqual(self.compter("publication_globale"), 1)

    def test_publier_deux_fois_met_a_jour_le_contenu(self):
        self.publier()
        (self.root / "resultats" / "liens_60.csv").write_bytes(b"code;url\r\n60002;https://b.fr/\r\n")
        self.publier()
        self.assertEqual(store_pg.PostgresSource(self.url).get("60").csv_bytes, b"code;url\r\n60002;https://b.fr/\r\n")

    def test_departement_absent_de_la_source_non_supprime(self):
        self.publier()
        (self.root / "reports" / "02").rmdir()
        res = self.publier()
        self.assertEqual(res["departements"], ["60", "80"])
        pg = store_pg.PostgresSource(self.url)
        self.assertIn("02", pg.depts())
        self.assertIsNotNone(pg.get("02"))

    def test_csv_global_absent_ne_supprime_pas_le_precedent(self):
        self.publier()
        (self.root / "resultats" / "liens_tous_departements.csv").unlink()
        res = self.publier()
        self.assertFalse(res["csv_global"])
        self.assertEqual(store_pg.PostgresSource(self.url).csv_all(), sp.CSV_TOUS_OCTETS)

    def test_csv_all_absent_none(self):
        self.assertIsNone(store_pg.PostgresSource(self.url).csv_all())

    def test_depts_vide_avant_publication(self):
        self.assertEqual(store_pg.PostgresSource(self.url).depts(), [])


@unittest.skipIf(bool(sp.RAISON_PG), sp.RAISON_PG)
class CachePgTests(sp.PgSchemaTestCase):
    def setUp(self):
        self.vider_tables()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        sp.construire_racine(self.root)
        conn = store_pg.connect(self.url)
        try:
            store_pg.publish(FileSource(self.root), conn)
        finally:
            conn.close()
        import psycopg
        self.psycopg = psycopg

    def tearDown(self):
        self.tmp.cleanup()

    def test_depts_puis_get_leger_une_seule_connexion(self):
        pg = store_pg.PostgresSource(self.url)
        with mock.patch("psycopg.connect", wraps=self.psycopg.connect) as spy:
            pg.depts()
            pg.get("60", report=False, csv=False)
            pg.get("80", report=False, csv=False)
        self.assertEqual(spy.call_count, 1)

    def test_get_avec_rapport_ou_csv_reconnecte(self):
        pg = store_pg.PostgresSource(self.url)
        with mock.patch("psycopg.connect", wraps=self.psycopg.connect) as spy:
            pg.depts()
            pg.get("60")                 # rapport et CSV demandés : requête ciblée
        self.assertEqual(spy.call_count, 2)

    def test_cache_expire(self):
        pg = store_pg.PostgresSource(self.url)
        with mock.patch.object(store_pg, "CACHE_LEGER_S", 0.0), \
                mock.patch("psycopg.connect", wraps=self.psycopg.connect) as spy:
            pg.depts()
            pg.get("60", report=False, csv=False)
        self.assertEqual(spy.call_count, 2)

    def test_cache_leger_coherent_avec_lecture_complete(self):
        pg = store_pg.PostgresSource(self.url)
        pg.depts()
        leger = pg.get("60", report=False, csv=False)
        self.assertEqual(leger.controle_date, pg.get("60").controle_date)
        self.assertIsNone(pg.get("99", report=False, csv=False))


@unittest.skipIf(bool(sp.RAISON_PG), sp.RAISON_PG)
class SuiviAnalysesPgTests(sp.PgSchemaTestCase):
    def setUp(self):
        self.vider_tables()
        self.conn = store_pg.connect(self.url)

    def tearDown(self):
        self.conn.close()

    def test_start_enregistre_departements_reprise_et_run(self):
        aid = store_pg.analysis_start(self.conn, ["60", "80"], True, "run-1")
        cur = store_pg.analysis_current(self.conn)
        self.assertEqual((cur["id"], cur["statut"], cur["departements"], cur["reprise"], cur["run_id"], cur["fin"]),
                         (aid, "en_cours", "60,80", True, "run-1", None))
        self.assertEqual(cur["journal"], "")

    def test_queue_refuse_tant_qu_une_analyse_est_active(self):
        a1 = store_pg.analysis_queue(self.conn, ["60"], False)
        self.assertIsInstance(a1, int)
        self.assertIsNone(store_pg.analysis_queue(self.conn, ["80"], True))
        self.assertEqual(store_pg.analysis_get(self.conn, a1)["statut"], "file")
        store_pg.analysis_end(self.conn, a1, "echec")
        self.assertIsInstance(store_pg.analysis_queue(self.conn, ["80"], True), int)

    def test_queue_refuse_si_en_cours(self):
        store_pg.analysis_start(self.conn, ["60"], False)
        self.assertIsNone(store_pg.analysis_queue(self.conn, ["60"], False))

    def test_attach_run_passe_de_file_a_en_cours(self):
        aid = store_pg.analysis_queue(self.conn, ["60"], False)
        self.assertTrue(store_pg.analysis_attach_run(self.conn, aid, "run-7"))
        row = store_pg.analysis_get(self.conn, aid)
        self.assertEqual((row["statut"], row["run_id"]), ("en_cours", "run-7"))

    def test_attach_run_sur_en_cours_remplace_seulement_run_id(self):
        aid = store_pg.analysis_start(self.conn, ["60"], False, "run-1")
        self.assertTrue(store_pg.analysis_attach_run(self.conn, aid, "run-2"))
        row = store_pg.analysis_get(self.conn, aid)
        self.assertEqual((row["statut"], row["run_id"]), ("en_cours", "run-2"))

    def test_attach_run_sur_analyse_annulee_refuse(self):
        aid = store_pg.analysis_queue(self.conn, ["60"], False)
        store_pg.analysis_end(self.conn, aid, "annulee")
        self.assertFalse(store_pg.analysis_attach_run(self.conn, aid, "run-x"))
        row = store_pg.analysis_get(self.conn, aid)
        self.assertEqual((row["statut"], row["run_id"]), ("annulee", ""))

    def test_end_ne_modifie_pas_un_statut_final(self):
        aid = store_pg.analysis_start(self.conn, ["60"], False)
        self.assertTrue(store_pg.analysis_end(self.conn, aid, "terminee"))
        fin = store_pg.analysis_get(self.conn, aid)["fin"]
        self.assertIsNotNone(fin)
        self.assertFalse(store_pg.analysis_end(self.conn, aid, "echec"))
        row = store_pg.analysis_get(self.conn, aid)
        self.assertEqual((row["statut"], row["fin"]), ("terminee", fin))

    def test_end_depuis_file(self):
        aid = store_pg.analysis_queue(self.conn, ["60"], False)
        self.assertTrue(store_pg.analysis_end(self.conn, aid, "annulee"))
        self.assertEqual(store_pg.analysis_get(self.conn, aid)["statut"], "annulee")

    def test_end_identifiant_inconnu(self):
        self.assertFalse(store_pg.analysis_end(self.conn, 999999, "echec"))
        self.assertIsNone(store_pg.analysis_get(self.conn, 999999))

    def test_log_tronque_a_4000_caracteres(self):
        aid = store_pg.analysis_start(self.conn, ["60"], False)
        store_pg.analysis_log(self.conn, aid, "a" * 3000)
        store_pg.analysis_log(self.conn, aid, "b" * 3000)
        journal = store_pg.analysis_get(self.conn, aid)["journal"]
        self.assertEqual(len(journal), store_pg.JOURNAL_MAX)
        self.assertEqual(journal, "a" * 1000 + "b" * 3000)     # on garde la fin

    def test_log_accumule_dans_l_ordre(self):
        aid = store_pg.analysis_start(self.conn, ["60"], False)
        store_pg.analysis_log(self.conn, aid, "un\n")
        store_pg.analysis_log(self.conn, aid, "deux\n")
        self.assertEqual(store_pg.analysis_get(self.conn, aid)["journal"], "un\ndeux\n")

    def test_current_retourne_la_plus_recente(self):
        a1 = store_pg.analysis_queue(self.conn, ["60"], False)
        store_pg.analysis_end(self.conn, a1, "terminee")
        a2 = store_pg.analysis_start(self.conn, ["80"], False)
        self.assertEqual(store_pg.analysis_current(self.conn)["id"], a2)

    def test_current_vide_none(self):
        self.assertIsNone(store_pg.analysis_current(self.conn))

    def test_statut_invalide_refuse(self):
        import psycopg
        aid = store_pg.analysis_start(self.conn, ["60"], False)
        with self.assertRaises(psycopg.errors.CheckViolation):
            store_pg.analysis_end(self.conn, aid, "bidon")
        self.conn.rollback()
        self.assertEqual(store_pg.analysis_get(self.conn, aid)["statut"], "en_cours")


if __name__ == "__main__":
    unittest.main()
