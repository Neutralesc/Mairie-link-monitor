"""Rapport de couverture : combien de communes sont réellement analysées, et pourquoi les autres ne le sont pas."""
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402,F401
import coverage  # noqa: E402
import csvio  # noqa: E402
import helpers  # noqa: E402
import mairie_link_monitor as mlm  # noqa: E402
from layout import DeptPaths  # noqa: E402


class CoverageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.paths = DeptPaths(Path(self.tmp.name), "60")
        self.paths.reports_dir.mkdir(parents=True)
        csvio.write_rows(self.paths.inventory_communes, [
            {"code_insee": c, "nom_commune": n, "statut_commune": "x", "nb_urls_valides": u} for c, n, u in (
                ("60001", "A", "1"), ("60002", "B", "1"), ("60003", "C", "1"), ("60004", "D", "1"),
                ("60005", "E", "1"), ("60006", "F", "0"), ("60007", "G", "1"))])
        csvio.write_rows(self.paths.crawl_sites, [
            {"code_insee": "60001", "statut_crawl": "lien_trouve", "motif": "arrêt", "url_utilisee": "https://a.fr/"},
            {"code_insee": "60002", "statut_crawl": "explore_complet"},
            {"code_insee": "60003", "statut_crawl": "explore_partiel"},
            {"code_insee": "60004", "statut_crawl": "erreur_acces", "motif": "page d'accueil : erreur_dns"},
            {"code_insee": "60005", "statut_crawl": "robots_interdit"}])

    def tearDown(self):
        self.tmp.cleanup()

    def test_etats_par_commune(self):
        rows = {r["code_insee"]: r for r in coverage.build(self.paths)}
        self.assertEqual(rows["60001"]["etat"], "lien trouvé")
        self.assertEqual(rows["60002"]["etat"], "site lu en entier, aucun lien")
        self.assertTrue(rows["60003"]["etat"].startswith("site lu partiellement"))
        self.assertEqual(rows["60004"]["etat"], "site non lu : erreur_acces")
        self.assertEqual(rows["60006"]["etat"], "aucun site référencé (DILA) et aucun site trouvé")
        self.assertTrue(rows["60007"]["etat"].startswith("non traitée"))
        self.assertEqual([c for c, r in rows.items() if r["analysee"] == "oui"], ["60001", "60002", "60003"])

    def test_entonnoir_affiche_et_fichier_ecrit(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            coverage.report(self.paths)
        text = out.getvalue()
        self.assertIn("7 communes (COG)", text)
        self.assertIn("3 analysées", text)
        self.assertIn("1 avec lien trouvé", text)
        self.assertTrue(self.paths.coverage.exists())
        self.assertEqual(len(helpers.dict_rows(self.paths.coverage)), 7)

    def test_commande_coverage(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = mlm.main(["--root", self.tmp.name, "coverage", "--dept", "60"])
        self.assertEqual(rc, 0)
        self.assertIn("Couverture de l'analyse", out.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mlm.main(["--root", self.tmp.name, "coverage", "--dept", "80"]), 1)   # inventaire absent


if __name__ == "__main__":
    unittest.main()
