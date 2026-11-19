"""Tests de la découverte de sites (communes sans site référencé) : noms de domaine usuels + validation stricte."""
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import page  # noqa: E402
import csvio  # noqa: E402
import discover as d  # noqa: E402
import helpers  # noqa: E402
from layout import DeptPaths  # noqa: E402

GOOD = ("<html><head><title>Mairie de Longueil-Sainte-Marie</title></head><body><h1>Bienvenue</h1>"
        "<p>Mairie - 1 place de la Mairie 60126 Longueil-Sainte-Marie</p></body></html>")


class PureTests(unittest.TestCase):
    def test_variantes_de_nom(self):
        self.assertEqual(d.name_variants("L'Abergement-Clémenciat"),
                         ["l-abergement-clemenciat", "labergementclemenciat", "abergement-clemenciat", "abergementclemenciat"])
        self.assertIn("oeuilly", d.name_variants("Œuilly"))
        self.assertEqual(d.name_variants("!!!"), [])

    def test_domaines_candidats(self):
        hosts = d.candidate_hosts("Longueil-Sainte-Marie", "60")
        for h in ("longueil-sainte-marie.fr", "mairie-longueil-sainte-marie.fr", "mairielongueilsaintemarie.fr",
                  "longueil-sainte-marie60.fr", "commune-longueil-sainte-marie.fr", "longueilsaintemarie.com"):
            self.assertIn(h, hosts)
        self.assertEqual(len(hosts), len(set(hosts)))
        self.assertFalse(any(h.split(".")[0].endswith("2a") for h in d.candidate_hosts("Ajaccio", "2A")))  # pas de suffixe pour la Corse

    def test_code_postal_du_departement(self):
        self.assertTrue(d.postal_regex("60").search("60126 Longueil"))
        self.assertFalse(d.postal_regex("60").search("62100 Calais ; 160126"))
        self.assertTrue(d.postal_regex("2A").search("20000 Ajaccio"))
        self.assertTrue(d.postal_regex("971").search("97100 Basse-Terre"))

    def test_validation_stricte(self):
        ok, why = d.validate_home(GOOD, "Longueil-Sainte-Marie", "60")
        self.assertTrue(ok, why)
        self.assertFalse(d.validate_home(GOOD, "Autre-Commune", "60")[0])                       # nom absent
        self.assertFalse(d.validate_home(GOOD.replace("60126", "62100"), "Longueil-Sainte-Marie", "60")[0])   # autre département
        self.assertFalse(d.validate_home("<p>Longueil-Sainte-Marie 60126 camping</p>", "Longueil-Sainte-Marie", "60")[0])  # pas mairie
        self.assertFalse(d.validate_home("", "Longueil-Sainte-Marie", "60")[0])


