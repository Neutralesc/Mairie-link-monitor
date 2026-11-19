"""Tests de l'explorateur : analyse HTML (selectolax), parcours en largeur, arrêt au premier lien, robots.txt,
politesse, échecs, repli, orchestration par département."""
import asyncio
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import HTML, T, page  # noqa: E402
import crawl_links as c  # noqa: E402
import csvio  # noqa: E402
import helpers  # noqa: E402
from layout import DeptPaths  # noqa: E402

OPTS = dict(max_pages=40, max_depth=2, concurrency=4, host_delay=0, timeout=5, site_timeout=30, contact="",
            use_sitemap=False, retry_failed=False, insecure_ssl=True, retry_pause=0, max_par_domaine=4)


def site(url="https://m.fr/", code="60001", name="M", dept="60", **extra):
    return {"code_insee": code, "nom_commune": name, "start_url": url, "confiance_lot2": "probable_officiel",
            "ssl_invalide": False, "_dept": dept, **extra}


def crawl(handler, sites=None, **over):
    return asyncio.run(c.crawl_all(sites or [site()], {**OPTS, **over}, transport=httpx.MockTransport(handler)))


class Chunked(httpx.AsyncByteStream):
    """Corps livré par blocs de 64 Ko, comme sur un vrai réseau."""
    def __init__(self, data: bytes):
        self.data = data

    async def __aiter__(self):
        for i in range(0, len(self.data), 65536):
            yield self.data[i:i + 65536]


