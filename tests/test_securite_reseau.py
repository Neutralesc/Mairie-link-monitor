"""Protection anti-SSRF (securite_reseau) : classification des adresses, verifier_hote avec résolveur injecté, et crawl
avec transport simulé où une redirection vers un hôte interne ne produit aucune requête.

Aucun réseau réel : la résolution DNS est remplacée par une fonction de test.
"""
import asyncio
import logging
import sys
import unittest
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import T  # noqa: E402
import crawl_links as c  # noqa: E402
import securite_reseau as sr  # noqa: E402

logging.getLogger("securite_reseau").setLevel(logging.ERROR)     # refus journalisés en WARNING : bruit inutile ici

REFUSEES = ["127.0.0.1", "10.0.0.1", "192.168.1.5", "172.16.0.1", "169.254.169.254", "100.64.0.1", "0.0.0.0", "::1",
            "fe80::1", "fc00::1", "::ffff:127.0.0.1", "224.0.0.1", "64:ff9b::7f00:1"]
AUTORISEES = ["93.184.216.34", "8.8.8.8", "2606:4700:4700::1111"]


def resolveur(table: dict, appels: list | None = None):
    """Résolveur asynchrone de test : `table` associe un hôte à ses adresses ; hôte absent = erreur de résolution."""
    async def resoudre(hote: str) -> list[str]:
        if appels is not None:
            appels.append(hote)
        if hote not in table:
            raise OSError(f"résolution impossible pour {hote}")
        return table[hote]
    return resoudre


class AdressesTests(unittest.TestCase):
    def test_adresses_non_publiques_refusees(self):
        for ip in REFUSEES:
            with self.subTest(ip=ip):
                self.assertFalse(sr.ip_autorisee(ip))

    def test_adresses_publiques_autorisees(self):
        for ip in AUTORISEES:
            with self.subTest(ip=ip):
                self.assertTrue(sr.ip_autorisee(ip))

    def test_ipv4_embarquees_controlees_comme_ipv4(self):
        self.assertTrue(sr.ip_autorisee("64:ff9b::808:808"))        # NAT64 de 8.8.8.8 : IPv4 publique, autorisée
        self.assertFalse(sr.ip_autorisee("64:ff9b::a00:1"))         # NAT64 de 10.0.0.1 : privée, refusée
        self.assertFalse(sr.ip_autorisee("::ffff:10.1.2.3"))        # IPv4 mappée sur un réseau privé
        self.assertFalse(sr.ip_autorisee("2002:7f00:1::"))          # 6to4 vers 127.0.0.1

    def test_adresse_mal_formee_refusee(self):
        for texte in ("", "pas-une-ip", "999.1.1.1"):
            with self.subTest(texte=texte):
                self.assertFalse(sr.ip_autorisee(texte))


class VerifierHoteTests(unittest.TestCase):
    def setUp(self):
        sr.vider_cache()
        self.addCleanup(sr.vider_cache)

    def verifier(self, hote: str, table: dict | None = None, appels: list | None = None):
        return asyncio.run(sr.verifier_hote(hote, resoudre=resolveur(table or {}, appels)))

    def test_localhost_et_suffixes_locaux_refuses_sans_resolution(self):
        appels: list = []
        for hote in ("localhost", "LOCALHOST.", "app.localhost", "nas.local", "svc.internal"):
            with self.subTest(hote=hote):
                self.assertEqual(self.verifier(hote, appels=appels), sr.RAISON_NON_PUBLIQUE)
        self.assertEqual(appels, [])                                  # aucune résolution pour ces noms

    def test_ip_litterale(self):
        self.assertEqual(self.verifier("127.0.0.1"), sr.RAISON_NON_PUBLIQUE)
        self.assertEqual(self.verifier("[::1]"), sr.RAISON_NON_PUBLIQUE)
        self.assertIsNone(self.verifier("93.184.216.34"))

    def test_tld_numerique_refuse(self):
        self.assertEqual(self.verifier("www.169.254.169.254"), sr.RAISON_NON_PUBLIQUE)

    def test_hote_public(self):
        self.assertIsNone(self.verifier("mairie.example", {"mairie.example": ["93.184.216.34"]}))

    def test_hote_mixte_public_et_prive_refuse(self):
        table = {"mixte.example": ["93.184.216.34", "10.0.0.7"]}
        self.assertEqual(self.verifier("mixte.example", table), sr.RAISON_NON_PUBLIQUE)

    def test_echec_de_resolution_retourne_none(self):
        # Une erreur DNS n'est pas un refus : le flux normal de l'exploration la signalera.
        self.assertIsNone(self.verifier("inexistant.example", {}))

    def test_resolution_vide_retourne_none(self):
        self.assertIsNone(self.verifier("vide.example", {"vide.example": []}))

    def test_cache_une_seule_resolution(self):
        appels: list = []
        table = {"cache.example": ["93.184.216.34"]}
        self.assertIsNone(self.verifier("cache.example", table, appels))
        self.assertIsNone(self.verifier("cache.example", table, appels))
        self.assertEqual(appels, ["cache.example"])

    def test_cache_conserve_le_refus(self):
        appels: list = []
        table = {"interne.example": ["192.168.0.9"]}
        self.assertEqual(self.verifier("interne.example", table, appels), sr.RAISON_NON_PUBLIQUE)
        self.assertEqual(self.verifier("interne.example", table, appels), sr.RAISON_NON_PUBLIQUE)
        self.assertEqual(len(appels), 1)


