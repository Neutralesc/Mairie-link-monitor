"""v0.16 : liens non balisés, accueil sans lien (plan B), pièges d'URL, robots.txt en redirection sans issue, TLS ancien."""
import asyncio
import contextlib
import io
import ssl
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import HTML, T, page  # noqa: E402
import crawl_links as c  # noqa: E402
import helpers  # noqa: E402
from layout import DeptPaths  # noqa: E402
from test_crawl_links import args, crawl, inventory, site  # noqa: E402


def kinds(html: str) -> list[tuple[str, str]]:
    return [(l["type_lien"], l["target_url"]) for l in c.parse_page(html, "https://m.fr/", "m.fr").links]


class PureTests(unittest.TestCase):
    def test_liens_non_balises(self):
        self.assertEqual(kinds(f"<a href='#' onclick=\"window.open('{T}/60618-x')\">x</a>"), [("lien_attribut", f"{T}/60618-x")])
        self.assertEqual(kinds(f'<div data-href="//vosquestions.ecologie-territoires.gouv.fr/60618">x</div>'),
                         [("lien_attribut", f"{T}/60618")])
        self.assertEqual(kinds('<script>var u="https:\\/\\/vosquestions.ecologie-territoires.gouv.fr\\/60618";</script>'),
                         [("lien_script", f"{T}/60618")])
        self.assertEqual(kinds('<a href="/redir?u=https%3A%2F%2Fvosquestions.ecologie-territoires.gouv.fr%2Fx">x</a>'),
                         [("lien_attribut", f"{T}/x")])
        self.assertEqual(kinds("<p>Voir vosquestions.ecologie-territoires.gouv.fr</p>"), [("texte", T)])

    def test_ce_qui_n_est_pas_un_lien(self):
        self.assertEqual(kinds(f'<head><link rel="preconnect" href="{T}"><meta content="{T}"></head><body>x</body>'), [])
        self.assertEqual(kinds('<a href="https://evil-vosquestions.ecologie-territoires.gouv.fr/">x</a>'), [])
        self.assertEqual(kinds('<a href="https://vosquestions.ecologie-territoires.gouv.fr.evil.com/">x</a>'), [])

    def test_lien_balise_prioritaire_pas_de_doublon(self):
        self.assertEqual(kinds(f'<a href="{T}/ok" onclick="x">x</a><p>vosquestions.ecologie-territoires.gouv.fr</p>'),
                         [("lien", f"{T}/ok")])

    def test_url_canonique_sans_suivi_et_gabarits(self):
        self.assertEqual(c.normalize_internal("https://m.fr/a?utm_source=x&id=3&fbclid=9#z"), "https://m.fr/a?id=3")
        self.assertEqual(c.normalize_internal("https://m.fr/a?utm_source=x"), "https://m.fr/a")
        self.assertEqual(c.trap_key("https://m.fr/agenda?date=2026-03&x=1"), "/agenda?date&x")
        self.assertEqual(c.trap_key("https://m.fr/blog/page/12/"), "/blog/page/#/")
        self.assertEqual(c.trap_key("https://m.fr/archives/2024/05/"), "/archives/#/#/")
        for page_de_contenu in ("https://m.fr/node/12", "https://m.fr/?p=12", "https://m.fr/actualite/mairie-ouverte"):
            self.assertEqual(c.trap_key(page_de_contenu), "", page_de_contenu)

    def test_cause_d_un_accueil_vide(self):
        def cause(html):
            return c.parse_page(html, "https://m.fr/", "m.fr", diagnose=True).cause
        self.assertIn("anti-robot", cause("<title>Just a moment...</title><body>x</body>"))
        self.assertIn("JavaScript", cause("<script src=a></script><script src=b></script><script src=c></script><div id=root></div>"))
        self.assertIn("aucun lien", cause("<p>Bienvenue</p>"))
        self.assertIn("autres domaines", cause("<a href='https://x.fr/1'>1</a><a href='https://x.fr/2'>2</a><a href='https://x.fr/3'>3</a>"))

    def test_contexte_tls_tolerant(self):
        ctx = c.legacy_ssl_context()
        self.assertIsInstance(ctx, ssl.SSLContext)
        self.assertEqual((ctx.check_hostname, ctx.verify_mode), (False, ssl.CERT_NONE))


