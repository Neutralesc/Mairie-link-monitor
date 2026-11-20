"""Onglet « Résultats » (webapp.resultats), navigation du menu, pagination, recherche et états vides.

Sans Postgres : une source en mémoire implémente le protocole publications.PublicationSource. Le serveur HTTP tourne
en local (127.0.0.1), sans réseau externe.
"""
import base64
import re
from html import unescape as html_unescape
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support_publications as sp  # noqa: E402  (chemins src/ et tests/)
import history  # noqa: E402
import report  # noqa: E402
import webapp  # noqa: E402
from layout import DeptPaths  # noqa: E402
from publications import Publication, SourceIndisponible, trier_liens  # noqa: E402

CSV_60 = "code_insee;url_page\r\n60001;https://a.fr/\r\n".encode("utf-8")
MOT_DE_PASSE = "mdp-resultats-9f2"
ETAT_VIDE_AUCUNE_DONNEE = "Aucun résultat publié pour le moment : lancez une analyse depuis le menu Analyses."
ETAT_VIDE_RECHERCHE = "Aucune commune ne correspond à votre recherche."
LIBELLE_TABLEAU = "Communes dont le site contient un lien vers Vos Questions"


def lien(dept: str, code: str, nom: str, url: str | None = None) -> dict:
    return {"dept": dept, "code_insee": code, "nom_commune": nom, "url_page": url or f"https://{code}.example/"}


LIENS_PETITS = [
    lien("60", "60001", "Châtillon", "https://chatillon.example/"),
    lien("60", "60002", "Beauvais", "https://beauvais.example/"),
    lien("80", "80001", "Amiens", "https://amiens.example/"),
]


def liens_nombreux(n: int) -> list[dict]:
    """n communes du 60, noms « Commune 001 » à « Commune n » (l'ordre de tri est l'ordre numérique)."""
    return [lien("60", f"60{i:03d}", f"Commune {i:03d}", f"https://c{i:03d}.example/") for i in range(1, n + 1)]


class MemSource:
    """Source en mémoire (protocole PublicationSource). `depts` par défaut : départements des liens."""

    def __init__(self, liens=(), depts=None, csv_global=None, indisponible=False, sans_csv=()):
        self._liens = trier_liens(list(liens))
        self._depts = sorted(depts if depts is not None else {r["dept"] for r in self._liens})
        self._csv_global = csv_global
        self._indisponible = indisponible
        self._sans_csv = set(sans_csv)

    def _verifier(self):
        if self._indisponible:
            raise SourceIndisponible("detail-technique-interne")

    def depts(self) -> list[str]:
        self._verifier()
        return list(self._depts)

    def tous_les_liens(self, q, dept, limit, offset):  # accueil protégé : liste vide (le tableau n'est pas testé ici)
        self._verifier()
        return [], 0

    def nouveaux_liens(self, depuis, limit):
        self._verifier()
        return [], 0

    def liens(self, dept: str | None = None) -> list[dict]:
        self._verifier()
        return [r for r in self._liens if dept is None or r["dept"] == dept]

    def get(self, dept: str, *, report: bool = True, csv: bool = True) -> Publication | None:
        self._verifier()
        if dept not in self._depts:
            return None
        present = dept not in self._sans_csv
        return Publication(dept=dept, controle_date="", communes=0, avec_lien=0,
                           report_html=f"<h1>Compte rendu {dept}</h1>" if report else "",
                           csv_bytes=CSV_60 if (csv and present) else b"", csv_present=present, resume_changements="")

    def csv_all(self) -> bytes | None:
        self._verifier()
        return self._csv_global

    def has_csv_all(self) -> bool:
        self._verifier()
        return self._csv_global is not None

    def synthese(self, dept: str) -> dict | None:
        self._verifier()
        return None                                  # pas de synthèse en mémoire : les pages de rapport sont testées ailleurs

    def resumes(self) -> list[dict]:
        self._verifier()
        return []                                    # aucun département à résumer (accueil : chiffres à zéro)

    def analyses(self, limit: int = 50) -> list[dict]:
        self._verifier()
        return []

    def analyse(self, analysis_id: int) -> dict | None:
        self._verifier()
        return None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