class CrawlAntiSsrfTests(unittest.TestCase):
    """Transport simulé + verif_ip True + résolveur injecté. Le crawler essaie aussi les variantes www. : le résolveur
    raisonne donc par motif (toute variante d'un nom « interne » ou du portail « prive » est non publique)."""

    def setUp(self):
        sr.vider_cache()
        self.addCleanup(sr.vider_cache)
        self.requetes: list[str] = []

    def site(self) -> dict:
        return {"code_insee": "60001", "nom_commune": "A", "start_url": T + "/", "confiance_lot2": "probable_officiel",
                "ssl_invalide": False, "_dept": "60", "known_pages": []}

    def opts(self, motifs_prives: tuple[str, ...]) -> dict:
        async def resoudre(hote: str) -> list[str]:
            return ["10.0.0.5"] if any(m in hote for m in motifs_prives) else ["93.184.216.34"]
        return dict(max_pages=5, max_depth=1, concurrency=4, host_delay=0, timeout=5, site_timeout=20, contact="",
                    use_sitemap=False, retry_failed=False, insecure_ssl=False, max_par_domaine=4,
                    verif_ip=True, resoudre_hote=resoudre)

    def explorer(self, handler, motifs_prives: tuple[str, ...]):
        async def run():
            return await c.crawl_all([self.site()], self.opts(motifs_prives), transport=httpx.MockTransport(handler))
        return asyncio.run(run())

    def test_redirection_vers_hote_interne_zero_requete(self):
        def handler(req: httpx.Request) -> httpx.Response:
            self.requetes.append(str(req.url))
            if req.url.path == "/robots.txt":
                return httpx.Response(404)
            return httpx.Response(302, headers={"location": "https://interne.example/admin"})

        res = self.explorer(handler, motifs_prives=("interne",))
        self.assertEqual(len(res), 1)
        self.assertTrue(self.requetes)                                # le portail, lui, est bien interrogé
        self.assertFalse(any("interne" in u for u in self.requetes), self.requetes)   # aucune requête vers l'interne
        self.assertEqual(res[0].row["statut_crawl"], "erreur_acces")
        self.assertIn(sr.RAISON_NON_PUBLIQUE, res[0].row["motif"])

    def test_accueil_sur_hote_prive_zero_requete(self):
        def handler(req: httpx.Request) -> httpx.Response:
            self.requetes.append(str(req.url))
            return httpx.Response(200, text="<p>x</p>", headers={"content-type": "text/html"})

        res = self.explorer(handler, motifs_prives=("vosquestions",))
        self.assertEqual(self.requetes, [])
        self.assertEqual(res[0].row["statut_crawl"], "erreur_acces")
        self.assertIn(sr.RAISON_NON_PUBLIQUE, res[0].row["motif"])


if __name__ == "__main__":
    unittest.main()