class CrawlTests(unittest.TestCase):
    def test_lien_dans_onclick_trouve_et_arret(self):
        seen = []

        def h(req):
            seen.append(req.url.path)
            if req.url.path == "/":
                return page(f"<a href='/a'>a</a><button onclick=\"location='{T}/60001-x'\">Vos questions</button>")
            return httpx.Response(404)
        (res,) = crawl(h)
        self.assertEqual(res.row["statut_crawl"], "lien_trouve")
        self.assertEqual(res.links[0]["type_lien"], "lien_attribut")
        self.assertNotIn("/a", seen)                                      # arrêt dès la première page

    def test_url_en_texte_brut_signalee_mais_non_comptee(self):
        def h(req):
            if req.url.path == "/":
                return page("<a href='/a'>a</a><p>Le portail : vosquestions.ecologie-territoires.gouv.fr</p>")
            return page("<p>x</p>") if req.url.path == "/a" else httpx.Response(404)
        (res,) = crawl(h)
        self.assertEqual((res.row["statut_crawl"], res.links), ("explore_complet", []))
        self.assertIn("texte brut", res.row["motif"])

    def test_plan_b_accueil_vide_plan_du_site_puis_lien(self):
        def h(req):
            p = req.url.path
            if p == "/":
                return page("<script src=a></script><script src=b></script><script src=c></script><div id=root></div>")
            if p == "/sitemap.xml":
                return httpx.Response(200, headers={"content-type": "application/xml"},
                                      text="<urlset><url><loc>https://m.fr/vie-locale</loc></url></urlset>")
            if p == "/vie-locale":
                return page(f'<a href="{T}/60001-m">Vos questions</a>')
            return httpx.Response(404)
        (res,) = crawl(h)
        self.assertEqual((res.row["statut_crawl"], res.links[0]["page_url"]), ("lien_trouve", "https://m.fr/vie-locale"))
        self.assertIn("JavaScript", res.row["accueil_diag"])

    def test_plan_b_sans_resultat_site_signale_partiel_sans_fausse_erreur(self):
        def h(req):
            return page("<p>Bienvenue</p>") if req.url.path == "/" else httpx.Response(404)
        (res,) = crawl(h)
        r = res.row
        self.assertEqual(r["statut_crawl"], "explore_partiel")            # plus « lu en entier » pour un accueil vide
        self.assertIn("accueil sans lien interne", r["motif"])
        self.assertEqual((r["pages_visitees"], r["pages_en_erreur"], r["pages_interdites_robots"]), (1, 0, 0))
        self.assertIn("aucun lien", r["accueil_diag"])

    def test_plan_b_aussi_quand_le_seul_lien_est_l_accueil_lui_meme(self):
        asked = []

        def h(req):
            asked.append(req.url.path)
            return page("<a href='/fr'>Accueil</a><a href='/fr/index.html'>Accueil</a>") if req.url.path == "/fr/" else httpx.Response(404)
        (res,) = crawl(h, sites=[site("https://m.fr/fr/")])
        self.assertIn("/mentions-legales", asked)
        self.assertIn("accueil sans lien interne", res.row["motif"])

    def test_plan_b_respecte_robots_txt(self):
        asked = []

        def h(req):
            asked.append(req.url.path)
            if req.url.path == "/robots.txt":
                return httpx.Response(200, headers={"content-type": "text/plain"}, text="User-agent: *\nDisallow: /contact\n")
            return page("<p>Bienvenue</p>") if req.url.path == "/" else httpx.Response(404)
        (res,) = crawl(h)
        self.assertNotIn("/contact", asked)
        self.assertEqual(res.row["pages_interdites_robots"], 0)           # une sonde interdite n'est pas comptée

    def test_pas_de_plan_b_si_l_accueil_a_des_liens(self):
        asked = []

        def h(req):
            asked.append(req.url.path)
            return page("<a href='/a'>a</a>") if req.url.path == "/" else (page("<p>x</p>") if req.url.path == "/a" else httpx.Response(404))
        (res,) = crawl(h)
        self.assertEqual(res.row["statut_crawl"], "explore_complet")
        self.assertEqual(res.row["accueil_diag"], "")
        self.assertNotIn("/mentions-legales", asked)

    def test_pagination_et_calendrier_plafonnes_mais_pas_le_contenu(self):
        asked = []
        agenda = "".join(f'<a href="/agenda?date=2026-{i:02d}">m{i}</a>' for i in range(1, 31))
        contenu = "".join(f'<a href="/actualite/n{i}">n{i}</a>' for i in range(1, 21))
        suivi = '<a href="/doc?utm_source=news&id=1">d</a><a href="/doc?id=1&fbclid=zz">d</a>'

        def h(req):
            asked.append(req.url.path + ("?" + req.url.query.decode() if req.url.query else ""))
            return page(agenda + contenu + suivi) if req.url.path == "/" else (
                httpx.Response(404) if req.url.path == "/robots.txt" else page("<p>x</p>"))
        (res,) = crawl(h, max_depth=1, max_pages=100)
        self.assertEqual(sum(a.startswith("/agenda") for a in asked), c.TRAP_CAP)
        self.assertEqual(res.row["variantes_ignorees"], 30 - c.TRAP_CAP)
        self.assertEqual(sum(a.startswith("/actualite/") for a in asked), 20)         # contenu : jamais plafonné
        self.assertEqual(asked.count("/doc?id=1"), 1)                                 # même page, suivi retiré

    def test_robots_en_redirection_sans_destination_ne_bloque_plus_le_site(self):
        def h(req):
            if req.url.path == "/robots.txt":
                return httpx.Response(301)
            return page(f'<a href="{T}/">Vos questions</a>')
        (res,) = crawl(h)
        self.assertEqual(res.row["statut_crawl"], "lien_trouve")

    def test_robots_en_boucle_de_redirections_ne_bloque_plus_le_site(self):
        def h(req):
            if req.url.path == "/robots.txt":
                return httpx.Response(302, headers={"location": "/robots.txt"})
            return page(f'<a href="{T}/">Vos questions</a>')
        (res,) = crawl(h)
        self.assertEqual(res.row["statut_crawl"], "lien_trouve")


