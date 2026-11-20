"""Délai adaptatif par hôte (Throttle) et plafond de requêtes simultanées par adresse IP.

Horloge et sommeil sont injectés : aucun test n'attend réellement. Le transport est simulé.
"""
import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402,F401  (chemins src/ et tests/)
import crawl_links as c  # noqa: E402


class Horloge:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class DelaiAdaptatifTests(unittest.TestCase):
    def test_trois_echecs_delai_entre_4x_le_plancher_et_10s(self):
        th = c.Throttle(0.5, horloge=Horloge())
        for _ in range(3):
            th.echec("a.fr")
        d = th.delai("a.fr")
        self.assertGreaterEqual(d, 4 * 0.5)
        self.assertLessEqual(d, 10.0)

    def test_succes_redescend_jusqu_au_plancher(self):
        th = c.Throttle(0.5, horloge=Horloge())
        for _ in range(3):
            th.echec("a.fr")
        apres_echecs = th.delai("a.fr")
        for _ in range(10):
            th.succes("a.fr", 0.2)
        self.assertLess(th.delai("a.fr"), apres_echecs)
        self.assertAlmostEqual(th.delai("a.fr"), 0.5, places=2)        # retour au plancher

    def test_plafond_delai_max(self):
        th = c.Throttle(0.5, horloge=Horloge(), delai_max=3.0)
        for _ in range(10):
            th.echec("a.fr")
        self.assertLessEqual(th.delai("a.fr"), 3.0)

    def test_hotes_independants(self):
        th = c.Throttle(0.5, horloge=Horloge())
        th.echec("lent.fr")
        self.assertEqual(th.delai("autre.fr"), 0.5)

    def test_mode_fixe_ignore_echecs_et_succes(self):
        th = c.Throttle(1.0, adaptatif=False, horloge=Horloge())
        th.echec("a.fr")
        th.succes("a.fr", 0.1)
        self.assertEqual(th.delai("a.fr"), 1.0)
        th.per_host["a.fr"] = 2.0                                       # Crawl-delay : toujours pris en compte
        self.assertEqual(th.delai("a.fr"), 2.0)

    def test_429_double_le_plancher_puis_restaure_apres_succes(self):
        th = c.Throttle(0.5, horloge=Horloge())
        th.slow_down("a.fr")
        self.assertEqual(th.per_host["a.fr"], 1.0)
        for _ in range(30):
            th.succes("a.fr", 0.1)
        self.assertAlmostEqual(th.per_host["a.fr"], 0.5, places=2)


class RetryAfterTests(unittest.TestCase):
    def test_retry_after_3s_impose_le_delai_puis_expire(self):
        h = Horloge()
        th = c.Throttle(0.5, horloge=h)
        th.slow_down("a.fr", retry_after=3.0)
        self.assertGreaterEqual(th.delai("a.fr"), 3.0)
        h.t += 3.5                                                      # fenêtre Retry-After écoulée
        self.assertLess(th.delai("a.fr"), 3.0)

    def test_retry_after_plafonne(self):
        th = c.Throttle(0.5, horloge=Horloge())
        th.slow_down("a.fr", retry_after=10_000.0)
        self.assertLessEqual(th.delai("a.fr"), c.RETRY_AFTER_MAX_S)

    def test_attente_injectee_entre_deux_requetes(self):
        """Le sommeil est remplacé : on vérifie la durée demandée, sans attendre."""
        h = Horloge()
        sommeils: list[float] = []

        async def faux_sommeil(duree):
            sommeils.append(round(duree, 3))
            h.t += duree

        async def run():
            th = c.Throttle(0.0, horloge=h)
            th.slow_down("a.fr", retry_after=3.0)
            await th.wait("a.fr")                                       # première requête : pas d'attente
            await th.wait("a.fr")                                       # seconde : Retry-After de 3 s
        with mock.patch.object(c.asyncio, "sleep", faux_sommeil):
            asyncio.run(run())
        self.assertEqual(sommeils, [3.0])


class PlafondIPTests(unittest.TestCase):
    def setUp(self):
        # crawl_all peut laisser la protection anti-SSRF active : on la désactive pour ces tests de transport simulé.
        patch = mock.patch.dict(c.SECURITE, {"actif": False, "resoudre": None})
        patch.start()
        self.addCleanup(patch.stop)
        c.IP_PAR_HOTE.clear()
        self.addCleanup(c.IP_PAR_HOTE.clear)

    def test_six_hotes_meme_ip_jamais_plus_de_quatre_simultanees(self):
        hotes = [f"site{i}.fr" for i in range(6)]
        for h in hotes:
            c.IP_PAR_HOTE[h] = "192.0.2.10"
        en_vol, pic = [0], [0]

        async def handler(req):
            en_vol[0] += 1
            pic[0] = max(pic[0], en_vol[0])
            await asyncio.sleep(0.05)
            en_vol[0] -= 1
            return httpx.Response(200, headers={"content-type": "text/html"}, text="<p>x</p>")

        async def run():
            th = c.Throttle(0.0, requetes_par_ip=4)
            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            try:
                return await asyncio.gather(*(c._fetch(client, f"https://{h}/", h, th, want_body=False) for h in hotes))
            finally:
                await client.aclose()
        res = asyncio.run(run())
        self.assertTrue(all(r.status == 200 for r in res))
        self.assertLessEqual(pic[0], 4)
        self.assertEqual(pic[0], 4)                                     # le plafond est atteint (pas de sérialisation)

    def test_hote_non_resolu_a_sa_propre_file(self):
        th = c.Throttle(0.0)
        self.assertIs(th.plafond_ip("inconnu.fr"), th.plafond_ip("inconnu.fr"))
        self.assertIsNot(th.plafond_ip("inconnu.fr"), th.plafond_ip("autre.fr"))

    def test_hotes_sur_meme_ip_partagent_la_file(self):
        c.IP_PAR_HOTE["a.fr"] = c.IP_PAR_HOTE["b.fr"] = "192.0.2.10"
        th = c.Throttle(0.0)
        self.assertIs(th.plafond_ip("a.fr"), th.plafond_ip("b.fr"))


if __name__ == "__main__":
    unittest.main()
