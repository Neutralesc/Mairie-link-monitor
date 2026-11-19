"""Tests du programme unique : départements, export minimal, pipeline complet (réseau simulé)."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import COG, T, dila, patch_network, web  # noqa: E402  (ajoute aussi src/ au chemin)
import helpers  # noqa: E402
import inventory as inv  # noqa: E402
import mairie_link_monitor as mlm  # noqa: E402
import results  # noqa: E402
from layout import DeptPaths, normalize_dept  # noqa: E402


def write_links(paths, rows):
    import csvio
    paths.reports_dir.mkdir(parents=True, exist_ok=True)
    csvio.write_rows(paths.links_found, rows, ["code_insee", "page_url", "site_url", "target_url", "type_lien", "ssl_invalide"])


class DeptTests(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual([normalize_dept(x) for x in ("1", "01", " 60 ", "2a", "2B", "971")],
                         ["01", "01", "60", "2A", "2B", "971"])

    def test_select_dept_et_arrondissements(self):
        rows = inv.read_cog(COG)
        self.assertEqual(inv.cog_departments(rows), ["01", "2A", "60", "75"])
        dept_rows, communes, arm = inv.select_dept(rows, "75")
        self.assertEqual([c["COM"] for c in communes], ["75056"])
        self.assertEqual(arm, {"75101": "75056", "75102": "75056"})
        self.assertEqual([c["COM"] for c in inv.select_dept(rows, "60")[1]], ["60001", "60002"])  # COMD exclue
        self.assertEqual([c["COM"] for c in inv.select_dept(rows, "01")[1]], ["01001"])

    def test_mairies_d_arrondissement_rattachees_a_la_commune(self):
        def rec(nom, code, url):
            return {"nom": nom, "site_internet": json.dumps([{"valeur": url}]),
                    "pivot": json.dumps([{"type_service_local": "mairie", "code_insee_commune": [code]}])}
        communes = [{"COM": "75056", "LIBELLE": "Paris", "DEP": "75", "TYPECOM": "COM"}]
        recs = [rec("Mairie - Paris 1er", "75101", "https://p1.fr"), rec("Mairie - Paris 2e", "75102", "https://p2.fr")]
        rows, _, orphans = inv.build_inventory(communes, recs, "T", {"75101": "75056", "75102": "75056"})
        self.assertEqual(orphans, [])
        self.assertEqual(rows[0]["nb_enregistrements_mairie"], 2)
        self.assertEqual(rows[0]["statut_commune"], inv.AMBIGU)   # 2 mairies principales : signalé, pas masqué

    def test_mairie_annexe_non_principale(self):
        r = {"nom": "Mairie annexe - X", "pivot": json.dumps([{"type_service_local": "mairie", "code_insee_commune": ["1"]}])}
        self.assertEqual(inv.service_type(r), "mairie_annexe")


class ExportTests(unittest.TestCase):
    def test_export_minimal(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = DeptPaths(Path(tmp), "60")
            write_links(p, [
                {"code_insee": "60701", "page_url": "https://w.fr/a", "target_url": "t", "type_lien": "lien", "ssl_invalide": "0"},
                {"code_insee": "60701", "page_url": "https://w.fr/a", "target_url": "t2", "type_lien": "iframe", "ssl_invalide": "0"},
                {"code_insee": "60701", "page_url": "https://w.fr/b", "target_url": "t", "type_lien": "lien", "ssl_invalide": "0"},
                {"code_insee": "60001", "page_url": "https://a.fr/", "target_url": "t", "type_lien": "texte", "ssl_invalide": "0"},
                {"code_insee": "60002", "page_url": "https://b.fr/", "target_url": "t", "type_lien": "lien", "ssl_invalide": "1"}])
            self.assertEqual(results.export_dept(p, mode="toutes_les_pages"), 3)   # doublon fusionné, texte exclu
            self.assertEqual(helpers.list_rows(p.result_csv), [["code_insee", "url_page"], ["60002", "https://b.fr/"],
                                                               ["60701", "https://w.fr/a"], ["60701", "https://w.fr/b"]])
            self.assertEqual(results.export_dept(p), 2)                             # défaut : une page par commune
            self.assertEqual(helpers.list_rows(p.result_csv), [["code_insee", "url_page"], ["60002", "https://b.fr/"],
                                                               ["60701", "https://w.fr/a"]])
            self.assertEqual(results.export_dept(p, include_text=True, delimiter=","), 3)
            self.assertIn("code_insee,url_page", p.result_csv.read_text(encoding="utf-8-sig"))
            results.export_dept(p)
            self.assertIn("code_insee;url_page", p.result_csv.read_text(encoding="utf-8-sig"))   # défaut : « ; »

    def test_export_sans_exploration(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                results.export_dept(DeptPaths(Path(tmp), "80"))

    def test_cumul(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_links(DeptPaths(root, "60"), [{"code_insee": "60001", "page_url": "https://a.fr/", "type_lien": "lien"}])
            write_links(DeptPaths(root, "80"), [{"code_insee": "80001", "page_url": "https://b.fr/", "type_lien": "lien"}])
            out = results.export_all(root)
            self.assertEqual([r[0] for r in list(helpers.list_rows(out))[1:]], ["60001", "80001"])


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "data").mkdir()
        (self.root / "data" / "v_commune_2026.csv").write_text(COG, encoding="utf-8")
        self.restore = patch_network(web)
        self.dila = mock.patch.object(inv, "fetch_dila_records", side_effect=dila)
        self.dila.start()

    def tearDown(self):
        self.dila.stop()
        self.restore()
        self.tmp.cleanup()

    def run_main(self, *extra):
        return mlm.main(["--root", str(self.root), "run", "--dept", *extra, "--no-discover", "--host-delay", "0",
                         "--processes", "1"])

    def test_pipeline_complet_un_departement(self):
        with contextlib.redirect_stdout(io.StringIO()):
            rc = self.run_main("60", "--use-cached-cog")
        self.assertEqual(rc, 0)
        rows = helpers.list_rows(self.root / "resultats" / "liens_60.csv")
        self.assertEqual(rows, [["code_insee", "url_page"], ["60001", "https://www.communea.fr/risques"]])
        p = DeptPaths(self.root, "60")
        for f in (p.inventory_communes, p.crawl_sites, p.links_found, p.crawl_summary, p.coverage):
            self.assertTrue(f.exists(), f)
        self.assertFalse(p.url_checks.exists())                  # plus de passe de vérification redondante
        comm = {r["code_insee"]: r for r in helpers.dict_rows(p.inventory_communes)}
        self.assertEqual(comm["60002"]["statut_commune"], inv.SANS_URL)
        cov = {r["code_insee"]: r for r in helpers.dict_rows(p.coverage)}
        self.assertEqual((cov["60001"]["etat"], cov["60001"]["analysee"]), ("lien trouvé", "oui"))
        self.assertEqual((cov["60002"]["analysee"], cov["60002"]["etat"][:9]), ("non", "aucun sit"))

    def test_departement_inconnu_et_all_sans_confirmation(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.run_main("999", "--use-cached-cog"), 2)
            self.assertEqual(self.run_main("all", "--use-cached-cog"), 2)

    def test_zero_devant_le_code_departement(self):
        with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(inv, "fetch_dila_records", return_value=[]) as m:
            mlm.main(["--root", str(self.root), "run", "--dept", "1", "--use-cached-cog", "--steps", "inventory"])
            m.assert_called_once_with("01")

    def test_echec_d_inventaire_stoppe_le_departement(self):
        with mock.patch.object(inv, "fetch_dila_records", side_effect=RuntimeError("API en panne")), \
                contextlib.redirect_stdout(io.StringIO()):
            rc = self.run_main("60", "--use-cached-cog")
        self.assertEqual(rc, 1)
        self.assertFalse((self.root / "resultats").exists())

    def test_deux_departements_ecrits_un_par_un(self):
        order = []
        real = results.export_dept

        def spy(paths, *a, **k):
            order.append(paths.dept)
            return real(paths, *a, **k)
        with mock.patch.object(results, "export_dept", spy), contextlib.redirect_stdout(io.StringIO()):
            rc = self.run_main("60", "1", "--use-cached-cog")
        self.assertEqual((rc, order), (0, ["60", "01", "60", "01"]))   # fin de chaque département, puis après PanneauPocket
        self.assertTrue((self.root / "resultats" / "liens_tous_departements.csv").exists())

    def test_migrate_legacy_depuis_un_autre_dossier(self):
        old = self.root / "ancien-lot1"
        (old / "reports").mkdir(parents=True)
        (old / "data").mkdir()
        (old / "reports" / "links_found_oise.csv").write_text("code_insee,page_url,type_lien\n60701,https://w.fr/a,lien\n", encoding="utf-8-sig")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mlm.main(["--root", str(self.root), "migrate-legacy", "--dept", "60"]), 1)   # rien ici : signalé
            self.assertEqual(mlm.main(["--root", str(self.root), "migrate-legacy", "--dept", "60", "--source", str(old)]), 0)
            self.assertEqual(mlm.main(["--root", str(self.root), "export", "--dept", "60"]), 0)
        self.assertTrue(DeptPaths(self.root, "60").result_csv.exists())


if __name__ == "__main__":
    unittest.main()
