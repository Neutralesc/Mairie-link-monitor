"""Résolution DNS préalable : concurrence limitée, nouvelles tentatives, motif conservé, seconde tentative du site."""
import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402,F401
import crawl_links as cl  # noqa: E402


class PrefetchTests(unittest.TestCase):
    def test_echec_passager_retente_puis_reussi(self):
        answers = iter(["OSError: [Errno 11002] getaddrinfo failed", ""])
        calls = []

        async def fake(host, timeout=5.0):
            calls.append(host)
            return next(answers)
        with mock.patch.object(cl, "resolve", fake):
            err = asyncio.run(cl.prefetch_dns("https://www.m.fr/x", asyncio.Semaphore(1), 0))
        self.assertEqual((err, calls), ("", ["www.m.fr", "www.m.fr"]))

    def test_echec_persistant_motif_conserve(self):
        async def fake(host, timeout=5.0):
            return "OSError: [Errno 11001] getaddrinfo failed"
        with mock.patch.object(cl, "resolve", fake):
            err = asyncio.run(cl.prefetch_dns("https://m.fr/", asyncio.Semaphore(1), 0))
        self.assertIn("11001", err)

    def test_adresse_ip_jamais_resolue(self):
        async def boom(host, timeout=5.0):
            raise AssertionError("pas de résolution pour une adresse IP")
        with mock.patch.object(cl, "resolve", boom):
            self.assertEqual(asyncio.run(cl.prefetch_dns("http://127.0.0.3:8080/", asyncio.Semaphore(1), 0)), "")

    def test_concurrence_limitee(self):
        state = {"active": 0, "max": 0}

        async def slow(host, timeout=5.0):
            state["active"] += 1
            state["max"] = max(state["max"], state["active"])
            await asyncio.sleep(0.02)
            state["active"] -= 1
            return ""

        async def many():
            sem = asyncio.Semaphore(3)
            await asyncio.gather(*(cl.prefetch_dns(f"https://s{i}.fr/", sem, 0) for i in range(12)))
        with mock.patch.object(cl, "resolve", slow):
            asyncio.run(many())
        self.assertEqual(state["max"], 3)

    def test_erreur_dns_du_site_retentee_avec_le_motif(self):
        async def fail(host, timeout=5.0):
            return "OSError: [Errno 11002] getaddrinfo failed"
        crawls = []

        async def fake_crawl(site, clients, throttle, opts, attempt=1):
            crawls.append(attempt)
            row = {"code_insee": "1", "site_url": site["start_url"], "statut_crawl": "erreur_acces",
                   "motif": "page d'accueil : erreur_dns", "erreur": "", "tentatives": attempt}
            return cl.SiteResult(row, [], "")
        site = {"code_insee": "1", "nom_commune": "M", "start_url": "https://m.fr/", "confiance_lot2": "x", "ssl_invalide": False}
        opts = dict(concurrency=1, host_delay=0, timeout=5, retry_pause=0, dns_retry_pause=0, dns_simultanees=2)
        with mock.patch.object(cl, "resolve", fail), mock.patch.object(cl, "crawl_site", fake_crawl):
            (res,) = asyncio.run(cl.crawl_all([site], opts))
        self.assertEqual(crawls, [1, 2])                                  # erreur DNS : seconde tentative
        self.assertIn("11002", res.row["erreur"])


if __name__ == "__main__":
    unittest.main()
