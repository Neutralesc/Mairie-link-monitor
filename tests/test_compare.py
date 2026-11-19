"""Commande compare : explique les communes d'anciens résultats qui ne sont plus retrouvées."""
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402,F401
import csvio  # noqa: E402
import helpers  # noqa: E402
import mairie_link_monitor as mlm  # noqa: E402
from layout import DeptPaths  # noqa: E402


class CompareTests(unittest.TestCase):
    def test_comparaison_explique_les_communes_perdues(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = DeptPaths(root, "60")
            paths.reports_dir.mkdir(parents=True)
            old = root / "ancien" / "links_found_oise.csv"
            old.parent.mkdir()
            old.write_text(                                              # ancien fichier : séparateur « , »
                "code_insee,nom_commune,site_url,page_url\n"
                "60001,A,https://a.fr/,https://a.fr/\n60001,A,https://a.fr/,https://a.fr/menu\n"
                "60002,B,https://b.fr/,https://b.fr/x\n60003,C,https://c.fr/,https://c.fr/\n"
                "60004,D,https://d.fr/,https://d.fr/\n60005,E,https://e.fr/,https://e.fr/\n", encoding="utf-8-sig")
            csvio.write_rows(paths.links_found, [
                {"code_insee": "60001", "page_url": "https://a.fr/"}, {"code_insee": "60002", "page_url": "https://b.fr/autre"}])
            csvio.write_rows(paths.crawl_sites, [
                {"code_insee": "60003", "site_url": "https://c.fr/", "statut_crawl": "erreur_acces", "motif": "page d'accueil : timeout"},
                {"code_insee": "60004", "site_url": "https://d.fr/", "statut_crawl": "explore_complet", "motif": "x", "pages_visitees": "12"}])
            csvio.write_rows(paths.url_checks, [{"code_insee": "60005", "statut_acces": "http_4xx", "confiance": "inaccessible"}])
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = mlm.main(["--root", tmp, "compare", "--dept", "60", "--ancien", str(old)])
            self.assertEqual(rc, 0)
            rows = {r["code_insee"]: r for r in helpers.dict_rows(paths.reports_dir / "comparaison_ancienne_version.csv")}
            self.assertEqual(rows["60001"]["verdict"], "retrouvée (même page)")
            self.assertEqual(rows["60002"]["verdict"], "retrouvée (autre page)")
            self.assertIn("illisible", rows["60003"]["cause"])
            self.assertIn("lien n'y est plus", rows["60004"]["cause"])
            self.assertIn("http_4xx", rows["60005"]["cause"])
            text = out.getvalue()
            self.assertIn("Ancienne : 6 page(s) distincte(s) pour 5 commune(s)", text)
            self.assertIn("pour 2 commune(s)", text)

    def test_fichier_ancien_introuvable(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mlm.main(["--root", tmp, "compare", "--dept", "60", "--ancien", str(Path(tmp) / "nope.csv")]), 1)


if __name__ == "__main__":
    unittest.main()
