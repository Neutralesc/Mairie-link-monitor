"""Tests des correctifs récents de l'explorateur : statuts robots (401/403/503), accueil non HTML, erreurs internes,
résolution DNS (noms invalides et inexistants), délai de site, gabarits de pagination, identité de page, sitemap."""
import asyncio
import importlib.util
import socket
import sys
import unittest
from pathlib import Path
from unittest import mock

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import T, page  # noqa: E402  (common remplace crawl_links.resolve par une fausse résolution)
import crawl_links as c  # noqa: E402

# Copie fraîche du module, chargée sans le remplacement de `resolve` fait par common.py : les tests DNS testent
# la vraie fonction. Aucune résolution réelle : socket/loop.getaddrinfo sont simulés, ou le nom est invalide
# (« a..fr » échoue à l'encodage IDNA, avant tout accès réseau).
_SRC = Path(__file__).resolve().parents[1] / "src" / "crawl_links.py"
_spec = importlib.util.spec_from_file_location("crawl_links_sans_stub", _SRC)
FRESH = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = FRESH
_spec.loader.exec_module(FRESH)

OPTS = dict(max_pages=40, max_depth=2, concurrency=4, host_delay=0, timeout=5, site_timeout=30, contact="",
            use_sitemap=False, retry_failed=False, insecure_ssl=True, retry_pause=0, max_par_domaine=4)
HTML_PLAIN = {"content-type": "text/plain; charset=utf-8"}


def site(url="https://m.fr/", code="60001", name="M", dept="60", **extra):
    return {"code_insee": code, "nom_commune": name, "start_url": url, "confiance_lot2": "probable_officiel",
            "ssl_invalide": False, "_dept": dept, **extra}


def crawl(handler, sites=None, **over):
    return asyncio.run(c.crawl_all(sites or [site()], {**OPTS, **over}, transport=httpx.MockTransport(handler)))


def is_robots(req: httpx.Request) -> bool:
    return req.url.path == "/robots.txt"


def home_with_target(req: httpx.Request, robots_status: int) -> httpx.Response:
    if is_robots(req):
        return httpx.Response(robots_status)
    return page(f'<a href="{T}/">Vos questions</a>')


class RobotsStatusTests(unittest.TestCase):
    def test_robots_401_n_interdit_pas_l_exploration(self):
        (res,) = crawl(lambda req: home_with_target(req, 401))
        self.assertEqual(res.row["statut_crawl"], "lien_trouve")         # RFC 9309 : 4xx = pas de restriction

    def test_robots_403_n_interdit_pas_l_exploration(self):
        (res,) = crawl(lambda req: home_with_target(req, 403))
        self.assertEqual(res.row["statut_crawl"], "lien_trouve")

    def test_robots_503_reste_indisponible(self):
        with mock.patch.object(c.Throttle, "slow_down"):                 # le ralentissement 503 est testé ailleurs
            (res,) = crawl(lambda req: home_with_target(req, 503))
        self.assertEqual((res.row["statut_crawl"], res.row["pages_visitees"]), ("robots_indisponible", 0))

    def test_robots_503_sur_une_autre_origine_profondeur_1(self):
        requested = []

        def h(req):
            requested.append(str(req.url))
            if is_robots(req):
                return httpx.Response(503 if req.url.host == "www.m.fr" else 404)
            if req.url.path == "/":
                return page('<a href="https://www.m.fr/page">Page</a>')
            return page("<p>x</p>")
        with mock.patch.object(c.Throttle, "slow_down"):
            (res,) = crawl(h)
        self.assertGreaterEqual(res.row["pages_en_erreur"], 1)
        self.assertEqual(res.row["statut_crawl"], "explore_partiel")
        self.assertNotIn("https://www.m.fr/page", requested)            # la page n'est pas lue sans robots.txt


class AccueilTests(unittest.TestCase):
    def test_accueil_text_plain_est_une_erreur_non_html(self):
        def h(req):
            if is_robots(req):
                return httpx.Response(404)
            return httpx.Response(200, headers=HTML_PLAIN, text="bonjour")
        (res,) = crawl(h)
        self.assertEqual(res.row["statut_crawl"], "erreur_acces")
        self.assertIn("non_html", res.row["motif"])

    def test_timeout_de_site_sans_page_lue(self):
        async def h(req):
            if is_robots(req):
                return httpx.Response(404)
            await asyncio.Event().wait()                                 # réponse qui n'arrive jamais
        (res,) = crawl(h, site_timeout=0.05)
        self.assertEqual((res.row["statut_crawl"], res.row["pages_visitees"]), ("erreur_acces", 0))
        self.assertIn("sans page lue", res.row["motif"])


