"""Tests du lot 2 (aucun accès réseau : transport httpx simulé)."""
import asyncio
import csv
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

import httpx

import helpers

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import verify_urls as v  # noqa: E402
from layout import DeptPaths  # noqa: E402


class PureFunctionTests(unittest.TestCase):
    def test_redirect_type(self):
        self.assertEqual(v.redirect_type("http://a.fr", "http://a.fr/"), "aucune")
        self.assertEqual(v.redirect_type("http://www.a.fr", "https://www.a.fr/"), "http_vers_https")
        self.assertEqual(v.redirect_type("https://a.fr", "https://www.a.fr/"), "changement_sous_domaine_ou_www")
        self.assertEqual(v.redirect_type("https://a.fr", "https://b.com/"), "changement_domaine")
        self.assertEqual(v.redirect_type("https://a.fr", "https://a.fr/accueil"), "meme_domaine_chemin_different")

    def test_classify_status(self):
        c = v.classify_status
        self.assertEqual(c(200, None), "accessible")
        self.assertEqual(c(404, None), "http_4xx")
        self.assertEqual(c(403, None), "bloque_ou_limite")
        self.assertEqual(c(503, None), "http_5xx")
        self.assertEqual(c(None, httpx.ReadTimeout("t")), "timeout")
        self.assertEqual(c(None, httpx.TooManyRedirects("x")), "trop_de_redirections")
        self.assertEqual(c(None, httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED]")), "erreur_ssl")
        self.assertEqual(c(None, httpx.ConnectError("[Errno -2] Name or service not known")), "erreur_dns")
        self.assertEqual(c(None, httpx.ConnectError("refused")), "erreur_connexion")

    def test_domain_matches_commune(self):
        m = v.domain_matches_commune
        self.assertTrue(m("www.wavignies.fr", "Wavignies"))
        self.assertTrue(m("mairie-acyenmultien.fr", "Acy-en-Multien"))
        self.assertTrue(m("www.avilly-saint-leonard.com", "Avilly-Saint-Léonard"))
        self.assertFalse(m("www.oise.fr", "Wavignies"))
        self.assertTrue(m("baboeuf60.fr", "Babœuf"))
        self.assertTrue(m("crevecoeur-le-grand.fr", "Crèvecœur-le-Grand"))
        self.assertTrue(m("mairie-eve.fr", "Ève"))
        self.assertTrue(m("www.mairie-mesnilenthelle.fr", "Le Mesnil-en-Thelle"))
        self.assertFalse(m("steven.fr", "Ève"))  # nom court : mot entier exigé

    def test_confiance(self):
        a = v.assess_confidence
        self.assertEqual(a("Wavignies", "https://www.wavignies.fr/", "https://www.wavignies.fr/", "accessible", 1)[0],
                         "probable_officiel")
        self.assertEqual(a("Betz", "https://lapagelocale.fr/60620-betz", None, "accessible", 1)[0], "tiers_probable")
        self.assertEqual(a("Attichy", "https://www.gendarmerie.interieur.gouv.fr", None, "accessible", 1)[0],
                         "institution_non_communale")
        self.assertEqual(a("Beauvais", "https://www.beauvaisis.fr/vie-pratique/ccas/", None, "accessible", 1)[0],
                         "intercommunal_ou_service_probable")
        self.assertEqual(a("X", "https://www.cc-exemple.fr/", None, "accessible", 1)[0],
                         "intercommunal_ou_service_probable")
        self.assertEqual(a("Senantes", "https://communesenantes.e-monsite.com/", None, "accessible", 1)[0],
                         "plateforme_tierce_a_verifier")
        self.assertEqual(a("Wavignies", "https://www.wavignies.fr/", None, "timeout", 1)[0], "inaccessible")
        self.assertEqual(a("Zzz", "https://exemple.fr/", "https://exemple.fr/", "accessible", 1)[0], "a_verifier")
        # redirection de domaine : l'agglomération n'est pas la commune
        self.assertEqual(a("Beauvais", "https://www.beauvais.fr/", "https://www.beauvaisis.fr/", "accessible", 1,
                           "changement_domaine")[0], "a_verifier")
        self.assertEqual(a("Breuil-le-Vert", "https://www.mairie-breuil-le-vert.com/", "https://www.breuil-le-vert.fr/",
                           "accessible", 1, "changement_domaine")[0], "probable_officiel")
        self.assertEqual(a("Sainte-Eusoye", "http://www.sainte-eusoye.fr", "https://sainte-eusoye.neopse-site.com/fr/",
                           "accessible", 1, "changement_domaine")[0], "plateforme_tierce_a_verifier")
        self.assertEqual(a("Zzz", "https://exemple.fr/", None, "accessible", 3)[0], "intercommunal_ou_service_probable")


def handler(request: httpx.Request) -> httpx.Response:
    u = str(request.url)
    if u == "http://ok.fr/":
        return httpx.Response(301, headers={"location": "https://www.ok.fr/"})
    if u == "https://www.ok.fr/":
        return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"})
    if u == "https://gone.fr/":
        return httpx.Response(404)
    if u == "https://boom.fr/":
        return httpx.Response(503)
    if u == "https://slow.fr/":
        raise httpx.ReadTimeout("slow", request=request)
    return httpx.Response(200)


class NetworkSimulationTests(unittest.TestCase):
    URLS = ["http://ok.fr/", "https://gone.fr/", "https://boom.fr/", "https://slow.fr/"]

    def test_check_all(self):
        res = asyncio.run(v.check_all(self.URLS, 4, 0, 5, 5, "test", transport=httpx.MockTransport(handler)))
        self.assertEqual(res["http://ok.fr/"]["statut_acces"], "accessible")
        self.assertEqual(res["http://ok.fr/"]["final_url"], "https://www.ok.fr/")
        self.assertEqual(res["http://ok.fr/"]["nb_redirections"], 1)
        self.assertEqual(res["https://gone.fr/"]["statut_acces"], "http_4xx")
        self.assertEqual(res["https://boom.fr/"]["statut_acces"], "http_5xx")
        self.assertEqual(res["https://slow.fr/"]["statut_acces"], "timeout")
        self.assertIn("ReadTimeout", res["https://slow.fr/"]["erreur"])

    def test_politesse_delai_par_hote(self):
        times = []

        def h(request):
            times.append(__import__("time").monotonic())
            return httpx.Response(200)
        asyncio.run(v.check_all(["https://a.fr/1", "https://a.fr/2"], 4, 0.3, 5, 5, "t",
                                transport=httpx.MockTransport(h)))
        self.assertGreaterEqual(times[1] - times[0], 0.28)

    def test_run_complet_et_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = DeptPaths(root, "60")
            paths.reports_dir.mkdir(parents=True)
            with paths.inventory_urls.open("w", encoding="utf-8-sig", newline="") as f:
                w = csv.DictWriter(f, fieldnames=["code_insee", "nom_commune", "type_service", "url_candidate", "statut_url"])
                w.writeheader()
                for i, u in enumerate(self.URLS):
                    w.writerow({"code_insee": f"6000{i}", "nom_commune": "Ok" if i == 0 else f"C{i}",
                                "type_service": "mairie", "url_candidate": u, "statut_url": "url_non_verifiee"})
                w.writerow({"code_insee": "60009", "nom_commune": "Vide", "type_service": "mairie",
                            "url_candidate": "", "statut_url": ""})
            orig = v.httpx.AsyncClient
            v.httpx.AsyncClient = lambda **kw: orig(**{**kw, "transport": httpx.MockTransport(handler)})
            try:
                args = Namespace(limit=0, concurrency=4, host_delay=0, timeout=5, max_redirects=5,
                                 contact="", resume=False)
                self.assertEqual(v.run(paths, args), 0)
                rows = list(helpers.dict_rows(paths.url_checks))
                self.assertEqual(len(rows), 4)
                by = {r["url_candidate"]: r for r in rows}
                self.assertEqual(by["http://ok.fr/"]["type_redirection"], "http_vers_https+changement_sous_domaine_ou_www")
                self.assertEqual(by["https://gone.fr/"]["statut_acces"], "http_4xx")
                # resume : seules les URL en échec transitoire (boom 503, slow timeout) sont recontrôlées
                calls = []
                orig_handler = handler
                def counting(req):
                    calls.append(str(req.url)); return orig_handler(req)
                v.httpx.AsyncClient = lambda **kw: orig(**{**kw, "transport": httpx.MockTransport(counting)})
                args.resume = True
                self.assertEqual(v.run(paths, args), 0)
                self.assertEqual(sorted(calls), ["https://boom.fr/", "https://slow.fr/"])
            finally:
                v.httpx.AsyncClient = orig


if __name__ == "__main__":
    unittest.main()