class PureTests(unittest.TestCase):
    def test_is_target_url(self):
        for ok in (T, T + "/", "http://vosquestions.ecologie-territoires.gouv.fr/x?y=1",
                   "https://www.vosquestions.ecologie-territoires.gouv.fr", "HTTPS://VosQuestions.Ecologie-Territoires.gouv.fr/"):
            self.assertTrue(c.is_target_url(ok), ok)
        for ko in ("https://ecologie-territoires.gouv.fr", "https://autre.ecologie-territoires.gouv.fr",
                   "https://vosquestions.ecologie-territoires.gouv.fr.evil.com/", "ftp://vosquestions.ecologie-territoires.gouv.fr",
                   "https://exemple.fr/?u=vosquestions.ecologie-territoires.gouv.fr", "mailto:a@b.fr", ""):
            self.assertFalse(c.is_target_url(ko), ko)

    def test_parse_page_selectolax(self):
        html = f'''<html><head><base href="https://mairie.fr/sous/"></head><body>
        <a href="{T}/q">Questions <b>citoyennes</b></a><a href="/contact">Contact</a><a href="a.html#x">A</a>
        <a href="https://autre.fr/">Externe</a><a href="mailto:x@y.fr">m</a><a href="javascript:void(0)">j</a>
        <a href="doc.pdf">pdf</a><a href="http://[bad">invalide</a><a>sans href</a><a href="">vide</a>
        <iframe src="{T}/widget"></iframe><area href="{T}/zone"/></body></html>'''
        d = c.parse_page(html, "https://mairie.fr/", "mairie.fr")
        self.assertEqual({(l["target_url"], l["type_lien"]) for l in d.links},
                         {(T + "/q", "lien"), (T + "/widget", "iframe"), (T + "/zone", "lien")})
        self.assertEqual([l["texte_lien"] for l in d.links if l["target_url"].endswith("/q")], ["Questions citoyennes"])
        self.assertEqual(sorted(d.internal), ["https://mairie.fr/contact", "https://mairie.fr/sous/a.html"])
        self.assertEqual(d.invalid_hrefs, 1)

    def test_parse_page_documents_vides_ou_casses(self):
        for html in ("", "   ", "<", "texte seul", "<a href", "\x00<a href=x>"):
            d = c.parse_page(html, "https://m.fr/", "m.fr")
            self.assertIsInstance(d.links, list)                       # jamais d'exception

    def test_zone_du_lien(self):
        html = (f'<header><a href="{T}/a">h</a></header><div class="main-menu"><a href="{T}/b">m</a></div>'
                f'<footer><a href="{T}/c">f</a></footer><p><a href="{T}/d">texte</a></p>'
                f'<div id="sidebar"><a href="{T}/e">s</a></div>')
        zones = {l["target_url"].rsplit("/", 1)[1]: l["zone"] for l in c.parse_page(html, "https://m.fr/", "m.fr").links}
        self.assertEqual(zones, {"a": "menu_ou_bandeau", "b": "menu_ou_bandeau", "c": "menu_ou_bandeau",
                                 "d": "contenu", "e": "menu_ou_bandeau"})

    def test_cible_propre_a_la_commune(self):
        s = c.target_scope
        self.assertEqual(s(f"{T}/60369-longueil-sainte-marie", "60369"), "oui")
        self.assertEqual(s(f"{T}/60370-autre-commune", "60369"), "autre_commune")
        self.assertEqual(s(f"{T}/", "60369"), "generique")
        self.assertEqual(s(f"{T}/2A004-ajaccio", "2A004"), "oui")

    def test_select_sites_la_verification_n_est_plus_un_filtre(self):
        rows = [dict(code_insee="1", nom_commune="A", url_candidate="https://a.fr", confiance="probable_officiel"),
                dict(code_insee="2", nom_commune="B", url_candidate="https://lapagelocale.fr/x", confiance="tiers_probable"),
                dict(code_insee="3", nom_commune="C", url_candidate="https://x.gouv.fr", confiance="institution_non_communale"),
                dict(code_insee="4", nom_commune="D", url_candidate="https://d.fr", confiance="devine_a_verifier")]
        todo, skipped = c.select_sites(rows)
        self.assertEqual([s["code_insee"] for s in todo], ["1", "2", "4"])
        self.assertEqual([s["code_insee"] for s in skipped], ["3"])
        self.assertEqual([s["code_insee"] for s in c.select_sites(rows, include=("probable_officiel",))[0]], ["1"])

    def test_urls_de_repli(self):
        self.assertEqual(c.alternate_starts("https://www.m.fr/ancienne/page"),
                         ["https://www.m.fr/", "https://m.fr/", "http://www.m.fr/"])
        self.assertEqual(c.alternate_starts("http://m.fr"), ["https://m.fr/", "https://www.m.fr/", "http://www.m.fr/"])
        self.assertNotIn("http://m.fr/", c.alternate_starts("http://m.fr/"))

    def test_shard_deterministe_et_par_domaine(self):
        a = c.shard_of("https://a.e-monsite.com/", 4)
        self.assertEqual(a, c.shard_of("https://b.e-monsite.com/x", 4))        # même domaine d'hébergement, même processus
        self.assertTrue(all(0 <= c.shard_of(f"https://site{i}.fr/", 4) < 4 for i in range(50)))
        self.assertGreater(len({c.shard_of(f"https://site{i}.fr/", 4) for i in range(50)}), 1)


def tree(visited, link_pages=("/environnement/risques",), nav=False):
    def h(req):
        p = req.url.path
        if p in ("/robots.txt", "/sitemap.xml"):
            return httpx.Response(404)
        visited.append(p)
        if p == "/":
            return page('<a href="/actualites">Actualités</a><a href="/contact">Contact</a>'
                        '<a href="/environnement/risques">Risques</a><a href="/ecole">École</a>')
        if p in link_pages:
            return page(f'<a href="{T}/">Vos questions</a>')
        return page("<p>page</p>")
    return h