class ErreurInterneTests(unittest.TestCase):
    def test_exception_interne_n_arrete_pas_les_autres_sites(self):
        original = c.crawl_site

        async def fautif(site, clients, throttle, opts, attempt=1):
            if site["code_insee"] == "60001":
                raise RuntimeError("boum")
            return await original(site, clients, throttle, opts, attempt)

        def h(req):
            if is_robots(req):
                return httpx.Response(404)
            if req.url.host == "n.fr":
                return page(f'<a href="{T}/">Vos questions</a>')
            return page("<p>x</p>")
        with mock.patch.object(c, "crawl_site", fautif):
            results = crawl(h, sites=[site(code="60001"), site(url="https://n.fr/", code="60002", name="N")])
        by_code = {r.row["code_insee"]: r.row for r in results}
        self.assertEqual(set(by_code), {"60001", "60002"})
        self.assertEqual(by_code["60001"]["statut_crawl"], "erreur_acces")
        self.assertTrue(by_code["60001"]["motif"].startswith("erreur interne"))
        self.assertEqual(by_code["60002"]["statut_crawl"], "lien_trouve")


class DnsTests(unittest.TestCase):
    def test_resolve_nom_mal_forme_ne_leve_pas(self):
        motif = asyncio.run(FRESH.resolve("a..fr"))
        self.assertIsInstance(motif, str)
        self.assertTrue(motif)

    def test_resolve_nom_inexistant_motif_nxdomain(self):
        calls = []

        async def faux(self, host, port, **kw):
            calls.append(host)
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        with mock.patch.object(asyncio.base_events.BaseEventLoop, "getaddrinfo", faux):
            motif = asyncio.run(FRESH.resolve("nexistepas.invalid"))
        self.assertEqual(len(calls), 1)
        self.assertTrue(motif.startswith(FRESH.NXDOMAIN_PREFIX), motif)

    def test_prefetch_dns_ne_reessaie_pas_un_nom_inexistant(self):
        calls = []

        async def faux(self, host, port, **kw):
            calls.append(host)
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

        async def run():
            return await FRESH.prefetch_dns("https://nexistepas.invalid/", asyncio.Semaphore(1), 0)
        with mock.patch.object(asyncio.base_events.BaseEventLoop, "getaddrinfo", faux):
            err = asyncio.run(run())
        self.assertEqual(len(calls), 1)
        self.assertTrue(err.startswith(FRESH.NXDOMAIN_PREFIX), err)

    def test_prefetch_dns_reessaie_une_erreur_passagere(self):
        calls = []

        async def faux(self, host, port, **kw):
            calls.append(host)
            raise socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")

        async def run():
            return await FRESH.prefetch_dns("https://passagere.invalid/", asyncio.Semaphore(1), 0)
        with mock.patch.object(asyncio.base_events.BaseEventLoop, "getaddrinfo", faux):
            err = asyncio.run(run())
        self.assertEqual(len(calls), FRESH.DNS_ATTEMPTS)
        self.assertTrue(err)
        self.assertFalse(err.startswith(FRESH.NXDOMAIN_PREFIX))


class GabaritsEtIdentiteTests(unittest.TestCase):
    def test_trap_key_page_de_contenu_avec_parametre_non_numerique(self):
        self.assertEqual(c.trap_key("http://x.fr/?page=contact"), "")

    def test_trap_key_calendrier(self):
        self.assertEqual(c.trap_key("http://x.fr/agenda?date=2026-03"), "/agenda?date")

    def test_trap_key_pagination_numerique(self):
        self.assertEqual(c.trap_key("http://x.fr/?page=2"), "/?page")

    def test_identite_de_page_une_seule_requete(self):
        visited = []

        def h(req):
            p = req.url.path
            if p in ("/robots.txt", "/sitemap.xml"):
                return httpx.Response(404)
            visited.append(p)
            if p == "/":
                return page('<a href="/fr">A</a><a href="/fr/">B</a><a href="/fr/index.html">C</a>')
            return page("<p>x</p>")
        crawl(h)
        self.assertEqual(sum(p.startswith("/fr") for p in visited), 1)

    def test_sitemap_amp_decode(self):
        xml = '<urlset><url><loc>https://m.fr/a?x=1&amp;y=2</loc></url></urlset>'
        self.assertEqual(c.sitemap_locs(xml), ["https://m.fr/a?x=1&y=2"])

    def test_sitemap_candidates_amp_decode(self):
        xml = '<urlset><url><loc>https://m.fr/a?x=1&amp;y=2</loc></url></urlset>'

        async def get(url, **kw):
            return c.Fetched(status=200, final_url=url, html=xml, content_type="application/xml")
        out = asyncio.run(c.sitemap_candidates(get, "https://m.fr/", None, "m.fr"))
        self.assertIn("https://m.fr/a?x=1&y=2", out)


if __name__ == "__main__":
    unittest.main()
