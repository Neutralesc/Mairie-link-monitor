"""Tests : CSV (séparateur), config.toml (défauts, migration), choix des départements au lancement."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import COG, dila, patch_network, web  # noqa: E402
import config as cfg  # noqa: E402
import csvio  # noqa: E402
import helpers  # noqa: E402
import inventory as inv  # noqa: E402
import mairie_link_monitor as mlm  # noqa: E402
import results  # noqa: E402


class CsvIoTests(unittest.TestCase):
    def test_ecriture_point_virgule_par_defaut_et_relecture(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "x.csv"
            rows = [{"a": "https://x.fr/,https://y.fr/", "b": "Vos questions, écologie"}]
            csvio.write_rows(p, rows)
            self.assertEqual(p.read_text(encoding="utf-8-sig").splitlines()[0], "a;b")   # colonnes séparées (Excel FR)
            self.assertEqual(csvio.read_rows(p), rows)                                      # virgules préservées

    def test_lecture_des_anciens_fichiers_a_virgule(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "old.csv"
            p.write_text("code_insee,page_url\n60701,https://w.fr/a\n", encoding="utf-8-sig")
            self.assertEqual(csvio.read_rows(p), [{"code_insee": "60701", "page_url": "https://w.fr/a"}])

    def test_separateur_invalide(self):
        with self.assertRaises(ValueError):
            csvio.set_delimiter("|")


class ConfigTests(unittest.TestCase):
    def test_valeurs_par_defaut_respectent_les_decisions(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "config.toml"
            p.write_text(cfg.template(), encoding="utf-8")
            v = cfg.load(p)
            self.assertEqual(v, cfg.defaults())                      # fichier généré == défauts internes
            self.assertFalse(v["no_insecure_ssl"])                   # sites à certificat invalide explorés
            self.assertTrue(v["no_sitemap"])                         # pas de sitemap par défaut
            self.assertFalse(v["no_discover"])
            self.assertFalse(v["no_retry"])
            self.assertEqual((v["delimiter"], v["export_mode"]), (";", "une_page_par_commune"))
            self.assertEqual(v["steps"], ["inventory", "discover", "crawl", "export"])   # plateformes : hors défaut
            self.assertNotIn("plateformes", v["steps"])
            self.assertEqual((v["max_pages"], v["max_depth"], v["site_timeout"]), (500, 5, 900.0))
            self.assertEqual((v["host_delay"], v["adaptive_delay"]), (0.5, True))
            self.assertGreaterEqual(v["concurrency"], 200)           # parallélisme élevé par défaut
            self.assertEqual(v["processes"], 0)                      # automatique

    def test_erreurs_claires(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "config.toml"
            for bad in ('inconnue = 1\n', 'parallelisme = "beaucoup"\n', 'export_mode = "n_importe_quoi"\n',
                        'parallelisme = -3\n', 'etapes = ["inventory", "locate"]\n'):
                p.write_text(bad, encoding="utf-8")
                with self.assertRaises(cfg.ConfigError, msg=bad):
                    cfg.load(p)

    def test_update_value_conserve_le_reste(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "config.toml"
            p.write_text(cfg.template(), encoding="utf-8")
            cfg.update_value(p, "contact", "moi@exemple.fr")
            cfg.update_value(p, "parallelisme", 10)
            v = cfg.load(p)
            self.assertEqual((v["contact"], v["concurrency"], v["max_pages"]), ("moi@exemple.fr", 10, 500))

    def test_migration_d_un_ancien_fichier(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "config.toml"
            p.write_text('contact = "moi@x.fr"\ndemander_contact = false\nparallelisme = 24\nprofil = "complet"\n'
                         'releve_complet = true\nseparateur_csv = ","\n', encoding="utf-8")
            self.assertIn("mis à jour", cfg.migrate(p))
            v = cfg.load(p)
            self.assertEqual((v["contact"], v["ask_contact"], v["delimiter"]), ("moi@x.fr", False, ","))
            self.assertGreaterEqual(v["concurrency"], 200)           # ancien défaut (24) non conservé
            self.assertTrue((Path(tmp) / "config.toml.bak").exists())
            self.assertIsNone(cfg.migrate(p))                        # idempotent

    def test_migration_v3_conserve_vos_reglages_et_ajoute_plateformes(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "config.toml"
            p.write_text(cfg.template().replace("version_config = 4", "version_config = 3")
                         .replace('etapes = ["inventory", "discover", "crawl", "plateformes", "export"]',
                                  'etapes = ["inventory", "discover", "crawl", "export"]')
                         .replace("parallelisme = 300", "parallelisme = 600"), encoding="utf-8")
            self.assertIn("mis à jour", cfg.migrate(p))
            v = cfg.load(p)
            self.assertEqual(v["steps"], ["inventory", "discover", "crawl", "plateformes", "export"])
            self.assertEqual((v["concurrency"], v["config_version"]), (600, 4))         # réglage personnel conservé
            self.assertIsNone(cfg.migrate(p))

    def test_ligne_de_commande_prioritaire_et_config_creee(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with contextlib.redirect_stdout(io.StringIO()):
                args = mlm.parse_args(["--root", str(root), "run", "--dept", "60"])
            self.assertTrue((root / "config.toml").exists())         # créé au premier lancement
            self.assertEqual((args.concurrency, args.no_sitemap, args.processes), (300, True, 0))
            cfg.update_value(root / "config.toml", "parallelisme", 50)
            self.assertEqual(mlm.parse_args(["--root", tmp, "run", "--dept", "60"]).concurrency, 50)
            self.assertEqual(mlm.parse_args(["--root", tmp, "run", "--dept", "60", "--concurrency", "7"]).concurrency, 7)
            self.assertFalse(mlm.parse_args(["--root", tmp, "run", "--dept", "60", "--use-sitemap"]).no_sitemap)

    def test_aide_ne_cree_pas_de_fichier(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(SystemExit), contextlib.redirect_stdout(io.StringIO()):
            mlm.parse_args(["--root", tmp, "run", "--help"])
        self.assertFalse((Path(tmp) / "config.toml").exists())

    def test_processus_automatique(self):
        with mock.patch("os.cpu_count", return_value=1):
            self.assertEqual(mlm.auto_processes(), 1)
        with mock.patch("os.cpu_count", return_value=8):
            self.assertEqual(mlm.auto_processes(), 6)
        with mock.patch("os.cpu_count", return_value=4):
            self.assertEqual(mlm.auto_processes(), 3)


class InteractiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "data").mkdir()
        (self.root / "data" / "v_commune_2026.csv").write_text(COG, encoding="utf-8")
        self.config = self.root / "config.toml"
        self.config.write_text(cfg.template(), encoding="utf-8")
        for k, v in (("delai_par_site_s", 0.0), ("timeout_s", 5.0), ("processus", 1), ("decouvrir_sites_manquants", False)):
            cfg.update_value(self.config, k, v)
        self.restore = patch_network(web)
        self.dila = mock.patch.object(inv, "fetch_dila_records", side_effect=dila)
        self.dila.start()

    def tearDown(self):
        self.dila.stop()
        self.restore()
        self.tmp.cleanup()

    def launch(self, answers, argv=None):
        asked, it = [], iter(answers)

        def fake_input(prompt):
            asked.append(prompt)
            return next(it)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = mlm.main(argv if argv is not None else ["--root", str(self.root)], input_fn=fake_input)
        return rc, asked, out.getvalue()

    def result(self, dept="60"):
        return helpers.list_rows(self.root / "resultats" / f"liens_{dept}.csv")

    def test_lancement_sans_argument_demande_departement_et_contact(self):
        rc, asked, out = self.launch(["60", "moi@exemple.fr", ""])            # département, contact, confirmation
        self.assertEqual(rc, 0)
        self.assertTrue(asked[0].startswith("Département"))
        self.assertEqual(self.result(), [["code_insee", "url_page"], ["60001", "https://www.communea.fr/risques"]])
        self.assertEqual(cfg.load(self.config)["contact"], "moi@exemple.fr")  # mémorisé
        self.assertEqual(json.loads((self.root / "data" / "dernier_lancement.json").read_text())["departements"], ["60"])
        self.assertIn("sites en parallèle", out)                              # récapitulatif de performance

    def test_departement_invalide_puis_valide(self):
        cfg.update_value(self.config, "contact", "a@b.fr")
        rc, asked, out = self.launch(["999", "60", ""])
        self.assertEqual(rc, 0)
        self.assertIn("inconnu", out)
        self.assertEqual(len([a for a in asked if a.startswith("Département")]), 2)

    def test_tous_demande_confirmation(self):
        cfg.update_value(self.config, "contact", "a@b.fr")
        rc, asked, _ = self.launch(["tous", "n", "60", ""])
        self.assertEqual(rc, 0)
        self.assertTrue(any("Confirmer" in a for a in asked))
        self.assertFalse((self.root / "reports" / "01").exists())

    def test_annulation_ne_lance_rien(self):
        cfg.update_value(self.config, "contact", "a@b.fr")
        rc, _, out = self.launch(["60", "n"])
        self.assertEqual(rc, 0)
        self.assertIn("Annulé", out)
        self.assertFalse((self.root / "reports").exists())

    def test_reprise_proposee_quand_des_resultats_existent(self):
        cfg.update_value(self.config, "contact", "a@b.fr")
        self.assertEqual(self.launch(["60", ""])[0], 0)
        rc, asked, out = self.launch(["", "1", ""])                           # Entrée = dernier département ; reprise
        self.assertEqual(rc, 0)
        self.assertTrue(any(a.startswith("Choix") for a in asked))
        self.assertIn("(reprise)", out)
        self.assertNotIn("(reprise)", self.launch(["60", "2", ""])[2])        # tout refaire

    def test_departement_en_ligne_de_commande_ne_pose_aucune_question(self):
        def boom(prompt):
            raise AssertionError("aucune question attendue")
        with contextlib.redirect_stdout(io.StringIO()):
            rc = mlm.main(["--root", str(self.root), "run", "--dept", "60"], input_fn=boom)
        self.assertEqual(rc, 0)

    def test_sans_departement_et_sans_terminal(self):
        with mock.patch.object(sys.stdin, "isatty", return_value=False):
            self.assertEqual(mlm.main(["--root", str(self.root), "run"]), 2)

    def test_ancien_config_migre_au_lancement(self):
        self.config.write_text('contact = "ancien@x.fr"\nparallelisme = 24\nprofil = "complet"\n', encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = mlm.main(["--root", str(self.root), "run", "--dept", "60", "--no-discover"], input_fn=lambda p: "")
        self.assertEqual(rc, 0)
        self.assertEqual(cfg.load(self.config)["contact"], "ancien@x.fr")
        self.assertIn("mis à jour", out.getvalue())


class MenuDuplicatesTests(unittest.TestCase):
    def test_pick_page(self):
        rows = [{"page_url": "https://m.fr/a/b/c", "site_url": "https://m.fr/", "cible_commune": "generique"},
                {"page_url": "https://m.fr/", "site_url": "https://m.fr/", "cible_commune": "generique"},
                {"page_url": "https://m.fr/x", "site_url": "https://m.fr/", "cible_commune": "generique"}]
        self.assertEqual(results.pick_page(rows)["page_url"], "https://m.fr/")                # accueil d'abord
        rows[0]["cible_commune"] = "oui"
        self.assertEqual(results.pick_page(rows)["page_url"], "https://m.fr/a/b/c")           # cible propre à la commune
        rows[0]["cible_commune"] = "autre_commune"
        self.assertEqual(results.pick_page(rows)["page_url"], "https://m.fr/")
        self.assertEqual(results.pick_page(rows[2:])["page_url"], "https://m.fr/x")


if __name__ == "__main__":
    unittest.main()