class CrawlTests(unittest.TestCase):
    def test_largeur_sans_priorite_puis_arret_au_premier_lien(self):
        visited = []
        (res,) = crawl(tree(visited))
        # ordre d'apparition dans la page d'accueil, aucun tri par contenu ; arrêt dès le premier lien
        self.assertEqual(visited, ["/", "/actualites", "/contact", "/environnement/risques"])
        self.assertEqual(res.row["statut_crawl"], "lien_trouve")
        self.assertEqual(res.row["pages_visitees"], 4)
        self.assertEqual(res.links[0]["page_url"], "https://m.fr/environnement/risques")
        self.assertEqual(res.links[0]["cible_commune"], "generique")

    def test_tous_les_niveaux_1_avant_le_niveau_2(self):
        visited = []

        def h(req):
            p = req.url.path
            if p in ("/robots.txt", "/sitemap.xml"):
                return httpx.Response(404)
            visited.append(p)
            if p == "/":
                return page("".join(f'<a href="/n1/{i}">{i}</a>' for i in range(6)))
            if p.startswith("/n1/"):
                return page("".join(f'<a href="/n2/{p[-1]}{j}">x</a>' for j in range(6)))
            return page("<p>x</p>")
        crawl(h)
        first_l2 = next(i for i, p in enumerate(visited) if p.startswith("/n2/"))
        self.assertEqual(first_l2, 7)                                    # accueil + 6 pages de niveau 1

    def test_page_banale_proche_de_l_accueil_jamais_ecartee(self):
        def h(req):
            p = req.url.path
            if p in ("/robots.txt", "/sitemap.xml"):
                return httpx.Response(404)
            if p == "/":
                return page('<a href="/contact">Contact</a>' + "".join(f'<a href="/environnement/t{i}">E{i}</a>' for i in range(8)))
            if p == "/contact":
                return page(f'<a href="{T}/">Vos questions</a>')
            if p.startswith("/environnement/t"):
                return page("".join(f'<a href="/risques/r{p[-1]}{j}">R</a>' for j in range(8)))
            return page("<p>x</p>")
        (res,) = crawl(h, max_pages=12)                                   # plafond bien inférieur au nombre de pages
        self.assertEqual(res.row["statut_crawl"], "lien_trouve")

    def test_site_sans_lien_explore_complet_ou_partiel(self):
        visited = []
        (res,) = crawl(tree(visited, link_pages=()))
        self.assertEqual((res.row["statut_crawl"], res.row["pages_visitees"]), ("explore_complet", 5))
        (res,) = crawl(tree([], link_pages=()), max_pages=2)
        self.assertEqual((res.row["statut_crawl"], res.row["limite_pages_atteinte"]), ("explore_partiel", 1))
        (res,) = crawl(tree([], link_pages=()), max_depth=0)
        self.assertEqual(res.row["limite_profondeur_atteinte"], 1)

    def test_robots(self):
        def robots(text=None, status=None):
            def h(req):
                if req.url.path == "/robots.txt":
                    return httpx.Response(status, headers={"content-type": "text/plain"}, text=text or "") if status else \
                        httpx.Response(200, headers={"content-type": "text/plain"}, text=text)
                return page(f'<a href="/prive/x">p</a><a href="{T}/">Vos questions</a>' if req.url.path == "/" else "<p>x</p>")
            return h
        (res,) = crawl(robots("User-agent: *\nDisallow: /\n"))
        self.assertEqual((res.row["statut_crawl"], res.row["pages_visitees"]), ("robots_interdit", 0))
        (res,) = crawl(robots(status=503))
        self.assertEqual((res.row["statut_crawl"], res.row["pages_visitees"]), ("robots_indisponible", 0))
        (res,) = crawl(robots(status=404))
        self.assertEqual(res.row["statut_crawl"], "lien_trouve")         # pas de robots.txt : exploration autorisée
        (res,) = crawl(robots(status=403))
        self.assertEqual(res.row["statut_crawl"], "robots_interdit")     # 403 : tout interdit
        (res,) = crawl(robots("User-agent: *\nDisallow: /prive\n"), )
        self.assertEqual(res.row["statut_crawl"], "lien_trouve")

    def test_robots_qui_redirige_vers_un_autre_domaine_est_suivi(self):
        def h(req):
            if req.url.host == "m.fr" and req.url.path == "/robots.txt":
                return httpx.Response(301, headers={"location": "https://autre.fr/robots.txt"})
            if req.url.host == "autre.fr" and req.url.path == "/robots.txt":
                return httpx.Response(200, headers={"content-type": "text/plain"}, text="User-agent: *\nDisallow: /prive\n")
            return page(f'<a href="{T}/">Vos questions</a>')
        (res,) = crawl(h)
        self.assertEqual(res.row["statut_crawl"], "lien_trouve")         # avant : robots_indisponible (HTTP 301)

    def test_site_deplace_vers_un_autre_domaine_est_explore_a_sa_nouvelle_adresse(self):
        def h(req):
            p = req.url.path
            if p in ("/robots.txt", "/sitemap.xml"):
                return httpx.Response(404)
            if req.url.host == "m.fr":
                return httpx.Response(301, headers={"location": "https://nouveau.fr/accueil"})
            return page('<a href="/page2">p2</a>' if p == "/accueil" else f'<a href="{T}/">Vos questions</a>')
        (res,) = crawl(h)
        self.assertEqual(res.row["statut_crawl"], "lien_trouve")
        self.assertEqual(res.row["url_utilisee"], "https://nouveau.fr/accueil")
        self.assertIn("site déplacé", res.row["erreur"])
        self.assertEqual(res.links[0]["page_url"], "https://nouveau.fr/page2")     # liens internes suivis sur le nouveau domaine

    def test_robots_503_passager_retente(self):
        state = {"n": 0}

        def h(req):
            if req.url.path == "/robots.txt":
                state["n"] += 1
                return httpx.Response(503) if state["n"] <= 4 else httpx.Response(404)   # 4 = accueil + 3 URL de repli
            return page(f'<a href="{T}/">Vos questions</a>')
        with mock.patch.object(c.Throttle, "slow_down"):         # le ralentissement 429/503 est testé à part
            (res,) = crawl(h, retry_failed=True)
            self.assertEqual((res.row["statut_crawl"], res.row["tentatives"]), ("lien_trouve", 2))
            state["n"] = 0
            (res,) = crawl(h, retry_failed=False)
            self.assertEqual(res.row["statut_crawl"], "robots_indisponible")

    def test_disallow_partiel_respecte_et_compte(self):
        visited = []

        def h(req):
            p = req.url.path
            if p == "/robots.txt":
                return httpx.Response(200, headers={"content-type": "text/plain"}, text="User-agent: *\nDisallow: /prive\n")
            visited.append(p)
            return page('<a href="/prive/x">p</a><a href="/public">q</a>' if p == "/" else "<p>x</p>")
        (res,) = crawl(h)
        self.assertNotIn("/prive/x", visited)
        self.assertEqual(res.row["pages_interdites_robots"], 1)
        self.assertEqual(res.row["statut_crawl"], "explore_complet")       # l'interdit n'est pas « atteignable » : signalé
        self.assertIn("interdites par robots.txt", res.row["motif"])

    def test_crawl_delay_robots_respecte(self):
        throttle = c.Throttle(0.0)
        secure = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(
            200, headers={"content-type": "text/plain"}, text="User-agent: *\nCrawl-delay: 1\n")
            if r.url.path == "/robots.txt" else page("<p>x</p>")))
        asyncio.run(c.crawl_site(site(), (secure, None), throttle, {**OPTS, "max_pages": 1}))
        self.assertEqual(throttle.per_host["m.fr"], 1.0)

    def test_429_ralentit_le_site(self):
        throttle = c.Throttle(1.0)

        def h(req):
            return httpx.Response(429) if req.url.path != "/robots.txt" else httpx.Response(404)
        cl = httpx.AsyncClient(transport=httpx.MockTransport(h))
        asyncio.run(c.crawl_site(site(), (cl, None), c.Throttle(0.0), {**OPTS, "max_pages": 1}))
        t2 = c.Throttle(0.0)
        asyncio.run(c.crawl_site(site(), (cl, None), t2, {**OPTS, "max_pages": 1}))
        self.assertGreaterEqual(t2.per_host.get("m.fr", 0), 0.5)         # délai porté à ≥ 0,5 s puis doublé

    def test_erreur_acces_et_redirection_externe(self):
        def h(req):
            if req.url.path == "/robots.txt":
                return httpx.Response(404)
            raise httpx.ConnectTimeout("t", request=req)
        (res,) = crawl(h)
        self.assertEqual(res.row["statut_crawl"], "erreur_acces")
        self.assertIn("timeout", res.row["motif"])

        def redir(req):
            return httpx.Response(404) if req.url.path == "/robots.txt" else httpx.Response(302, headers={"location": "https://autre.fr/"})
        (res,) = crawl(redir)                                 # boucle de redirections entre domaines : abandon, jamais infini
        self.assertEqual(res.row["statut_crawl"], "erreur_acces")
        self.assertIn("redirections", res.row["motif"])

    def test_nouvelle_tentative_apres_echec_passager(self):
        state = {"n": 0}

        def h(req):
            if req.url.path in ("/robots.txt", "/sitemap.xml"):
                return httpx.Response(404)
            state["n"] += 1
            if state["n"] <= 4:                                          # l'accueil ET ses 3 URL de repli échouent
                raise httpx.ReadTimeout("lent", request=req)
            return page(f'<a href="{T}/">Vos questions</a>')
        (res,) = crawl(h, retry_failed=True)
        self.assertEqual((res.row["statut_crawl"], res.row["tentatives"]), ("lien_trouve", 2))
        state["n"] = 0
        (res,) = crawl(h, retry_failed=False)
        self.assertEqual((res.row["statut_crawl"], res.row["tentatives"]), ("erreur_acces", 1))

    def test_urls_de_repli_racine_et_www(self):
        def h(req):
            p = req.url.path
            if p in ("/robots.txt", "/sitemap.xml") or p == "/ancienne-page":
                return httpx.Response(404)
            return page(f'<a href="{T}/">Vos questions</a>') if p == "/" else httpx.Response(404)
        (res,) = crawl(h, sites=[site("https://m.fr/ancienne-page")])
        self.assertEqual((res.row["statut_crawl"], res.row["url_utilisee"]), ("lien_trouve", "https://m.fr/"))

        def h2(req):
            if req.url.path in ("/robots.txt", "/sitemap.xml"):
                return httpx.Response(404)
            if req.url.host == "m.fr":
                raise httpx.ConnectError("Name or service not known", request=req)
            return page(f'<a href="{T}/">Vos questions</a>')
        (res,) = crawl(h2)
        self.assertEqual(res.row["url_utilisee"], "https://www.m.fr/")

    def test_certificat_invalide_bascule_sans_verification_et_signale(self):
        ok = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404) if r.url.path == "/robots.txt"
                                                             else page(f'<a href="{T}/">Vos questions</a>')))

        def bad(req):
            raise httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed", request=req)
        secure = httpx.AsyncClient(transport=httpx.MockTransport(bad))
        s = site()
        res = asyncio.run(c.crawl_site(s, (secure, ok), c.Throttle(0.0), OPTS))
        self.assertEqual(res.row["statut_crawl"], "lien_trouve")
        self.assertEqual((res.row["ssl_invalide"], res.links[0]["ssl_invalide"]), (1, 1))
        res = asyncio.run(c.crawl_site(site(), (secure, None), c.Throttle(0.0), OPTS))   # exploration désactivée
        self.assertEqual(res.row["statut_crawl"], "erreur_acces")
        self.assertIn("erreur_ssl", res.row["motif"])

    def test_pied_de_page_d_une_page_lourde_lu_et_troncature_signalee(self):
        def big(extra):
            def h(req):
                if req.url.path != "/":                                   # robots.txt, plan du site, pages usuelles
                    return httpx.Response(404)
                return httpx.Response(200, headers=HTML, stream=Chunked(("<div>" + "x" * extra + f'</div><footer><a href="{T}/">Vos questions</a></footer>').encode()))
            return h
        (res,) = crawl(big(1_500_000))
        self.assertEqual(res.row["statut_crawl"], "lien_trouve")
        (res,) = crawl(big(6_000_000))                                    # au-delà de la limite : jamais « complet »
        self.assertEqual((res.row["statut_crawl"], res.row["pages_tronquees"]), ("explore_partiel", 1))

    def test_sitemap_option_dans_l_ordre_du_fichier(self):
        visited = []

        def h(req):
            p = req.url.path
            if p == "/robots.txt":
                return httpx.Response(200, headers={"content-type": "text/plain"}, text="User-agent: *\nDisallow: /prive\n")
            if p == "/sitemap.xml":
                return httpx.Response(200, headers={"content-type": "application/xml"}, text=(
                    "<urlset><url><loc>https://m.fr/a</loc></url><url><loc>https://m.fr/prive/b</loc></url>"
                    "<url><loc>https://m.fr/c</loc></url></urlset>"))
            visited.append(p)
            return page(f'<a href="{T}/">V</a>' if p == "/c" else "<p>x</p>")
        (res,) = crawl(h, use_sitemap=True)
        self.assertEqual(visited, ["/", "/a", "/c"])                     # ordre du fichier ; /prive interdit
        self.assertEqual(res.row["statut_crawl"], "lien_trouve")
        visited.clear()
        crawl(h, use_sitemap=False)
        # Sans l'option, le plan du site n'est lu que si l'accueil n'a aucun lien interne (plan B) : c'est le cas ici.
        self.assertEqual(visited, ["/", "/a", "/c"])

    def test_plafond_par_domaine_d_hebergement(self):
        state = {"active": 0, "max": 0}

        async def h(req):
            if req.url.path == "/robots.txt":
                return httpx.Response(404)
            state["active"] += 1
            state["max"] = max(state["max"], state["active"])
            await asyncio.sleep(0.03)
            state["active"] -= 1
            return page("<p>x</p>")
        sites = [site(f"https://s{i}.e-monsite.com/", code=f"6{i:04d}") for i in range(10)]
        asyncio.run(c.crawl_all(sites, {**OPTS, "concurrency": 10, "max_par_domaine": 2}, transport=httpx.MockTransport(h)))
        self.assertLessEqual(state["max"], 2)
        state["max"] = 0
        asyncio.run(c.crawl_all(sites, {**OPTS, "concurrency": 10, "max_par_domaine": 5}, transport=httpx.MockTransport(h)))
        self.assertGreater(state["max"], 2)


