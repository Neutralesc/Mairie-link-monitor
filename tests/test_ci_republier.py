"""Commande ci_analyse republier : republie les résultats déjà sur le disque (cache restauré), sans analyse."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support_publications as sp  # noqa: E402
from test_ci_analyse import lancer_ci  # noqa: E402

import store_pg  # noqa: E402


class RepublierSansBaseTests(unittest.TestCase):
    def test_departement_invalide_code_2_sans_toucher_la_base(self):
        r = lancer_ci(["republier", "--depts", "60; rm -rf /"], DATABASE_URL="postgresql://inexistant.invalid/x")
        self.assertEqual(r.returncode, 2)
        self.assertIn("invalide", r.stderr)

    def test_sans_url_de_base_code_2(self):
        r = lancer_ci(["republier", "--depts", "60"])
        self.assertEqual(r.returncode, 2)

    def test_base_inaccessible_n_affiche_que_le_type_d_erreur(self):
        r = lancer_ci(["republier", "--depts", "60"], DATABASE_URL="postgresql://secretuser@hote-inconnu.invalid:1/db")
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("secretuser", r.stdout + r.stderr)
        self.assertNotIn("hote-inconnu", r.stdout + r.stderr)

    def test_workflow_option_republier_et_etapes_conditionnelles(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML absent")
        texte = (sp.RACINE / ".github" / "workflows" / "analyse.yml").read_text(encoding="utf-8")
        flux = yaml.safe_load(texte)
        entrees = (flux.get("on") or flux.get(True))["workflow_dispatch"]["inputs"]
        self.assertEqual(entrees["republier"]["type"], "boolean")
        self.assertFalse(entrees["republier"]["default"])
        etapes = {e["name"]: e for e in flux["jobs"]["analyse"]["steps"]}
        for nom in ("Sauvegarde de l'état", "Marquage de l'analyse annulée ou en échec"):
            self.assertIn("inputs.republier != true", etapes[nom]["if"], nom)
        for etape in flux["jobs"]["analyse"]["steps"]:               # aucune entrée interpolée dans un script shell
            self.assertNotIn("${{", etape.get("run", ""), etape["name"])


@unittest.skipIf(bool(sp.RAISON_PG), sp.RAISON_PG)
class RepublierPgTests(sp.PgSchemaTestCase):
    def setUp(self):
        self.vider_tables()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        sp.construire_racine(self.root)                    # 02, 60 et 80 + CSV global

    def republier(self, depts: str):
        return lancer_ci(["republier", "--depts", depts, "--root", str(self.root)], DATABASE_URL=self.url)

    def test_republie_les_departements_demandes_avec_la_liste_des_communes(self):
        r = self.republier("60")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("republié : 60", r.stdout)
        pg = store_pg.PostgresSource(self.url)
        self.assertEqual(pg.depts(), ["60"])
        lignes = pg.liens()
        self.assertEqual([(l["dept"], l["code_insee"]) for l in lignes], [("60", "60001")])
        self.assertTrue(lignes[0]["nom_commune"])

    def test_tous_republie_tout_ce_qui_est_sur_le_disque(self):
        r = self.republier("tous")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(store_pg.PostgresSource(self.url).depts(), ["02", "60", "80"])

    def test_met_a_niveau_une_publication_faite_sans_liste(self):
        import psycopg
        self.assertEqual(self.republier("60").returncode, 0)
        with psycopg.connect(self.url, autocommit=True) as conn:       # simule une publication d'avant la fonctionnalité
            conn.execute("UPDATE publications SET liens_json = '[]'")
        self.assertEqual(store_pg.PostgresSource(self.url).liens(), [])
        self.assertEqual(self.republier("60").returncode, 0)
        self.assertEqual(len(store_pg.PostgresSource(self.url).liens()), 1)

    def test_racine_sans_resultat_code_1_et_message(self):
        with tempfile.TemporaryDirectory() as vide:
            r = lancer_ci(["republier", "--depts", "60", "--root", vide], DATABASE_URL=self.url)
        self.assertEqual(r.returncode, 1)
        self.assertIn("aucun résultat à republier", r.stderr)
        self.assertEqual(store_pg.PostgresSource(self.url).depts(), [])


if __name__ == "__main__":
    unittest.main()
