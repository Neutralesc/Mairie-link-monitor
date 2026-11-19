"""Mesures de performance et recommandation de réglage du parallélisme."""
import asyncio
import contextlib
import io
import json
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import T, page  # noqa: E402
import crawl_links as c  # noqa: E402
import csvio  # noqa: E402
import helpers  # noqa: E402
import mairie_link_monitor as mlm  # noqa: E402
import metrics as mt  # noqa: E402
import tuning  # noqa: E402
from layout import DeptPaths  # noqa: E402


class MetricsTests(unittest.TestCase):
    def test_issues_classees(self):
        self.assertEqual(mt.outcome("accessible", 200), "ok")
        self.assertEqual(mt.outcome("http_5xx", 503), "limite_429_503")
        self.assertEqual(mt.outcome("http_4xx", 429), "limite_429_503")
        self.assertEqual(mt.outcome("http_5xx", 500), "http_5xx")
        self.assertEqual(mt.outcome("timeout", None), "timeout")
        self.assertEqual(mt.outcome("trop_de_redirections", None), "autre")

    def test_snapshot_fenetre_et_remise_a_zero(self):
        m = mt.Metrics(1234, 50)
        for _ in range(8):
            m.record("accessible", 200, 0.1)
        m.record("timeout", None, 15.0)
        m.record("http_4xx", 404, 0.05)
        m.probe(30.0)
        m.sites_done = 3
        row = m.snapshot()
        self.assertEqual((row["requetes"], row["ok"], row["timeout"], row["http_4xx"], row["sites_termines"]), (10, 8, 1, 1, 3))
        self.assertEqual((row["taches_configurees"], row["processus"], row["retard_boucle_max_ms"]), (50, 1234, 30.0))
        self.assertGreaterEqual(row["latence_p95_ms"], 100)
        self.assertEqual(m.snapshot()["requetes"], 0)                    # fenêtre suivante vide


class CrawlMetricsTests(unittest.TestCase):
    def test_crawl_all_emet_des_mesures(self):
        rows = []

        def h(req):
            if req.url.path in ("/robots.txt", "/sitemap.xml"):
                return httpx.Response(404)
            return page('<a href="/p">p</a>' if req.url.path == "/" else "<p>x</p>")
        sites = [{"code_insee": f"6{i:04d}", "nom_commune": f"S{i}", "start_url": f"https://s{i}.fr/", "confiance_lot2": "x",
                  "ssl_invalide": False, "_dept": "60"} for i in range(6)]
        opts = dict(max_pages=5, max_depth=2, concurrency=3, host_delay=0, timeout=5, site_timeout=20, contact="",
                    use_sitemap=False, retry_failed=False, insecure_ssl=False, max_par_domaine=4, metrics_interval=0.05)
        res = asyncio.run(c.crawl_all(sites, opts, transport=httpx.MockTransport(h), on_metrics=rows.append))
        self.assertEqual(len(res), 6)
        self.assertGreaterEqual(len(rows), 1)
        self.assertGreaterEqual(sum(r["requetes"] for r in rows), 12)     # au moins robots.txt + accueil par site
        self.assertEqual(sum(r["sites_termines"] for r in rows[-1:]), 6)  # cumul à la dernière ligne
        self.assertEqual(sum(r["ok"] for r in rows) + sum(r["http_4xx"] for r in rows), sum(r["requetes"] for r in rows))
        self.assertIsNone(c.METRICS)                                     # collecteur retiré après usage

    def test_latence_hors_attente_de_politesse_octets_et_pool(self):
        body = ("<p>" + "x" * 5000 + "</p><a href='/p'>p</a>").encode()

        async def h(req):
            await asyncio.sleep(0.05)                                    # latence réseau simulée
            if req.url.path == "/robots.txt":
                return httpx.Response(404)
            return httpx.Response(200, headers={"content-type": "text/html"}, content=body if req.url.path == "/" else b"<p>x</p>")
        sites = [{"code_insee": f"6{i:04d}", "nom_commune": f"S{i}", "start_url": f"https://s{i}.fr/", "confiance_lot2": "x",
                  "ssl_invalide": False, "_dept": "60"} for i in range(4)]
        opts = dict(max_pages=3, max_depth=1, concurrency=2, host_delay=0.3, timeout=5, site_timeout=20, contact="",
                    use_sitemap=False, retry_failed=False, insecure_ssl=False, max_par_domaine=4, metrics_interval=0.1)
        rows = []
        asyncio.run(c.crawl_all(sites, opts, transport=httpx.MockTransport(h), on_metrics=rows.append))
        self.assertLess(max(r["latence_p50_ms"] for r in rows), 200)       # 50 ms de réseau, pas 300 ms de politesse
        self.assertEqual(sum(r["octets_recus"] for r in rows), 4 * len(body) + 4 * len(b"<p>x</p>"))   # / et /p par site
        self.assertEqual(max(r["sites_en_cours_max"] for r in rows), 2)    # 2 tâches du pool occupées
        self.assertGreaterEqual(max(r["attente_politesse_moy"] for r in rows), 0)
        self.assertEqual(rows[-1]["file_attente_min"], 0)

    def test_aucune_mesure_sans_callback(self):
        c.METRICS = None

        def h(req):
            return httpx.Response(404) if req.url.path == "/robots.txt" else page("<p>x</p>")
        asyncio.run(c.crawl_all([{"code_insee": "1", "nom_commune": "S", "start_url": "https://s.fr/", "confiance_lot2": "x",
                                  "ssl_invalide": False}], dict(max_pages=2, max_depth=1, concurrency=1, host_delay=0, timeout=5,
                                  site_timeout=10, contact="", use_sitemap=False, retry_failed=False, insecure_ssl=False,
                                  max_par_domaine=4), transport=httpx.MockTransport(h)))
        self.assertIsNone(c.METRICS)