class RunTests(unittest.TestCase):
    def test_reprise_refait_une_fois_les_petits_sites_lus_avant_le_plan_b(self):
        with tempfile.TemporaryDirectory() as tmp:
            asked = []

            def h(req):
                asked.append(req.url.path)
                if req.url.path == "/":
                    return page("<p>Bienvenue</p>")
                return httpx.Response(404)
            orig = httpx.AsyncClient
            httpx.AsyncClient = lambda **kw: orig(**{**kw, "transport": httpx.MockTransport(h)})
            try:
                p = DeptPaths(Path(tmp), "60")
                inventory(p, [("60002", "B", "https://b.fr/")])
                with mock.patch("builtins.print"):
                    c.run_many([p], args())
                    rows = helpers.dict_rows(p.crawl_sites)
                    # fichier d'une version antérieure : site « complet » d'une page, sans colonne accueil_diag
                    old = [{k: v for k, v in rows[0].items() if k not in ("accueil_diag", "variantes_ignorees")}]
                    old[0].update(statut_crawl="explore_complet", motif="toutes les pages atteignables dans les limites ont été lues")
                    c.write_csv(p.crawl_sites, old, [k for k in c.SITE_FIELDS if k not in ("accueil_diag", "variantes_ignorees")])
                    asked.clear()
                    c.run_many([p], args(resume=True))
                    self.assertIn("/", asked)                              # refait une fois
                    asked.clear()
                    c.run_many([p], args(resume=True))
                    self.assertEqual(asked, [])                            # puis considéré comme terminé
            finally:
                httpx.AsyncClient = orig

    def test_notes_de_fin_d_exploration(self):
        def h(req):
            p = req.url.path
            if req.url.host == "vide.fr":
                return page("<p>Bienvenue</p>") if p == "/" else httpx.Response(404)
            if p == "/":
                return page("".join(f'<a href="/p{i}">p</a>' for i in range(30)))
            return httpx.Response(404) if p == "/robots.txt" else page("<p>x</p>")
        with tempfile.TemporaryDirectory() as tmp:
            orig = httpx.AsyncClient
            httpx.AsyncClient = lambda **kw: orig(**{**kw, "transport": httpx.MockTransport(h)})
            out = io.StringIO()
            try:
                p = DeptPaths(Path(tmp), "60")
                inventory(p, [("60001", "Long", "https://long.fr/"), ("60002", "Vide", "https://vide.fr/")])
                with contextlib.redirect_stdout(out):
                    c.run_many([p], args(host_delay=0.05, site_timeout=0.3))
            finally:
                httpx.AsyncClient = orig
        text = out.getvalue()
        self.assertIn("coupés par la durée maximale", text)
        self.assertIn("accueil n'a aucun lien interne", text)


if __name__ == "__main__":
    unittest.main()
