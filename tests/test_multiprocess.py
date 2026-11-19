"""Exploration multi-processus : vrai serveur HTTP local (127.0.0.x), processus lancés en mode « spawn » (comme sous Windows)."""
import os
import sys
import tempfile
import threading
import unittest
from argparse import Namespace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import T  # noqa: E402
import crawl_links as c  # noqa: E402
import csvio  # noqa: E402
import helpers  # noqa: E402
from layout import DeptPaths  # noqa: E402

N = 40


class Handler(BaseHTTPRequestHandler):
    """Hôtes 127.0.0.N : le lien existe pour N pair, pas pour N impair. Compte les requêtes par hôte."""
    hits: dict = {}

    def do_GET(self):  # noqa: N802
        host = (self.headers.get("Host") or "").split(":")[0]
        Handler.hits[host] = Handler.hits.get(host, 0) + 1
        if self.path != "/":                           # robots.txt et pages usuelles du plan B : absents
            self.send_response(404)
            self.end_headers()
            return
        body = (f'<a href="{T}/">Vos questions</a>' if int(host.split(".")[-1]) % 2 == 0 else "<p>rien</p>").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class MultiProcessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def sites(self, dept="60"):
        return [{"code_insee": f"{dept}{i:03d}", "nom_commune": f"S{i}", "start_url": f"http://127.0.0.{i}:{self.port}/",
                 "confiance_lot2": "probable_officiel", "ssl_invalide": False, "_dept": dept, "known_pages": []}
                for i in range(1, N + 1)]

    OPTS = dict(max_pages=5, max_depth=1, concurrency=10, host_delay=0, timeout=5, site_timeout=20, contact="",
                use_sitemap=False, retry_failed=False, insecure_ssl=True, max_par_domaine=4)

    def test_plusieurs_processus_toutes_les_reponses_recues(self):
        got = []
        c.crawl_sites(self.sites(), self.OPTS, got.append, workers=2)
        self.assertEqual(len(got), N)
        by = {r.row["code_insee"]: r for r in got}
        for i in range(1, N + 1):
            expected = "lien_trouve" if i % 2 == 0 else "explore_partiel"     # accueil sans lien : plan B, rien de plus
            self.assertEqual(by[f"60{i:03d}"].row["statut_crawl"], expected, i)
        pids = {r.row["processus"] for r in got}
        self.assertEqual(len(pids), 2)                                   # deux processus distincts d'exploration
        self.assertNotIn(os.getpid(), pids)                              # ... différents du processus principal

    def test_mesures_remontees_par_chaque_processus(self):
        rows = []
        got = []
        c.crawl_sites(self.sites(), {**self.OPTS, "metrics_interval": 0.2}, got.append, workers=2, on_metrics=rows.append)
        self.assertEqual(len(got), N)
        self.assertEqual(len({r["processus"] for r in rows}), 2)         # une série de mesures par processus d'exploration
        self.assertNotIn(os.getpid(), {r["processus"] for r in rows})
        self.assertGreaterEqual(sum(r["requetes"] for r in rows), N * 2)  # au moins robots.txt + accueil par site
        self.assertTrue(all(r["taches_configurees"] == 5 for r in rows))  # 10 tâches réparties sur 2 processus

    def test_parallelisme_plafonne_par_processus(self):
        seen = []

        async def fake(sites, opts, on_result=None, on_done=None, **kw):
            seen.append(opts["concurrency"])
            return []
        with mock.patch.object(c, "crawl_all", fake):
            c.crawl_sites(self.sites(), {**self.OPTS, "concurrency": 300}, lambda r: None, workers=1)     # 1 processus : plafonné
            c.crawl_sites(self.sites(), {**self.OPTS, "concurrency": 150, "max_per_process": 40}, lambda r: None, workers=1)
        self.assertEqual(seen, [100, 40])

    def test_un_seul_processus_reste_dans_le_processus_principal(self):
        got = []
        c.crawl_sites(self.sites()[:5], self.OPTS, got.append, workers=4)   # trop peu de sites : pas de processus enfants
        self.assertEqual({r.row["processus"] for r in got}, {os.getpid()})

    def test_run_many_ecrit_le_departement_via_plusieurs_processus(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = DeptPaths(Path(tmp), "60")
            p.reports_dir.mkdir(parents=True)
            csvio.write_rows(p.inventory_urls, [dict(code_insee=s["code_insee"], nom_commune=s["nom_commune"],
                                                     url_candidate=s["start_url"], statut_url="url_non_verifiee")
                                                for s in self.sites()])
            done = []
            args = Namespace(max_pages=5, max_depth=1, concurrency=10, processes=2, host_delay=0, timeout=5, site_timeout=20,
                             include=list(c.DEFAULT_INCLUDE), no_insecure_ssl=False, limit_sites=0, commune=None, contact="",
                             resume=False, use_sitemap=False, recheck_known=True, retry_failed=False, max_par_domaine=4)
            with mock.patch("builtins.print"):
                rc = c.run_many([p], args, on_dept_done=lambda paths: done.append(len(helpers.dict_rows(paths.crawl_sites))))
            self.assertEqual((rc, done), (0, [N]))                       # écrit une seule fois, complet, à la fin du département
            self.assertEqual(len({r["code_insee"] for r in helpers.dict_rows(p.links_found)}), N // 2)


if __name__ == "__main__":
    unittest.main()