def sample(**over):
    base = dict(horodatage="t", processus=1.0, t_s=10.0, taches_configurees=50.0, sites_en_cours_moy=45.0, sites_en_cours_max=50.0,
                file_attente_min=100.0, en_vol_moy=20.0, en_vol_max=40.0, attente_politesse_moy=25.0, requetes=500.0,
                req_par_s=50.0, octets_recus=5_000_000.0, debit_mo_s=0.5, ok=480.0, http_4xx=15.0, http_5xx=0.0,
                limite_429_503=0.0, timeout=2.0, erreur_connexion=1.0, erreur_dns=2.0, erreur_ssl=0.0, autre=0.0,
                latence_p50_ms=300.0, latence_p95_ms=1200.0, cpu_pct=40.0, retard_boucle_p95_ms=20.0,
                retard_boucle_max_ms=60.0, sites_termines=10.0)
    base.update(over)
    return base


class TuningTests(unittest.TestCase):
    def advice(self, rows=None, cpu_count=8, **over):
        st = tuning.stats(rows or [sample(**over)] * 5)
        return " ".join(tuning.recommend(st, cpu_count=cpu_count))

    def test_pool_sature_avec_marge_augmenter_le_parallelisme(self):
        text = self.advice()
        self.assertIn("augmenter `parallelisme` par paliers", text)
        self.assertIn("≈ 80", text)                                       # 6 → 1 processus × 50 × 1,5 arrondi
        self.assertIn("politesse", text)

    def test_plafond_par_processus_atteint_proposer_plus_de_processus(self):
        text = self.advice(taches_configurees=100.0, sites_en_cours_moy=95.0)
        self.assertIn("augmenter `processus`", text)

    def test_trop_d_echecs_passagers(self):
        self.assertIn("Baisser `parallelisme`", self.advice(timeout=60.0, erreur_connexion=30.0, ok=380.0))
        self.assertIn("Baisser `parallelisme`", self.advice(limite_429_503=20.0, ok=460.0))

    def test_processeur_sature(self):
        self.assertIn("augmenter `processus`", self.advice(cpu_pct=98.0, retard_boucle_p95_ms=400.0))
        rows = [sample(cpu_pct=98.0, retard_boucle_p95_ms=400.0, processus=float(p)) for p in range(1, 8)]
        self.assertIn("plus de requêtes ne sera pas plus rapide", self.advice(rows))

    def test_pool_peu_occupe_la_limite_est_la_politesse(self):
        text = self.advice(sites_en_cours_moy=15.0)
        self.assertIn("n'est occupé qu'à 30 %", text)
        self.assertIn("la politesse", text)
        self.assertNotIn("augmenter `parallelisme`", text)

    def test_les_404_et_dns_ne_comptent_pas_comme_des_echecs_de_charge(self):
        st = tuning.stats([sample(http_4xx=200.0, erreur_dns=100.0, ok=198.0, timeout=1.0, erreur_connexion=1.0)] * 3)
        self.assertLess(st["taux_echec_passager"], 0.01)

    def test_phase_pleine_et_traine_separees(self):
        busy = [sample(t_s=10.0 * i, requetes=2500.0, file_attente_min=50.0) for i in range(1, 7)]          # 60 s à 250 req/s
        tail = [sample(t_s=10.0 * i, requetes=20.0, file_attente_min=0.0, timeout=8.0, ok=10.0, sites_en_cours_moy=3.0,
                       latence_p95_ms=30000.0) for i in range(7, 24)]                                          # 170 s de traîne
        st = tuning.stats(busy + tail)
        self.assertEqual(st["phase_pleine_s"], 60.0)
        self.assertGreater(st["part_temps_traine"], 0.7)
        self.assertLess(st["part_requetes_traine"], 0.05)
        self.assertLess(st["taux_echec_passager"], 0.05)                  # les timeouts de la traîne ne faussent pas la charge
        text = self.advice(busy + tail)
        self.assertIn("TRAÎNE", text)
        self.assertIn("latence p95 30 s", text)
        self.assertIn("plusieurs départements", text)

    def test_ancien_format_sans_file_d_attente(self):
        old = [{k: v for k, v in sample(t_s=10.0 * i, requetes=2500.0 if i <= 6 else 20.0).items()
                if k not in ("file_attente_min", "sites_en_cours_moy", "sites_en_cours_max", "attente_politesse_moy",
                             "octets_recus", "debit_mo_s")} for i in range(1, 24)]
        st = tuning.stats(old)
        self.assertTrue(st["ancien_format"])
        self.assertEqual(st["phase_pleine_s"], 60.0)                      # détectée par le débit (>= 50 % du pic)
        self.assertIn("ancienne version", self.advice(old))

    def test_debit_reseau_affiche_quand_connu(self):
        self.assertIn("Mbit/s", self.advice(debit_mo_s=12.5))

    def test_exploration_tres_courte_sans_division_par_zero(self):
        st = tuning.stats([sample(t_s=0.0, requetes=3.0, file_attente_min=0.0)])
        self.assertGreater(st["duree_s"], 0)
        self.assertTrue(tuning.recommend(st, cpu_count=4))

    def test_erreurs_dns_sous_la_charge_signalees(self):
        self.assertIn("resolutions_dns_simultanees", self.advice(erreur_dns=40.0, ok=440.0))
        self.assertNotIn("resolutions_dns_simultanees", self.advice())

    def test_sans_mesures(self):
        self.assertIn("Pas assez", tuning.recommend({})[0])