def make_handler(pages=None, robots=None):
    pages = pages or {}

    def h(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(200, headers={"content-type": "text/plain"}, text=robots) if robots else httpx.Response(404)
        return page(pages[req.url.host]) if req.url.host in pages and req.url.path == "/" else httpx.Response(404)
    return h


async def resolver_for(*hosts):
    async def r(host):
        return host in hosts
    return r


class ProbeTests(unittest.TestCase):
    commune = {"code_insee": "60369", "nom_commune": "Longueil-Sainte-Marie"}

    def probe(self, handler, hosts):
        import asyncio

        async def res(host):
            return host in hosts
        return asyncio.run(d.discover_all([("60", self.commune)], 4, 5, transport=httpx.MockTransport(handler), resolver=res))[0][1]

    def test_site_valide_trouve(self):
        r = self.probe(make_handler({"mairie-longueil-sainte-marie.fr": GOOD}), {"mairie-longueil-sainte-marie.fr"})
        self.assertEqual((r["confiance"], r["url_candidate"]), ("devine_a_verifier", "https://mairie-longueil-sainte-marie.fr/"))

    def test_domaine_existant_mais_autre_site_rejete(self):
        r = self.probe(make_handler({"longueil-sainte-marie.fr": "<p>Boutique de chaussures</p>"}), {"longueil-sainte-marie.fr"})
        self.assertEqual((r["confiance"], r["url_candidate"]), ("aucun_site_trouve", ""))

    def test_robots_interdit_respecte(self):
        r = self.probe(make_handler({"longueil-sainte-marie.fr": GOOD}, robots="User-agent: *\nDisallow: /\n"),
                       {"longueil-sainte-marie.fr"})
        self.assertEqual(r["url_candidate"], "")

    def test_site_heberge_ailleurs_apres_redirection_valide_sur_son_contenu(self):
        def h(req):
            if req.url.path == "/robots.txt":
                return httpx.Response(404)
            if req.url.host == "longueil-sainte-marie.fr":
                return httpx.Response(301, headers={"location": "https://longueil.wixsite.com/mairie"})
            return page(GOOD)
        r = self.probe(h, {"longueil-sainte-marie.fr"})
        self.assertEqual((r["confiance"], r["url_candidate"]), ("devine_a_verifier", "https://longueil.wixsite.com/mairie"))

        def parked(req):                                                  # domaine « à vendre » : redirige vers une page sans rapport
            if req.url.path == "/robots.txt":
                return httpx.Response(404)
            if req.url.host == "longueil-sainte-marie.fr":
                return httpx.Response(302, headers={"location": "https://parking.example/"})
            return page("<p>Ce domaine est à vendre</p>")
        self.assertEqual(self.probe(parked, {"longueil-sainte-marie.fr"})["url_candidate"], "")

    def test_motifs_de_rejet_conserves_pour_diagnostic(self):
        r = self.probe(make_handler({"longueil-sainte-marie.fr": "<p>Longueil-Sainte-Marie, mairie, camping</p>"}),
                       {"longueil-sainte-marie.fr"})
        self.assertEqual(r["url_candidate"], "")
        self.assertIn("longueil-sainte-marie.fr: aucun code postal du département 60", r["rejets"])

    def test_aucun_domaine_resolu(self):
        r = self.probe(make_handler(), set())
        self.assertEqual((r["candidats_testes"], r["confiance"]), (0, "aucun_site_trouve"))


class RunTests(unittest.TestCase):
    def test_run_many_ecrit_et_reprend(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = DeptPaths(Path(tmp), "60")
            p.reports_dir.mkdir(parents=True)
            csvio.write_rows(p.inventory_communes, [
                {"code_insee": "60369", "nom_commune": "Longueil-Sainte-Marie", "nb_urls_valides": "0"},
                {"code_insee": "60001", "nom_commune": "Avec Site", "nb_urls_valides": "1"},
                {"code_insee": "60002", "nom_commune": "Introuvable", "nb_urls_valides": "0"}])
            handler = make_handler({"longueil-sainte-marie.fr": GOOD})
            orig = httpx.AsyncClient
            httpx.AsyncClient = lambda **kw: orig(**{**kw, "transport": httpx.MockTransport(handler)})

            async def res(host):
                return host == "longueil-sainte-marie.fr"
            try:
                args = Namespace(resume=False, concurrency=4, timeout=5, contact="")
                with mock.patch("builtins.print"):
                    self.assertEqual(d.run_many([p], args, resolver=res), 0)
                rows = {r["code_insee"]: r for r in helpers.dict_rows(p.discovered)}
                self.assertEqual(set(rows), {"60369", "60002"})                      # la commune avec site n'est pas testée
                self.assertEqual(rows["60369"]["url_candidate"], "https://longueil-sainte-marie.fr/")
                self.assertEqual(rows["60002"]["confiance"], "aucun_site_trouve")
                calls = []
                handler2 = lambda r: (calls.append(r.url.host), httpx.Response(404))[1]  # noqa: E731
                httpx.AsyncClient = lambda **kw: orig(**{**kw, "transport": httpx.MockTransport(handler2)})
                args.resume = True
                with mock.patch("builtins.print"):
                    d.run_many([p], args, resolver=res)
                self.assertEqual(calls, [])                                          # reprise : rien à refaire
            finally:
                httpx.AsyncClient = orig


if __name__ == "__main__":
    unittest.main()