class ServeurResultats(unittest.TestCase):
    """Serveur local avec une source en mémoire. Les sous-classes fixent `password` et `source()`."""
    password = ""

    def source(self) -> MemSource:
        return MemSource(LIENS_PETITS, csv_global=CSV_60)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.opener = urllib.request.build_opener(NoRedirect)
        self.demarrer(self.source())

    def demarrer(self, source) -> None:
        """Démarre un serveur sur `source` ; chaque serveur est arrêté à la fin du test."""
        serveur = webapp.serve(self.root, "127.0.0.1", 0, self.password, webapp.Runner(self.root), source)
        self.addCleanup(serveur.server_close)
        self.addCleanup(serveur.shutdown)
        threading.Thread(target=serveur.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.base = f"http://127.0.0.1:{serveur.server_address[1]}"

    def appel(self, chemin: str, auth: str | None = None):
        """(statut, en-têtes, corps texte). Les réponses 4xx/5xx ne lèvent pas d'exception."""
        req = urllib.request.Request(self.base + chemin)
        if auth is not None:
            req.add_header("Authorization", "Basic " + base64.b64encode(f"x:{auth}".encode()).decode())
        try:
            with self.opener.open(req, timeout=5) as r:
                return r.status, r.headers, r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read().decode("utf-8")

    def page_resultats(self, query: str = "") -> str:
        status, _, html = self.appel("/resultats" + ("?" + query if query else ""))
        self.assertEqual(status, 200, html[:200])
        return html


class ResultatsBasiqueTests(ServeurResultats):
    def test_titre_tableau_et_une_ligne_par_commune(self):
        html = self.page_resultats()
        self.assertIn("<h1>Résultats</h1>", html)
        self.assertIn(f"<caption>{LIBELLE_TABLEAU}</caption>", html)
        self.assertEqual(html.count("<tr><td>"), 3)                       # une ligne de données par commune
        self.assertIn("<strong>3 communes avec lien</strong>", html)
        self.assertIn("<td>Châtillon</td>", html)
        self.assertIn("<td>60001</td>", html)

    def test_filtre_departement(self):
        html = self.page_resultats("dept=60")
        self.assertEqual(html.count("<tr><td>"), 2)
        self.assertIn("<td>Beauvais</td>", html)
        self.assertNotIn("<td>Amiens</td>", html)
        self.assertIn('<option value="60" selected>60</option>', html)
        self.assertIn("<strong>2 communes avec lien</strong>", html)

    def test_recherche_insensible_aux_accents(self):
        html = self.page_resultats("q=chatillon")
        self.assertIn("<td>Châtillon</td>", html)
        self.assertNotIn("<td>Beauvais</td>", html)

    def test_recherche_insensible_a_la_casse_et_aux_accents_en_majuscules(self):
        self.assertIn("<td>Châtillon</td>", self.page_resultats("q=" + urllib.parse.quote("CHÂTILLON")))

    def test_recherche_par_code_insee(self):
        html = self.page_resultats("q=60001")
        self.assertIn("<td>Châtillon</td>", html)
        self.assertNotIn("<td>Beauvais</td>", html)

    def test_recherche_vide_ou_espaces_affiche_tout(self):
        for query in ("q=", "q=%20%20"):
            with self.subTest(query=query):
                self.assertEqual(self.page_resultats(query).count("<tr><td>"), 3)

    def test_recherche_sans_resultat_message_exact(self):
        html = self.page_resultats("q=zzzz")
        self.assertIn(ETAT_VIDE_RECHERCHE, html)
        self.assertIn("<strong>0 commune avec lien</strong>", html)
        self.assertNotIn("<caption>", html)

    def test_lien_de_page_en_https_et_page_relevee(self):
        html = self.page_resultats()
        self.assertIn('href="https://chatillon.example/"', html)

    def test_accord_singulier_pluriel(self):
        self.assertIn("<strong>1 commune avec lien</strong>", self.page_resultats("dept=80"))
        self.assertIn("<strong>2 communes avec lien</strong>", self.page_resultats("dept=60"))
        self.assertIn("<strong>0 commune avec lien</strong>", self.page_resultats("q=zzzz"))

    def test_navigation_resultats_active_et_autres_entrees_inactives(self):
        html = self.page_resultats()
        self.assertIn('<a class="fr-nav__link" href="/resultats" target="_self" aria-current="page">Résultats</a>', html)
        self.assertIn('<a class="fr-nav__link" href="/analyse" target="_self">Analyse</a>', html)
        self.assertIn('<a class="fr-nav__link" href="/" target="_self">Accueil</a>', html)
        self.assertEqual(html.count('aria-current="page"'), 1)

    def test_accueil_affiche_accueil_active(self):
        status, _, html = self.appel("/")
        self.assertEqual(status, 200)
        self.assertIn('<a class="fr-nav__link" href="/" target="_self" aria-current="page">Accueil</a>', html)
        self.assertIn('<a class="fr-nav__link" href="/resultats" target="_self">Résultats</a>', html)
        self.assertIn("Résultats publiés", html)                 # chiffres de l'accueil


class PaginationTests(ServeurResultats):
    def source(self):
        return MemSource(liens_nombreux(250))

    def lignes(self, html: str) -> list[str]:
        return re.findall(r"<td>(Commune \d{3})</td>", html)

    def test_trois_pages_pour_250_lignes_premiere_page(self):
        html = self.page_resultats()
        self.assertEqual(len(self.lignes(html)), 100)
        self.assertEqual(self.lignes(html)[0], "Commune 001")
        self.assertIn('class="fr-pagination"', html)
        self.assertIn("Suivant", html)
        self.assertNotIn("Précédent", html)

    def test_page_2_contient_les_lignes_101_a_200(self):
        html = self.page_resultats("page=2")
        lignes = self.lignes(html)
        self.assertEqual(lignes, [f"Commune {i:03d}" for i in range(101, 201)])
        self.assertIn("<td>Commune 101</td>", html)
        self.assertIn("<td>Commune 200</td>", html)
        self.assertNotIn("<td>Commune 100</td>", html)
        self.assertNotIn("<td>Commune 201</td>", html)
        self.assertIn('aria-current="page"><span class="fr-sr-only">Page </span>2<', html)

    def test_page_hors_limite_bornee_a_la_derniere(self):
        html = self.page_resultats("page=99")
        self.assertEqual(self.lignes(html), [f"Commune {i:03d}" for i in range(201, 251)])
        self.assertIn('aria-current="page"><span class="fr-sr-only">Page </span>3<', html)
        self.assertNotIn("Suivant", html)

    def test_page_non_numerique_ou_nulle_donne_la_premiere(self):
        premiere = [f"Commune {i:03d}" for i in range(1, 101)]
        for query in ("page=abc", "page=0", "page=-3", "page="):
            with self.subTest(query=query):
                self.assertEqual(self.lignes(self.page_resultats(query)), premiere)

    def test_pagination_conserve_departement_et_recherche(self):
        html = self.page_resultats("dept=60&q=Commune&page=2")
        # Les « & » des href sont échappés en « &amp; » dans le HTML : on les rétablit avant l'analyse.
        hrefs = [urllib.parse.parse_qs(urllib.parse.urlsplit(html_unescape(h)).query)
                 for h in re.findall(r'href="(/resultats\?[^"]*)"', html)]
        self.assertTrue(hrefs)
        for params in hrefs:
            with self.subTest(params=params):
                self.assertEqual(params.get("dept"), ["60"])
                self.assertEqual(params.get("q"), ["Commune"])
                self.assertIn(params["page"][0], {"1", "2", "3"})   # précédent, courante et suivant

    def test_exactement_100_lignes_pas_de_pagination(self):
        self.demarrer(MemSource(liens_nombreux(100)))
        html = self.page_resultats()
        self.assertEqual(len(self.lignes(html)), 100)
        self.assertNotIn('class="fr-pagination"', html)


class EtatsVidesTests(ServeurResultats):
    def source(self):
        return MemSource([], depts=[])

    def test_aucune_donnee_publiee(self):
        html = self.page_resultats()
        self.assertIn(ETAT_VIDE_AUCUNE_DONNEE, html)
        self.assertIn("<strong>0 commune avec lien</strong>", html)
        self.assertNotIn("<caption>", html)
        self.assertNotIn('class="fr-pagination"', html)


class EtatsVidesFiltreTests(ServeurResultats):
    def source(self):
        return MemSource([lien("60", "60001", "Châtillon")], depts=["60", "80"])

    def test_filtre_sans_resultat_pour_la_recherche(self):
        html = self.page_resultats("dept=60&q=zzzz")
        self.assertIn(ETAT_VIDE_RECHERCHE, html)
        self.assertNotIn(ETAT_VIDE_AUCUNE_DONNEE, html)

    def test_departement_sans_commune_avec_lien_affiche_un_message_precis(self):
        # Un département filtré sans aucune commune avec lien n'affiche pas le message général « aucun résultat publié ».
        html = self.page_resultats("dept=80")
        self.assertIn("Aucune commune avec lien publiée pour le département 80.", html)
        self.assertNotIn(ETAT_VIDE_AUCUNE_DONNEE, html)


class SecuriteSaisiesTests(ServeurResultats):
    HOSTILE = '"><script>alert(1)</script>'
    HOSTILE_ECHAPPE = "&quot;&gt;&lt;script&gt;alert(1)&lt;/script&gt;"

    def test_recherche_hostile_echappee(self):
        html = self.page_resultats("q=" + urllib.parse.quote(self.HOSTILE))
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn(f'value="{self.HOSTILE_ECHAPPE}"', html)
        self.assertIn(ETAT_VIDE_RECHERCHE, html)

    def test_departement_hostile_ignore_et_non_reflete(self):
        html = self.page_resultats("dept=" + urllib.parse.quote(self.HOSTILE))
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertNotIn("alert(1)", html)
        self.assertEqual(html.count("<tr><td>"), 3)           # filtre ignoré : toutes les communes


class IndisponibleTests(ServeurResultats):
    def source(self):
        return MemSource(LIENS_PETITS, indisponible=True)

    def test_resultats_503_sans_detail_technique(self):
        status, _, html = self.appel("/resultats")
        self.assertEqual(status, 503)
        self.assertIn("Service indisponible", html)
        self.assertNotIn("detail-technique-interne", html)

    def test_accueil_reste_200_avec_alerte(self):
        status, _, html = self.appel("/")
        self.assertEqual(status, 200)
        self.assertIn("Base de données indisponible", html)


class AuthentificationResultatsTests(ServeurResultats):
    password = MOT_DE_PASSE

    def test_sans_authentification_401(self):
        status, en_tetes, html = self.appel("/resultats")
        self.assertEqual(status, 401)
        self.assertIn("Basic", en_tetes.get("WWW-Authenticate", ""))
        self.assertNotIn("<h1>Résultats</h1>", html)

    def test_mauvais_mot_de_passe_401(self):
        self.assertEqual(self.appel("/resultats", auth="faux")[0], 401)

    def test_avec_authentification_200(self):
        status, _, html = self.appel("/resultats", auth=MOT_DE_PASSE)
        self.assertEqual(status, 200)
        self.assertIn("<h1>Résultats</h1>", html)
        self.assertNotIn(MOT_DE_PASSE, html)


class RapportEtCsvInchangesTests(ServeurResultats):
    def source(self):
        return MemSource(LIENS_PETITS, depts=["60", "80"], csv_global=CSV_60, sans_csv=["80"])

    def test_rapport_et_csv_statuts(self):
        # Ancienne adresse /rapport/<dép> : 303 vers la page du département quand il est publié, 404 sinon.
        # Le CSV d'un département sans fichier reste absent (404) même si le département est publié.
        attendus = {"/rapport/60": 303, "/rapport/80": 303, "/rapport/99": 404,
                    "/csv/60": 200, "/csv/80": 404, "/csv/99": 404, "/csv/tous": 200}
        for chemin, statut in attendus.items():
            with self.subTest(chemin=chemin):
                self.assertEqual(self.appel(chemin)[0], statut)

    def test_rapport_redirige_vers_la_page_du_departement(self):
        opener = urllib.request.build_opener(NoRedirect)
        with self.assertRaises(urllib.error.HTTPError) as e:
            opener.open(self.base + "/rapport/60", timeout=5)
        self.assertEqual((e.exception.code, e.exception.headers.get("Location")), (303, "/rapports/60"))

    def test_csv_60_octets_conserves(self):
        status, en_tetes, corps = self.appel("/csv/60")
        self.assertEqual(status, 200)
        self.assertIn("attachment", en_tetes.get("Content-Disposition", ""))
        self.assertEqual(corps.encode("utf-8"), CSV_60)


class RapportEtCsvSansCsvGlobalTests(ServeurResultats):
    def source(self):
        return MemSource(LIENS_PETITS, csv_global=None)

    def test_csv_tous_404_sans_fichier_global(self):
        self.assertEqual(self.appel("/csv/tous")[0], 404)


class NavigationHorsWebTests(unittest.TestCase):
    """Fichiers écrits sur disque : pas de menu ni de lien d'accueil (static_prefix relatif)."""

    def test_page_hors_web_sans_menu(self):
        html = report.page("Compte rendu 60", "<h1>x</h1>", static_prefix="../static")
        self.assertNotIn("fr-nav", html)
        self.assertNotIn('id="modal-menu"', html)
        self.assertNotIn("aria-current", html)

    def test_compte_rendu_ecrit_sans_menu(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        sp.construire_racine(root)
        sortie = report.write(DeptPaths(root, "60"), history.db_path(root))
        texte = sortie.read_text(encoding="utf-8")
        self.assertTrue(texte.startswith("<!doctype html>"))
        self.assertIn("Compte rendu – département 60", texte)
        self.assertNotIn("fr-nav", texte)
        self.assertNotIn("aria-current", texte)

    def test_page_web_active_marque_une_seule_entree(self):
        html = report.page("Résultats", "<p>x</p>", active="resultats")
        self.assertEqual(html.count('aria-current="page"'), 1)
        self.assertIn('href="/resultats" target="_self" aria-current="page">Résultats</a>', html)

    def test_page_web_sans_active_aucune_entree_marquee(self):
        self.assertNotIn("aria-current", report.page("Accès", "<p>x</p>"))


class HelpersResultatsTests(unittest.TestCase):
    def test_sans_accents(self):
        self.assertEqual(webapp.sans_accents("Châtillon"), "chatillon")
        self.assertEqual(webapp.sans_accents("ÉTIENNE"), "etienne")
        self.assertEqual(webapp.sans_accents("Straße"), "strasse")

    def test_url_resultats_encode_et_garde_les_filtres(self):
        self.assertEqual(webapp.url_resultats("60", "a&b c", 2), "/resultats?page=2&dept=60&q=a%26b+c")
        self.assertEqual(webapp.url_resultats("", "", 1), "/resultats?page=1")

    def test_pagination_etats_precedent_suivant_et_trou(self):
        html = webapp.pagination("", "", 1, 10)
        self.assertIn('aria-current="page"><span class="fr-sr-only">Page </span>1<', html)
        self.assertIn("Suivant", html)
        self.assertNotIn("Précédent", html)
        self.assertEqual(html.count("…"), 1)

    def test_pagination_echappe_la_recherche(self):
        html = webapp.pagination("", '"><x', 1, 2)
        self.assertNotIn("<x", html)


if __name__ == "__main__":
    unittest.main()
