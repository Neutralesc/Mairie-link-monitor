"""PanneauPocket : pages de départ, rattachement aux communes, parcours des voisines, export et couverture.
Cas réel : Sery-Magneval (60618) publie le lien dans une annonce de sa page PanneauPocket."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import T  # noqa: E402
import coverage  # noqa: E402
import crawl_links as cl  # noqa: E402
import csvio  # noqa: E402
import helpers  # noqa: E402
import platforms as pf  # noqa: E402
import results  # noqa: E402
from layout import DeptPaths  # noqa: E402

PP = "https://app.panneaupocket.com/ville/"
SERY = PP + "325595414-sery-magneval-60800"
DUVY = PP + "2005248110-duvy-60800"
GENDARMERIE = PP + "43126981-gendarmerie-de-crepy-en-valois-60800"
ETAVIGNY = PP + "1296860570-etavigny-60620"
LEY = PP + "1090348316-ley-57810"


def html(*neighbors, link=""):
    body = "".join(f'<div class="panneau"><p>Annonce</p></div>' for _ in range(3))
    if link:
        body += f'<div class="panneau"><h2>Vos questions</h2><a href="{link}">{link}</a></div>'
    body += f'<a href="?panneau=577188381">suivant</a>'
    return "<html><body>" + body + "".join(f'<a href="{n}">voisine</a>' for n in neighbors) + "</body></html>"


SITE = {
    SERY: html(DUVY, GENDARMERIE, LEY, link=f"{T}/60618-sery-magneval"),
    DUVY: html(SERY, ETAVIGNY),
    GENDARMERIE: html(SERY),
    ETAVIGNY: html(DUVY),
    LEY: html(),
}


def handler(requests, robots="User-agent: *\nAllow: /\n"):
    def h(req):
        requests.append(str(req.url))
        if req.url.path == "/robots.txt":
            return httpx.Response(200, headers={"content-type": "text/plain"}, text=robots)
        url = f"https://{req.url.host}{req.url.path}"
        return httpx.Response(200, headers={"content-type": "text/html"}, text=SITE[url]) if url in SITE else httpx.Response(404)
    return h


class PureTests(unittest.TestCase):
    def test_page_canonique(self):
        self.assertEqual(cl.panneaupocket_page(SERY + "?panneau=577188381"), SERY)
        self.assertEqual(cl.panneaupocket_page(SERY + "/"), SERY)
        for ko in ("https://app.panneaupocket.com/", "https://app.panneaupocket.com/pdf/x.pdf", "https://exemple.fr/ville/1-a-60000"):
            self.assertEqual(cl.panneaupocket_page(ko), "")

    def test_rattachement_par_nom_et_code_postal(self):
        index = {"sery-magneval": [("60618", "Sery-Magneval", "60")], "duvy": [("60204", "Duvy", "60")],
                 "crepy-en-valois": [("60176", "Crépy-en-Valois", "60")], "nauviale": [("12168", "Nauviale", "12")],
                 "saint-martin": [("60100", "Saint-Martin", "60"), ("80100", "Saint-Martin", "80")]}
        self.assertEqual(pf.match_page(SERY, index)[0], "60618")
        self.assertIsNone(pf.match_page(GENDARMERIE, index))                         # gendarmerie : jamais une mairie
        self.assertEqual(pf.match_page(PP + "290951869-affichage-legal-nauviale-12330", index)[0], "12168")
        self.assertEqual(pf.match_page(PP + "1-saint-martin-80120", index)[0], "80100")  # homonymes : département du code postal
        self.assertIsNone(pf.match_page(PP + "1-cc-provence-verdon-83670", index))
        self.assertEqual(pf.depts_of_cp("20100"), {"2A", "2B"})
        self.assertEqual(pf.depts_of_cp("97110"), {"971"})


class WalkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = DeptPaths(Path(self.tmp.name), "60")
        self.p.data_dir.mkdir(parents=True)
        self.p.reports_dir.mkdir(parents=True)
        csvio.write_rows(self.p.inventory_communes, [
            {"code_insee": c, "nom_commune": n, "nb_urls_valides": u, "statut_commune": "x"}
            for c, n, u in (("60618", "Sery-Magneval", "0"), ("60204", "Duvy", "1"), ("60224", "Étavigny", "0"),
                            ("60176", "Crépy-en-Valois", "1"))])
        # fiche mairie DILA d'Étavigny : sa page PanneauPocket est dans « application_mobile »
        csvio.write_rows(self.p.dila_records, [{
            "nom": "Mairie - Étavigny", "code_insee_commune": "60224",
            "pivot": json.dumps([{"type_service_local": "mairie", "code_insee_commune": ["60224"]}]),
            "application_mobile": json.dumps([{"valeur": ETAVIGNY, "description": "Panneau Pocket", "custom_dico2": "Android"},
                                              {"valeur": "https://play.google.com/store/apps/details?id=x"}])}])
        self.requests = []
        self.orig = httpx.AsyncClient

    def tearDown(self):
        httpx.AsyncClient = self.orig
        self.tmp.cleanup()

    def run_pf(self, robots="User-agent: *\nAllow: /\n", extra=()):
        h = handler(self.requests, robots)
        httpx.AsyncClient = lambda **kw: self.orig(**{**kw, "transport": httpx.MockTransport(h)})
        with contextlib.redirect_stdout(io.StringIO()):
            return pf.run_many([self.p], Namespace(host_delay=0, timeout=5, contact="", pages_panneaupocket=list(extra)))

    def test_seery_magneval_trouve_depuis_la_dila_par_les_voisines(self):
        self.assertEqual(self.run_pf(), 0)
        pages = {r["url_page"]: r for r in helpers.dict_rows(self.p.platform_pages)}
        self.assertEqual(set(pages), {ETAVIGNY, DUVY, SERY, GENDARMERIE})            # Ley (57) : autre département, non lue
        self.assertEqual((pages[ETAVIGNY]["source"], pages[ETAVIGNY]["code_insee"]), ("dila", "60224"))
        self.assertEqual((pages[SERY]["code_insee"], pages[SERY]["source"]), ("60618", "voisine"))
        self.assertEqual(pages[GENDARMERIE]["code_insee"], "")                        # lue, jamais rattachée
        self.assertIn("non rattachée", pages[GENDARMERIE]["motif"])
        links = helpers.dict_rows(self.p.platform_links)
        self.assertEqual([(l["code_insee"], l["target_url"], l["cible_commune"]) for l in links],
                         [("60618", f"{T}/60618-sery-magneval", "oui")])
        self.assertEqual(links[0]["confiance_site"], "plateforme_panneaupocket")
        self.assertTrue(all("panneau=" not in u for u in self.requests))              # une requête par commune

    def test_export_et_couverture(self):
        self.run_pf()
        self.assertEqual(results.export_dept(self.p), 1)                              # sans links_found.csv : accepté
        self.assertEqual(helpers.list_rows(self.p.result_csv)[1], ["60618", SERY])
        cov = {r["code_insee"]: r for r in coverage.build(self.p)}
        self.assertEqual((cov["60618"]["etat"], cov["60618"]["analysee"]), ("lien trouvé (PanneauPocket)", "oui"))
        self.assertEqual(cov["60224"]["etat"], "page PanneauPocket lue, aucun lien")

    def test_le_site_de_la_mairie_reste_prioritaire_dans_l_export(self):
        self.run_pf()
        csvio.write_rows(self.p.links_found, [dict(code_insee="60618", site_url="https://sery.fr/", page_url="https://sery.fr/",
                                                   target_url=f"{T}/60618-sery-magneval", type_lien="lien", cible_commune="oui",
                                                   confiance_site="probable_officiel")], cl.LINK_FIELDS)
        results.export_dept(self.p)
        self.assertEqual(helpers.list_rows(self.p.result_csv)[1], ["60618", "https://sery.fr/"])
        results.export_dept(self.p, mode="toutes_les_pages")
        self.assertEqual(len(helpers.list_rows(self.p.result_csv)) - 1, 2)

    def test_pages_de_depart_depuis_les_sites_de_mairies_et_la_configuration(self):
        csvio.write_rows(self.p.dila_records, [{"nom": "x", "code_insee_commune": "60224", "pivot": "", "application_mobile": ""}])
        self.run_pf(extra=[SERY + "?panneau=577188381"])                             # comme dans config.toml
        self.assertIn(SERY, {r["url_page"] for r in helpers.dict_rows(self.p.platform_pages)})
        csvio.write_rows(self.p.platform_pages, [])
        csvio.write_rows(self.p.crawl_sites, [dict(code_insee="60204", site_url="https://duvy.fr/", statut_crawl="explore_complet",
                                                   pages_plateformes=DUVY)], cl.SITE_FIELDS)
        self.requests.clear()
        self.run_pf()
        self.assertIn(SERY, {r["url_page"] for r in helpers.dict_rows(self.p.platform_pages)})

    def test_robots_respecte(self):
        self.run_pf(robots="User-agent: *\nDisallow: /ville/\n")
        self.assertTrue(all(r["statut"] == "robots_interdit" for r in helpers.dict_rows(self.p.platform_pages)))
        self.assertFalse(any("/ville/" in u for u in self.requests))
        self.assertEqual(helpers.dict_rows(self.p.platform_links), [])


class CrawlCaptureTests(unittest.TestCase):
    def test_lien_panneaupocket_repere_sur_le_site_de_la_mairie(self):
        d = cl.parse_page(f'<a href="{SERY}?panneau=1">Nos annonces</a><a href="/contact">c</a>', "https://sery.fr/", "sery.fr")
        self.assertEqual(d.platform_pages, [SERY])
        self.assertEqual(d.internal, ["https://sery.fr/contact"])

    def test_redirections_cadres_et_javascript_suivis(self):
        page = ('<html><head><meta http-equiv="Refresh" content="0; URL=\'/accueil.html\'">'
                '<script>window.location.href = "/fr/index.php";</script></head>'
                '<frameset><frame src="menu.htm"><frame src="https://autre.fr/x"></frameset></html>')
        d = cl.parse_page(page, "https://m.fr/", "m.fr")
        self.assertEqual(sorted(d.internal), ["https://m.fr/accueil.html", "https://m.fr/fr/index.php", "https://m.fr/menu.htm"])
        d = cl.parse_page('<body><iframe src="/agenda"></iframe></body>', "https://m.fr/", "m.fr")
        self.assertEqual(d.internal, ["https://m.fr/agenda"])


if __name__ == "__main__":
    unittest.main()
