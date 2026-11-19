"""Lot 4 (historique, comparaison) et lot 5 (compte rendu HTML)."""
import contextlib
import io
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import COG, T, dila, patch_network, web  # noqa: E402
import crawl_links as cl  # noqa: E402
import csvio  # noqa: E402
import history  # noqa: E402
import inventory as inv  # noqa: E402
import mairie_link_monitor as mlm  # noqa: E402
import report  # noqa: E402
from layout import DeptPaths  # noqa: E402

COMMUNES = [("60001", "A"), ("60002", "B"), ("60003", "C"), ("60004", "D <script>"), ("60005", "E")]


def state(p: DeptPaths, links: dict, sites: dict):
    """links : code -> page ; sites : code -> statut_crawl."""
    csvio.write_rows(p.inventory_communes, [{"code_insee": c, "nom_commune": n, "nb_urls_valides": "1", "statut_commune": "x"}
                                            for c, n in COMMUNES])
    csvio.write_rows(p.crawl_sites, [dict(code_insee=c, site_url=f"https://{c}.fr/", statut_crawl=s) for c, s in sites.items()],
                     cl.SITE_FIELDS)
    csvio.write_rows(p.links_found, [dict(code_insee=c, site_url=f"https://{c}.fr/", page_url=u, target_url=T, type_lien="lien")
                                     for c, u in links.items()], cl.LINK_FIELDS)


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.p = DeptPaths(self.root, "60")
        self.p.reports_dir.mkdir(parents=True)
        self.db = history.db_path(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_premier_controle(self):
        state(self.p, {"60001": "https://60001.fr/"}, {"60001": "lien_trouve"})
        history.record(self.p, self.db, "2026-10-01T10:00:00+00:00")
        ch = history.changes(self.db, "60")
        self.assertIsNone(ch["precedent"])
        self.assertEqual((ch["courant"]["communes"], ch["courant"]["avec_lien"]), (5, 1))
        self.assertIn("premier contrôle", history.summary_line(ch))

    def test_nouveaux_disparus_confirmes_a_verifier_deplaces(self):
        state(self.p, {"60001": "https://60001.fr/a", "60002": "https://60002.fr/", "60003": "https://60003.fr/"},
              {"60001": "lien_trouve", "60002": "lien_trouve", "60003": "lien_trouve", "60004": "explore_complet",
               "60005": "explore_complet"})
        history.record(self.p, self.db, "2026-10-01T10:00:00+00:00")
        # 60001 : lien sur une autre page ; 60002 : site lu en entier sans le lien ; 60003 : site illisible ;
        # 60004 : nouveau lien ; 60005 : toujours sans lien
        state(self.p, {"60001": "https://60001.fr/b", "60004": "https://60004.fr/"},
              {"60001": "lien_trouve", "60002": "explore_complet", "60003": "erreur_acces", "60004": "lien_trouve",
               "60005": "explore_complet"})
        history.record(self.p, self.db, "2026-10-29T10:00:00+00:00")
        ch = history.changes(self.db, "60")
        codes = {k: [r["code_insee"] for r in ch[k]] for k in ("nouveaux", "disparus_confirmes", "disparus_a_verifier", "deplaces")}
        self.assertEqual(codes, {"nouveaux": ["60004"], "disparus_confirmes": ["60002"], "disparus_a_verifier": ["60003"],
                                 "deplaces": ["60001"]})
        self.assertEqual(ch["disparus_a_verifier"][0]["etat_actuel"], "site non lu : erreur_acces")
        self.assertEqual(ch["deplaces"][0]["page_precedente"], "https://60001.fr/a")
        self.assertEqual(history.summary_line(ch), "depuis le 01/10/2026 : 1 nouveau(x) lien(s), 1 disparu(s) confirmé(s), "
                                                   "1 disparu(s) à vérifier, 1 déplacé(s)")
        with sqlite3.connect(self.db) as conn:                         # une ligne par commune et par contrôle
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM etats").fetchone()[0], 10)

    def test_disparition_sur_panneaupocket_confirmee_si_page_lue(self):
        state(self.p, {}, {})
        csvio.write_rows(self.p.platform_links, [dict(code_insee="60005", site_url="https://app.panneaupocket.com/ville/1-e-60000",
                                                      page_url="https://app.panneaupocket.com/ville/1-e-60000", target_url=T,
                                                      type_lien="lien", confiance_site="plateforme_panneaupocket")], cl.LINK_FIELDS)
        csvio.write_rows(self.p.platform_pages, [dict(code_insee="60005", statut="lue", liens_trouves="1")])
        history.record(self.p, self.db)
        csvio.write_rows(self.p.platform_links, [], cl.LINK_FIELDS)
        csvio.write_rows(self.p.platform_pages, [dict(code_insee="60005", statut="lue", liens_trouves="0")])
        history.record(self.p, self.db)
        self.assertEqual([r["code_insee"] for r in history.changes(self.db, "60")["disparus_confirmes"]], ["60005"])

    def test_compte_rendu(self):
        state(self.p, {"60001": "https://60001.fr/a"}, {"60001": "lien_trouve", "60003": "erreur_acces"})
        history.record(self.p, self.db)
        state(self.p, {"60004": "https://app.panneaupocket.com/ville/9-d-60000"}, {"60001": "explore_complet", "60003": "erreur_acces"})
        history.record(self.p, self.db)
        html = report.render(self.p, self.db)
        self.assertIn("Compte rendu – département 60", html)
        self.assertIn("Nouveaux liens : 1", html)
        self.assertIn("Liens disparus (confirmé : site lu sans le lien) : 1", html)
        self.assertIn("PanneauPocket", html)
        self.assertIn("D &lt;script&gt;", html)                        # noms échappés
        self.assertNotIn("D <script>", html)
        self.assertIn("site non lu : erreur_acces", html)              # section « À vérifier »
        out = report.write(self.p, self.db)
        self.assertEqual(out.name, "rapport_60.html")
        self.assertTrue(out.read_text(encoding="utf-8").startswith("<!doctype html>"))

    def test_compte_rendu_sans_aucune_donnee(self):
        self.assertIn("pas encore de contrôle", report.render(DeptPaths(self.root, "80"), self.db))


class PipelineTests(unittest.TestCase):
    def test_chaque_analyse_enregistre_un_controle_et_un_compte_rendu(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data").mkdir()
            (root / "data" / "v_commune_2026.csv").write_text(COG, encoding="utf-8")
            restore = patch_network(web)
            try:
                with mock.patch.object(inv, "fetch_dila_records", side_effect=dila), contextlib.redirect_stdout(io.StringIO()) as out:
                    for _ in range(2):
                        self.assertEqual(mlm.main(["--root", tmp, "run", "--dept", "60", "--use-cached-cog", "--no-discover",
                                                   "--host-delay", "0", "--processes", "1"]), 0)
                    self.assertEqual(mlm.main(["--root", tmp, "historique", "--dept", "60"]), 0)
                    self.assertEqual(mlm.main(["--root", tmp, "rapport", "--dept", "60"]), 0)
            finally:
                restore()
            self.assertEqual(len(history.controls(history.db_path(root), "60")), 2)
            self.assertTrue((root / "resultats" / "rapport_60.html").exists())
            self.assertIn("0 nouveau(x) lien(s)", out.getvalue())


if __name__ == "__main__":
    unittest.main()
