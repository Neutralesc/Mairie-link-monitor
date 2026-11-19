"""Lancement direct de ci_analyse (analysis_id = 0, depuis l'onglet Actions de GitHub) : l'analyse est créée par le script."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support_publications as sp  # noqa: E402
from test_ci_analyse import lancer_ci  # noqa: E402

import ci_analyse  # noqa: E402
import store_pg  # noqa: E402


class EntierTests(unittest.TestCase):
    def test_valeur_vide_ou_blanche_vaut_zero(self):
        self.assertEqual(ci_analyse._entier(""), 0)
        self.assertEqual(ci_analyse._entier("  "), 0)
        self.assertEqual(ci_analyse._entier("0"), 0)
        self.assertEqual(ci_analyse._entier(" 12 "), 12)


@unittest.skipIf(bool(sp.RAISON_PG), sp.RAISON_PG)
class CiLancementDirectPgTests(sp.PgSchemaTestCase):
    def setUp(self):
        self.vider_tables()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        sp.construire_racine(self.root)
        (self.root / "src").mkdir()
        (self.root / "src" / "mairie_link_monitor.py").write_text(sp.FAUX_CRAWLER, encoding="utf-8")

    def analyses(self) -> list[dict]:
        import psycopg
        with psycopg.connect(self.url) as conn:
            cur = conn.execute("SELECT id, statut, run_id, departements FROM analyses ORDER BY id")
            return [dict(zip(("id", "statut", "run_id", "departements"), r)) for r in cur.fetchall()]

    def test_id_zero_cree_l_analyse_la_rattache_au_run_et_la_termine(self):
        r = lancer_ci(["run", "--analysis-id", "0", "--depts", "60", "--root", str(self.root)],
                      DATABASE_URL=self.url, FAUX_MODE="ok", GITHUB_RUN_ID="4242")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Lancement direct : analyse 1 créée.", r.stdout)
        (ligne,) = self.analyses()
        self.assertEqual((ligne["statut"], ligne["run_id"], ligne["departements"]), ("terminee", "4242", "60"))
        self.assertEqual(store_pg.PostgresSource(self.url).depts(), ["60"])

    def test_id_zero_refuse_si_une_analyse_est_deja_active(self):
        conn = store_pg.connect(self.url)
        try:
            store_pg.analysis_queue(conn, ["80"], False)
        finally:
            conn.close()
        r = lancer_ci(["run", "--analysis-id", "0", "--depts", "60", "--root", str(self.root)],
                      DATABASE_URL=self.url, FAUX_MODE="ok", GITHUB_RUN_ID="4243")
        self.assertEqual(r.returncode, ci_analyse.CODE_DEJA_ACTIVE)
        self.assertIn("déjà en cours", r.stderr)
        self.assertEqual([a["statut"] for a in self.analyses()], ["file"])      # rien de créé, rien publié
        self.assertEqual(store_pg.PostgresSource(self.url).depts(), [])

    def test_id_vide_vaut_lancement_direct(self):
        r = lancer_ci(["run", "--analysis-id", "", "--depts", "60", "--root", str(self.root)],
                      DATABASE_URL=self.url, FAUX_MODE="ok", GITHUB_RUN_ID="4244")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([a["statut"] for a in self.analyses()], ["terminee"])

    def test_fin_avec_id_zero_marque_l_analyse_du_run(self):
        conn = store_pg.connect(self.url)
        try:
            aid = store_pg.analysis_queue(conn, ["60"], False)
            store_pg.analysis_attach_run(conn, aid, "777")
        finally:
            conn.close()
        r = lancer_ci(["fin", "--analysis-id", "0", "--statut", "annulee"], DATABASE_URL=self.url, GITHUB_RUN_ID="777")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.analyses()[0]["statut"], "annulee")

    def test_fin_avec_id_zero_ne_touche_pas_un_autre_run_ni_un_statut_final(self):
        conn = store_pg.connect(self.url)
        try:
            aid = store_pg.analysis_queue(conn, ["60"], False)
            store_pg.analysis_attach_run(conn, aid, "888")
            self.assertFalse(store_pg.analysis_end_by_run(conn, "999", "echec"))      # autre run
            self.assertFalse(store_pg.analysis_end_by_run(conn, "", "echec"))         # pas de run
            self.assertTrue(store_pg.analysis_end_by_run(conn, "888", "terminee"))
            self.assertFalse(store_pg.analysis_end_by_run(conn, "888", "echec"))      # déjà finale
        finally:
            conn.close()
        self.assertEqual(self.analyses()[0]["statut"], "terminee")


class WorkflowLancementDirectTests(unittest.TestCase):
    def test_analysis_id_facultatif_avec_defaut_zero(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML absent")
        flux = yaml.safe_load((sp.RACINE / ".github" / "workflows" / "analyse.yml").read_text(encoding="utf-8"))
        entrees = (flux.get("on") or flux.get(True))["workflow_dispatch"]["inputs"]
        self.assertFalse(entrees["analysis_id"].get("required", False))
        self.assertEqual(str(entrees["analysis_id"]["default"]), "0")


if __name__ == "__main__":
    unittest.main()