def inventory(paths: DeptPaths, sites: list[tuple[str, str, str]]) -> None:
    paths.reports_dir.mkdir(parents=True, exist_ok=True)
    csvio.write_rows(paths.inventory_urls, [dict(code_insee=code, nom_commune=name, url_candidate=url,
                                                 statut_url="url_non_verifiee") for code, name, url in sites])


def args(**over):
    base = dict(max_pages=40, max_depth=2, concurrency=4, processes=1, host_delay=0, timeout=5, site_timeout=30,
                include=list(c.DEFAULT_INCLUDE), no_insecure_ssl=False, limit_sites=0, commune=None, contact="",
                resume=False, use_sitemap=False, recheck_known=True, retry_failed=False, max_par_domaine=4)
    base.update(over)
    return Namespace(**base)


class RunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.requests = []

        def h(req):
            self.requests.append(f"{req.url.host}{req.url.path}")
            p = req.url.path
            if p in ("/robots.txt", "/sitemap.xml"):
                return httpx.Response(404)
            if req.url.host in ("a.fr", "c.fr", "e.fr"):
                return page(f'<a href="{T}/">Vos questions</a>' if p == "/" else "<p>x</p>")
            return page('<a href="/p">p</a>' if p == "/" else "<p>x</p>")
        self.handler = h
        self.orig = httpx.AsyncClient
        httpx.AsyncClient = lambda **kw: self.orig(**{**kw, "transport": httpx.MockTransport(self.handler)})

    def tearDown(self):
        httpx.AsyncClient = self.orig
        self.tmp.cleanup()

    def test_fichiers_ecrits_et_statuts(self):
        p = DeptPaths(self.root, "60")
        inventory(p, [("60001", "A", "https://a.fr/"), ("60002", "B", "https://b.fr/"),
                      ("60003", "Gouv", "https://www.gendarmerie.interieur.gouv.fr/")])
        with mock.patch("builtins.print"):
            self.assertEqual(c.run_many([p], args()), 0)
        sites = {r["code_insee"]: r for r in helpers.dict_rows(p.crawl_sites)}
        self.assertEqual(sites["60001"]["statut_crawl"], "lien_trouve")
        self.assertEqual(sites["60002"]["statut_crawl"], "explore_complet")
        self.assertEqual(sites["60003"]["statut_crawl"], "non_explore")
        self.assertEqual([r["code_insee"] for r in helpers.dict_rows(p.links_found)], ["60001"])
        self.assertTrue(p.crawl_summary.exists())

    def test_resultat_ecrit_a_la_fin_de_chaque_departement(self):
        p60, p80 = DeptPaths(self.root, "60"), DeptPaths(self.root, "80")
        inventory(p60, [("60001", "A", "https://a.fr/"), ("60002", "B", "https://b.fr/")])
        inventory(p80, [("80001", "C", "https://c.fr/"), ("80002", "D", "https://d.fr/")])
        events = []

        def done(paths):
            events.append((paths.dept, paths.crawl_sites.exists(), len(helpers.dict_rows(paths.crawl_sites)),
                           DeptPaths(self.root, "80").crawl_sites.exists()))
        with mock.patch("builtins.print"):
            c.run_many([p60, p80], args(concurrency=1), on_dept_done=done)
        self.assertEqual(events[0], ("60", True, 2, False))              # 60 écrit complet AVANT que 80 soit commencé
        self.assertEqual(events[1][0], "80")
        self.assertEqual(len(events), 2)

    def test_points_de_sauvegarde_pendant_l_exploration(self):
        p = DeptPaths(self.root, "60")
        inventory(p, [(f"6{i:04d}", f"S{i}", f"https://s{i}.fr/") for i in range(6)])
        seen_rows = []
        base = self.handler

        def h(req):
            if req.url.host == "s5.fr" and req.url.path == "/":          # dernier site : un point de sauvegarde existe déjà
                seen_rows.append(len(helpers.dict_rows(p.crawl_sites)) if p.crawl_sites.exists() else 0)
            return base(req)
        self.handler = h
        with mock.patch.object(c, "CHECKPOINT_EVERY", 2), mock.patch("builtins.print"):
            c.run_many([p], args(concurrency=1))
        self.assertGreaterEqual(seen_rows[0], 4)

    def test_reprise_ne_refait_pas_les_sites_termines_mais_retente_les_echecs(self):
        p = DeptPaths(self.root, "60")
        inventory(p, [("60001", "A", "https://a.fr/"), ("60002", "B", "https://b.fr/")])
        with mock.patch("builtins.print"):
            c.run_many([p], args())
            self.requests.clear()
            c.run_many([p], args(resume=True))
        self.assertEqual(self.requests, [])
        rows = helpers.dict_rows(p.crawl_sites)
        rows[1]["statut_crawl"] = "erreur_acces"
        csvio.write_rows(p.crawl_sites, rows, c.SITE_FIELDS)
        with mock.patch("builtins.print"):
            c.run_many([p], args(resume=True))
        self.assertTrue(any(r.startswith("b.fr") for r in self.requests))
        self.assertFalse(any(r.startswith("a.fr") for r in self.requests))

    def test_relance_revérifie_d_abord_la_page_connue(self):
        p = DeptPaths(self.root, "60")
        inventory(p, [("60001", "A", "https://a.fr/")])
        csvio.write_rows(p.links_found, [dict(code_insee="60001", site_url="https://a.fr/", page_url="https://a.fr/ancien",
                                              target_url=T, type_lien="lien")], c.LINK_FIELDS)
        with mock.patch("builtins.print"):
            c.run_many([p], args())
        self.assertEqual([r for r in self.requests if not r.endswith("robots.txt")][0], "a.fr/ancien")

    def test_essai_partiel_conserve_les_autres_sites(self):
        p = DeptPaths(self.root, "60")
        inventory(p, [("60001", "A", "https://a.fr/"), ("60002", "B", "https://b.fr/")])
        with mock.patch("builtins.print"):
            c.run_many([p], args())
            before = helpers.dict_rows(p.links_found)
            c.run_many([p], args(commune=["60002"]))
        self.assertEqual({r["code_insee"] for r in helpers.dict_rows(p.crawl_sites)}, {"60001", "60002"})
        self.assertEqual(len(helpers.dict_rows(p.links_found)), len(before))

    def test_interruption_ecrit_les_resultats_partiels(self):
        p = DeptPaths(self.root, "60")
        inventory(p, [("60001", "A", "https://a.fr/"), ("60002", "B", "https://b.fr/")])

        def interrupted(sites, opts, on_result, workers=1, on_progress=None, on_metrics=None):
            res = asyncio.run(c.crawl_all(sites[:1], opts))
            on_result(res[0])
            raise KeyboardInterrupt
        with mock.patch.object(c, "crawl_sites", interrupted), mock.patch("builtins.print"):
            self.assertEqual(c.run_many([p], args()), 130)
        self.assertEqual([r["code_insee"] for r in helpers.dict_rows(p.crawl_sites)], ["60001"])

    def test_reprise_refait_les_sites_partiels_quand_les_limites_sont_relevees(self):
        p = DeptPaths(self.root, "60")
        inventory(p, [("60002", "B", "https://b.fr/")])
        with mock.patch("builtins.print"):
            c.run_many([p], args(max_pages=1))                           # b.fr : 1 page lue, limite de pages atteinte
            self.assertEqual(helpers.dict_rows(p.crawl_sites)[0]["statut_crawl"], "explore_partiel")
            self.requests.clear()
            c.run_many([p], args(max_pages=1, resume=True))               # mêmes limites : rien à refaire
            self.assertEqual(self.requests, [])
            c.run_many([p], args(max_pages=10, resume=True))              # limites relevées : le site partiel est refait
        self.assertTrue(self.requests)
        self.assertEqual(helpers.dict_rows(p.crawl_sites)[0]["statut_crawl"], "explore_complet")

    def test_les_sites_les_plus_lents_demarrent_en_premier(self):
        p = DeptPaths(self.root, "60")
        inventory(p, [("60001", "A", "https://a.fr/"), ("60002", "B", "https://b.fr/"), ("60003", "C", "https://c.fr/")])
        csvio.write_rows(p.crawl_sites, [
            dict(code_insee="60001", site_url="https://a.fr/", statut_crawl="erreur_acces", duree_s="1.0"),
            dict(code_insee="60002", site_url="https://b.fr/", statut_crawl="erreur_acces", duree_s="150.0")], c.SITE_FIELDS)
        with mock.patch("builtins.print"):
            c.run_many([p], args(concurrency=1))
        order = [r.split("/")[0] for r in self.requests if r.endswith("robots.txt")]
        self.assertEqual(order[0], "b.fr")                                # le plus lent d'abord
        self.assertEqual(order[1:], ["c.fr", "a.fr"])                     # inconnu (5 s par défaut) avant le plus rapide

    def test_reprise_refait_les_sites_coupes_par_la_duree_si_elle_est_augmentee(self):
        p = DeptPaths(self.root, "60")
        inventory(p, [("60002", "B", "https://b.fr/")])
        csvio.write_rows(p.crawl_sites, [dict(code_insee="60002", site_url="https://b.fr/", statut_crawl="explore_partiel",
                                              motif="durée maximale par site atteinte", duree_s="60.0", pages_visitees="12")],
                         c.SITE_FIELDS)
        with mock.patch("builtins.print"):
            c.run_many([p], args(resume=True, site_timeout=60))           # même durée : rien à refaire
            self.assertEqual(self.requests, [])
            c.run_many([p], args(resume=True, site_timeout=180))          # durée augmentée : refait
        self.assertTrue(self.requests)

    def test_sites_devines_ajoutes_aux_candidats(self):
        p = DeptPaths(self.root, "60")
        inventory(p, [("60001", "A", "https://a.fr/")])
        csvio.write_rows(p.discovered, [dict(code_insee="60002", nom_commune="C", url_candidate="https://c.fr/",
                                             statut_url="url_non_verifiee", confiance="devine_a_verifier"),
                                        dict(code_insee="60003", nom_commune="Z", url_candidate="",
                                             confiance="aucun_site_trouve")])
        with mock.patch("builtins.print"):
            c.run_many([p], args())
        links = {r["code_insee"]: r for r in helpers.dict_rows(p.links_found)}
        self.assertEqual(links["60002"]["confiance_site"], "devine_a_verifier")
        self.assertNotEqual(links["60001"]["confiance_site"], "devine_a_verifier")


if __name__ == "__main__":
    unittest.main()