class RunTests(unittest.TestCase):
    def test_run_many_ecrit_le_fichier_de_mesures_et_la_recommandation(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = DeptPaths(Path(tmp), "60")
            p.reports_dir.mkdir(parents=True)
            csvio.write_rows(p.inventory_urls, [dict(code_insee=f"6{i:04d}", nom_commune=f"S{i}", url_candidate=f"https://s{i}.fr/",
                                                     statut_url="url_non_verifiee") for i in range(5)])
            orig = httpx.AsyncClient
            handler = lambda r: page("<p>rien</p>") if r.url.path == "/" else httpx.Response(404)  # noqa: E731
            httpx.AsyncClient = lambda **kw: orig(**{**kw, "transport": httpx.MockTransport(handler)})
            args = Namespace(max_pages=3, max_depth=1, concurrency=4, processes=1, host_delay=0, timeout=5, site_timeout=20,
                             include=list(c.DEFAULT_INCLUDE), no_insecure_ssl=True, limit_sites=0, commune=None, contact="x@y.fr",
                             resume=False, use_sitemap=False, recheck_known=True, retry_failed=False, max_par_domaine=4,
                             metrics=True, metrics_interval=0.05)
            out = io.StringIO()
            try:
                with contextlib.redirect_stdout(out):
                    self.assertEqual(c.run_many([p], args), 0)
            finally:
                httpx.AsyncClient = orig
            perf = sorted((Path(tmp) / "data" / "performance").glob("perf-*.csv"))
            self.assertEqual(len(perf), 1)
            rows = helpers.dict_rows(perf[0])
            self.assertGreaterEqual(len(rows), 1)
            # par site : robots.txt + accueil + plan du site + 6 pages usuelles (accueil sans lien : plan B)
            self.assertEqual(sum(int(r["requetes"]) for r in rows), 5 * (1 + 1 + 1 + len(c.FALLBACK_PATHS)))
            info = json.loads(perf[0].with_suffix(".json").read_text(encoding="utf-8"))
            self.assertIn("cpu_count", info["machine"])
            self.assertIn("Recommandation", out.getvalue())
            # désactivable
            args.metrics = False
            shutil_count = len(list((Path(tmp) / "data" / "performance").glob("perf-*.csv")))
            httpx.AsyncClient = lambda **kw: orig(**{**kw, "transport": httpx.MockTransport(handler)})
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    c.run_many([p], args)
            finally:
                httpx.AsyncClient = orig
            self.assertEqual(len(list((Path(tmp) / "data" / "performance").glob("perf-*.csv"))), shutil_count)

    def test_commande_tune(self):
        with tempfile.TemporaryDirectory() as tmp:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(mlm.main(["--root", tmp, "tune"]), 1)    # aucune mesure
            perf = Path(tmp) / "data" / "performance" / "perf-20261026-100000.csv"
            for r in [sample()] * 3:
                csvio.append_row(perf, r, mt.FIELDS)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(mlm.main(["--root", tmp, "tune"]), 0)
            self.assertIn("Recommandation", out.getvalue())
            self.assertIn("requetes_par_s_phase_pleine", out.getvalue())


if __name__ == "__main__":
    unittest.main()
