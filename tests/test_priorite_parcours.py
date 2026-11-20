"""Priorité de visite (link_priority) et chemins exclus (EXCLUDED_PATHS) du crawler.

Les cas d'intégration utilisent un transport simulé : chaque requête est enregistrée, aucun réseau n'est utilisé.
"""
import asyncio
import sys
import unittest
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402,F401  (chemins src/ et tests/)
import crawl_links as c  # noqa: E402

BASE = "https://exemple-mairie.example"      # site de la commune : distinct du portail (liens internes, pas cibles)


class PrioriteTests(unittest.TestCase):
    def test_lien_de_menu_passe_avant_un_lien_de_contenu(self):
        menu_profond = c.link_priority(f"{BASE}/plan", "Plan", "menu_ou_bandeau", 5)
        contenu_direct = c.link_priority(f"{BASE}/page", "Lire la suite", "contenu", 0)
        self.assertLess(menu_profond, contenu_direct)

    def test_mot_cle_passe_avant_un_lien_neutre(self):
        avec_mot_cle = c.link_priority(f"{BASE}/urbanisme-permis", "Permis", "contenu", 2)
        neutre = c.link_priority(f"{BASE}/page-19", "Page 19", "contenu", 2)
        self.assertLess(avec_mot_cle, neutre)

    def test_mot_cle_dans_le_texte_du_lien(self):
        self.assertLess(c.link_priority(f"{BASE}/x", "Vos questions", "contenu", 1),
                        c.link_priority(f"{BASE}/y", "Bulletin", "contenu", 1))

    def test_mots_cles_sans_accents(self):
        # « Démarches » et « Énergie » reconnus comme « demarche » et « energie »
        self.assertEqual(c.link_priority(f"{BASE}/Démarches", "", "contenu", 1),
                         c.link_priority(f"{BASE}/demarches", "", "contenu", 1))
        self.assertEqual(c.link_priority(f"{BASE}/x", "Énergie", "contenu", 1),
                         c.link_priority(f"{BASE}/x", "energie", "contenu", 1))

    def test_la_profondeur_departage_a_classe_egale(self):
        self.assertLess(c.link_priority(f"{BASE}/a", "Plan", "menu_ou_bandeau", 1),
                        c.link_priority(f"{BASE}/b", "Plan", "menu_ou_bandeau", 2))

    def test_classes_ordonnees_menu_mot_cle_neutre(self):
        menu = c.link_priority(f"{BASE}/m", "Plan", "menu_ou_bandeau", 0)
        cle = c.link_priority(f"{BASE}/c", "Mairie", "contenu", 0)
        neutre = c.link_priority(f"{BASE}/n", "Autre", "contenu", 0)
        self.assertEqual((menu, cle, neutre), (10, 20, 30))


class ChemisExcluesTests(unittest.TestCase):
    def test_liste_contient_les_familles_attendues(self):
        for motif in ("/recherche", "/wp-admin", "/login", "/agenda/", "/feed", "/imprimer", "?s=", "?xml="):
            with self.subTest(motif=motif):
                self.assertIn(motif, c.EXCLUDED_PATHS)


class ParcoursTests(unittest.TestCase):
    ACCUEIL = """<html><body>
      <nav><a href="/menu-page">Menu</a><a href="/recherche?q=x">Recherche</a></nav>
      <main>
        <a href="/services-urbanisme">Urbanisme</a>
        <a href="/login">Connexion</a>
        <a href="/wp-admin/options">Admin</a>
        <a href="/agenda/2026">Agenda</a>
        <a href="/feed/">Flux</a>
        <a href="/?s=permis">Recherche interne</a>
        <a href="/imprimer/page">Imprimer</a>
        <a href="/page-neutre">Page</a>
      </main></body></html>"""

    def explorer(self, max_pages: int) -> tuple[list[str], list]:
        requetes: list[str] = []

        def handler(req: httpx.Request) -> httpx.Response:
            requetes.append(req.url.path + ("?" + req.url.query.decode() if req.url.query else ""))
            if req.url.path == "/robots.txt":
                return httpx.Response(404)
            if req.url.path == "/":
                return httpx.Response(200, text=self.ACCUEIL, headers={"content-type": "text/html; charset=utf-8"})
            return httpx.Response(200, text="<p>contenu</p>", headers={"content-type": "text/html; charset=utf-8"})

        site = {"code_insee": "60001", "nom_commune": "A", "start_url": BASE + "/", "confiance_lot2": "probable_officiel",
                "ssl_invalide": False, "_dept": "60", "known_pages": []}
        opts = dict(max_pages=max_pages, max_depth=2, concurrency=2, host_delay=0, timeout=5, site_timeout=20, contact="",
                    use_sitemap=False, retry_failed=False, insecure_ssl=False, max_par_domaine=4)

        async def run():
            return await c.crawl_all([site], opts, transport=httpx.MockTransport(handler))
        res = asyncio.run(run())
        return requetes, res

    def test_lien_de_menu_lu_avant_le_lien_de_contenu(self):
        # Accueil + une seule page : c'est le lien de menu qui doit être choisi, pas le lien « Urbanisme ».
        requetes, _ = self.explorer(max_pages=2)
        pages = [r for r in requetes if r not in ("/robots.txt",)]
        self.assertEqual(pages[0], "/")
        self.assertEqual(pages[1], "/menu-page")

    def test_chemins_exclus_jamais_demandes(self):
        requetes, res = self.explorer(max_pages=20)
        for exclu in ("/recherche", "/login", "/wp-admin", "/agenda", "/feed", "/?s=", "/imprimer"):
            with self.subTest(exclu=exclu):
                self.assertFalse(any(r.startswith(exclu) for r in requetes), requetes)
        self.assertIn("/services-urbanisme", requetes)                   # un lien normal reste suivi
        self.assertGreaterEqual(res[0].row["variantes_ignorees"], 1)     # et les exclus sont comptés comme ignorés


if __name__ == "__main__":
    unittest.main()
