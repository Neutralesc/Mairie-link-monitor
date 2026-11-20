"""Lecture publique (MLM_LECTURE_PUBLIQUE) servie par webapp : liste blanche des routes publiques, refus par défaut des
autres, menu adaptatif selon l'identifiant, en-têtes, échappement, CSV, pannes. Serveur réel sur 127.0.0.1, source
fichiers (FileSource). Aucun réseau. La variante Postgres du même contrat est dans test_consultation_postgres.py ;
l'activation par variable d'environnement dans test_consultation_api.py.
"""
import base64
import csv
import io
import re
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support_publications as sp  # noqa: E402
import csvio  # noqa: E402
import webapp  # noqa: E402
from layout import DeptPaths  # noqa: E402
from publications import FileSource, SourceIndisponible  # noqa: E402

MOT_DE_PASSE = "mot-de-passe-lecture-test-4b2f"
PUBLIQUES = ("/", "/a-propos", "/departement/60", "/departement/60.csv")
PROTEGEES = ("/analyse", "/suivi", "/api/etat", "/campagnes", "/campagnes/1", "/rapports", "/rapports/60",
             "/resultats", "/rapport/60", "/csv/60", "/csv/tous", "/lancer", "/reprendre/1", "/arreter",
             "/inconnu", "/a-propos/", "/departement/60/x", "/departement/60.json", "/departement/abcd")
MENU_ADMIN = ('href="/suivi"', 'href="/rapports"', 'href="/campagnes"')
MENTION = ("Résultats générés automatiquement à partir de pages publiques ; une absence de lien peut venir d'un site "
           "inaccessible ou d'un lien non détecté.")
FORMULES = ("=", "+", "-", "@", "\t", "\r")
IMG_HOSTILE = "<img src=x onerror=alert(1)>"
FORMULE_HOSTILE = '=HYPERLINK("https://evil.example","clic")'


class _SansRedirection(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


OPENER = urllib.request.build_opener(_SansRedirection)


def construire_racine_hostile(root: Path) -> None:
    """Racine de test avec des noms de communes hostiles (formule tableur, balise HTML) dans le département 60."""
    sp.construire_racine(root)
    csvio.write_rows(DeptPaths(root, "60").inventory_communes, [
        {"code_insee": "60001", "nom_commune": FORMULE_HOSTILE, "nb_urls_valides": "1"},
        {"code_insee": "60002", "nom_commune": IMG_HOSTILE, "nb_urls_valides": "1"},
        {"code_insee": "60003", "nom_commune": "@SUM(1+1)", "nb_urls_valides": "1"},
    ])


class PanneSource:
    """Source dont toutes les lectures échouent comme une base injoignable (SourceIndisponible)."""

    def __init__(self, racine: Path):
        self._source = FileSource(racine)

    def __getattr__(self, nom):
        def en_panne(*args, **kwargs):
            raise SourceIndisponible(nom)
        return en_panne


def basic(identifiant: str) -> str:
    return "Basic " + base64.b64encode(identifiant.encode()).decode()


class ServeurLecture(unittest.TestCase):
    """Serveur réel ; `lecture` active ou non la lecture publique. Les sous-classes adaptent la racine et la source."""

    lecture = True

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.preparer(self.root)
        self.runner = webapp.Runner(self.root)
        self.serveur = webapp.serve(self.root, "127.0.0.1", 0, MOT_DE_PASSE, self.runner, self.creer_source(),
                                    lecture_publique=self.lecture)
        self.addCleanup(self._arreter)
        threading.Thread(target=self.serveur.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.serveur.server_address[1]}"

    def _arreter(self):
        self.serveur.shutdown()
        self.serveur.server_close()

    def preparer(self, root: Path) -> None:
        sp.construire_racine(root)

    def creer_source(self):
        return FileSource(self.root)

    def appel(self, methode: str, chemin: str, auth: str | None = None, origine: bool = False,
              corps: bytes | None = None, brut_auth: str | None = None):
        """Retourne (statut, en-têtes, corps texte). `auth` = mot de passe (identifiant « x ») ; `brut_auth` = en-tête brut."""
        en_tetes = {}
        if auth is not None:
            en_tetes["Authorization"] = basic(f"x:{auth}")
        if brut_auth is not None:
            en_tetes["Authorization"] = brut_auth
        if origine:
            en_tetes["Origin"] = self.base  # origine valide : un formulaire accepté ailleurs le serait ici
        req = urllib.request.Request(self.base + chemin, data=corps, method=methode, headers=en_tetes)
        try:
            with OPENER.open(req, timeout=10) as r:
                return r.status, r.headers, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read().decode("utf-8", "replace")

    def html(self, chemin: str, auth: str | None = None) -> str:
        statut, _, corps = self.appel("GET", chemin, auth=auth)
        self.assertEqual(statut, 200, chemin)
        return corps


def attributs_href_action(html: str) -> list[str]:
    return re.findall(r'(?:href|action)="([^"]*)"', html)


class LectureDesactivee(ServeurLecture):
    """MLM_LECTURE_PUBLIQUE absente : comportement d'origine, tout derrière le mot de passe, racine comprise."""

    lecture = False

    def test_racine_protegee(self):
        self.assertEqual(self.appel("GET", "/")[0], 401)
        self.assertEqual(self.appel("GET", "/", auth=MOT_DE_PASSE)[0], 200)

    def test_pages_publiques_ne_s_ouvrent_pas(self):
        for chemin in ("/a-propos", "/departement/60", "/departement/60.csv"):
            with self.subTest(chemin=chemin):
                self.assertEqual(self.appel("GET", chemin)[0], 401)
                self.assertEqual(self.appel("GET", chemin, auth=MOT_DE_PASSE)[0], 404)

    def test_post_sur_racine_toujours_protege(self):
        self.assertEqual(self.appel("POST", "/", origine=True, corps=b"dept=60")[0], 401)
        self.assertIsNone(self.runner.proc)

    def test_head_et_verbes_inchanges(self):
        self.assertEqual(self.appel("HEAD", "/")[0], 501)
        self.assertEqual(self.appel("PUT", "/analyse", auth=MOT_DE_PASSE)[0], 501)


class ContratLecture:
    """Contrat de la lecture publique (mixin) : repris à l'identique par les variantes fichiers et Postgres.
    Utilise self.appel et self.html, fournis par ServeurLecture."""

    def test_pages_publiques_sans_authentification(self):
        for chemin in PUBLIQUES:
            with self.subTest(chemin=chemin):
                self.assertEqual(self.appel("GET", chemin)[0], 200)
        self.assertEqual(self.appel("HEAD", "/")[:1], (200,))
        self.assertEqual(self.appel("HEAD", "/")[2], "")

    def test_liste_blanche_et_refus_par_defaut(self):
        for chemin in PROTEGEES:
            with self.subTest(chemin=chemin):
                self.assertEqual(self.appel("GET", chemin)[0], 401)
                self.assertEqual(self.appel("GET", chemin, brut_auth="Basic n-importe-quoi")[0], 401)

    def test_protegees_accessibles_avec_identifiants_valides(self):
        self.assertEqual(self.appel("GET", "/analyse", auth=MOT_DE_PASSE)[0], 200)
        self.assertEqual(self.appel("GET", "/suivi", auth=MOT_DE_PASSE)[0], 200)

    def test_post_sur_publiques_405_meme_avec_identifiants_et_origine(self):
        for methode in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS", "FOOBAR"):
            for chemin in PUBLIQUES:
                with self.subTest(methode=methode, chemin=chemin):
                    statut, en_tetes, _ = self.appel(methode, chemin, auth=MOT_DE_PASSE, origine=True, corps=b"dept=60")
                    self.assertEqual(statut, 405)
                    self.assertEqual(en_tetes.get("Allow"), "GET, HEAD")
        self.assertIsNone(self.runner.proc)  # aucun POST ne lance d'analyse

    def test_post_sur_protegees_exige_le_mot_de_passe(self):
        for chemin in ("/analyse", "/lancer", "/arreter", "/reprendre/1", "/departement/60/x"):
            with self.subTest(chemin=chemin):
                self.assertEqual(self.appel("POST", chemin, origine=True, corps=b"dept=60")[0], 401)
        self.assertIsNone(self.runner.proc)

    def test_racine_visiteur_sans_menu_admin_ni_lancement(self):
        html = self.html("/")
        self.assertIn(MENTION, html)
        self.assertIn("Résultats", html)
        self.assertRegex(html, r'href="/analyse"[^>]*>Espace réservé</a>')
        for lien in MENU_ADMIN:
            self.assertNotIn(lien, html)
        self.assertNotIn("Lancer une analyse", html)
        self.assertNotIn("Analyse en cours", html)

    def test_liens_et_formulaires_visiteur(self):
        for chemin in ("/", "/a-propos", "/departement/60"):
            with self.subTest(chemin=chemin):
                html = self.html(chemin)
                self.assertNotIn('<form method="post"', html.lower())
                for valeur in attributs_href_action(html):
                    self.assertFalse(valeur.startswith("javascript:"), valeur)
                    if valeur.startswith("/"):
                        chemin_seul = valeur.split("#")[0].split("?")[0]  # ancre et filtres : même route publique
                        self.assertTrue(chemin_seul in ("/", "/a-propos", "/analyse", "/resultats.csv")
                                        or chemin_seul.startswith(("/departement/", "/static/")), valeur)
                self.assertEqual(attributs_href_action(html).count("/analyse"), 1)  # Espace réservé, une seule fois

    def test_identifiants_valides_menu_complet(self):
        html = self.html("/", auth=MOT_DE_PASSE)
        for lien in MENU_ADMIN + ('href="/analyse"',):
            self.assertIn(lien, html)
        self.assertIn("Lancer une analyse", html)  # tableau de bord d'administration
        self.assertIn('href="/suivi"', self.html("/a-propos", auth=MOT_DE_PASSE))
        self.assertIn('href="/suivi"', self.html("/departement/60", auth=MOT_DE_PASSE))

    def test_en_tete_authorization_invalide_reste_public(self):
        for brut in (basic("x:mauvais"), "Basic ###", "Bearer jeton", "Basic " + base64.b64encode(b"sansdeuxpoints").decode()):
            with self.subTest(brut=brut):
                statut, _, corps = self.appel("GET", "/", brut_auth=brut)
                self.assertEqual(statut, 200)
                self.assertNotIn('href="/suivi"', corps)
                self.assertEqual(self.appel("GET", "/a-propos", brut_auth=brut)[0], 200)
                self.assertEqual(self.appel("GET", "/departement/60.csv", brut_auth=brut)[0], 200)

    def test_en_tetes_noindex_et_cache_vary(self):
        for chemin in ("/", "/a-propos", "/departement/60", "/departement/99"):
            with self.subTest(chemin=chemin):
                statut, en_tetes, corps = self.appel("GET", chemin)
                self.assertEqual(en_tetes.get("X-Robots-Tag"), "noindex, nofollow")
                self.assertEqual(en_tetes.get("Cache-Control"), "no-store")
                self.assertIn("Authorization", en_tetes.get("Vary", ""))
                self.assertIsNone(en_tetes.get("Set-Cookie"))
                self.assertEqual(en_tetes.get("X-Content-Type-Options"), "nosniff")
                self.assertIn("frame-ancestors 'none'", en_tetes.get("Content-Security-Policy", ""))
                if statut == 200:
                    self.assertIn('<meta name="robots" content="noindex,nofollow">', corps)

    def test_mentions_obligatoires_sur_chaque_page_publique(self):
        for chemin in ("/", "/a-propos", "/departement/60"):
            with self.subTest(chemin=chemin):
                html = self.html(chemin)
                self.assertIn(MENTION, html)
                self.assertIn("Date de la dernière publication", html)

    def test_une_seule_titre_1_par_page(self):
        for chemin in ("/", "/a-propos", "/departement/60", "/departement/99"):
            with self.subTest(chemin=chemin):
                self.assertEqual(len(re.findall(r"<h1[\s>]", self.appel("GET", chemin)[2])), 1)

    def test_liste_et_recherche_des_departements(self):
        html = self.html("/")
        self.assertIn("Oise", html)
        self.assertIn("Somme", html)
        self.assertIn('aria-current="page"', html)
        self.assertIn("<caption>", html)
        self.assertIn('<th scope="col">Dernière publication</th>', html)
        self.assertIn('href="/departement/60.csv"', html)
        filtre = self.html("/?q=SOMME")  # sans accents ni casse
        self.assertIn("Somme", filtre)
        self.assertNotIn('href="/departement/60"', filtre)  # le tableau départements est filtré (la liste déroulante des liens ne l'est pas)
        self.assertIn("Aucun département", self.html("/?q=zzz-rien"))
        self.assertIn("Oise", self.html("/?q=60"))

    def test_departement_sans_details_techniques(self):
        html = self.html("/departement/60")
        self.assertIn("<h1>Oise (60)</h1>", html)
        self.assertIn("Télécharger la liste des communes (CSV)", html)
        self.assertIn('href="/departement/60.csv"', html)
        self.assertNotIn("Détails techniques", html)
        self.assertNotIn("Liens écartés", html)
        self.assertNotIn("Valeurs brutes", html)
        self.assertNotIn("Télécharger (HTML)", html)
        self.assertIn("<details", html)  # accordéons sans script

    def test_liens_externes_en_nouvelle_fenetre_nofollow(self):
        html = self.html("/departement/60")
        balises = re.findall(r'<a [^>]*target="_blank"[^>]*>', html)
        self.assertTrue(balises)
        for balise in balises:
            self.assertIn('rel="noopener noreferrer nofollow"', balise)
        self.assertIn("(nouvelle fenêtre)", html)

    def test_departement_inconnu_404_et_code_invalide_protege(self):
        self.assertEqual(self.appel("GET", "/departement/99")[0], 404)
        self.assertEqual(self.appel("GET", "/departement/2A")[0], 404)
        for chemin in ("/departement/abcd", "/departement/6", "/departement/60.json", "/departement/60/"):
            with self.subTest(chemin=chemin):
                self.assertEqual(self.appel("GET", chemin)[0], 401)  # hors liste blanche : protégé par défaut
                self.assertEqual(self.appel("GET", chemin, auth=MOT_DE_PASSE)[0], 404)

    def test_csv_departement(self):
        statut, en_tetes, corps = self.appel("GET", "/departement/60.csv")
        self.assertEqual(statut, 200)
        self.assertTrue(en_tetes.get("Content-Type", "").startswith("text/csv"))
        self.assertIn('filename="communes_60.csv"', en_tetes.get("Content-Disposition", ""))
        self.assertTrue(corps.startswith("﻿Code INSEE;Commune;Statut"))

    def test_csv_sans_formule_active(self):
        octets = self.appel("GET", "/departement/60.csv")[2].lstrip("﻿")
        for ligne in csv.reader(io.StringIO(octets), delimiter=";"):
            for cellule in ligne:
                self.assertFalse(cellule.startswith(FORMULES), cellule)

    def test_recherche_hostile_echappee(self):
        hostile = self.html('/?q="><script>alert(1)</script>')
        self.assertNotIn("<script>alert(1)", hostile)
        self.assertNotIn('"><script>', hostile)

    def test_head_sans_corps_avec_les_memes_en_tetes(self):
        statut, en_tetes, corps = self.appel("HEAD", "/departement/60")
        self.assertEqual((statut, corps), (200, ""))
        self.assertGreater(int(en_tetes.get("Content-Length", "0")), 0)
        self.assertEqual(en_tetes.get("X-Robots-Tag"), "noindex, nofollow")

    def test_secrets_absents_des_reponses(self):
        for chemin in ("/", "/departement/60", "/analyse"):
            statut, en_tetes, corps = self.appel("GET", chemin, brut_auth=basic("x:faux"))
            texte = corps + "\n".join(f"{k}: {v}" for k, v in en_tetes.items())
            self.assertNotIn(MOT_DE_PASSE, texte)


class LectureFichiers(ContratLecture, ServeurLecture):
    """Variante fichiers (FileSource), racine de test standard : 60 complet, 80 sans CSV."""

    lecture = True

    def test_departement_sans_csv_reste_consultable(self):
        self.assertIn("Somme", self.html("/departement/80"))
        self.assertEqual(self.appel("GET", "/departement/80.csv")[0], 200)


class LectureHostile(ServeurLecture):
    """Noms de communes hostiles : échappés dans les pages, aucune formule active dans le CSV."""

    lecture = True

    def preparer(self, root: Path) -> None:
        construire_racine_hostile(root)

    def test_saisie_hostile_echappee(self):
        html = self.html("/departement/60")
        self.assertNotIn(IMG_HOSTILE, html)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", html)
        self.assertNotIn("javascript:", html.lower())
        self.assertNotIn("<img src", html)

    def test_csv_neutralise(self):
        octets = self.appel("GET", "/departement/60.csv")[2]
        cellules = [c for ligne in csv.reader(io.StringIO(octets.lstrip("﻿")), delimiter=";") for c in ligne]
        self.assertIn("'" + FORMULE_HOSTILE, cellules)  # apostrophe ajoutée : la formule n'est pas exécutée
        self.assertIn("'@SUM(1+1)", cellules)
        for cellule in cellules:
            self.assertFalse(cellule.startswith(FORMULES), cellule)

    def test_recherche_hostile_echappee(self):
        html = self.html("/?q=" + "%3Cimg%20src%3Dx%3E")
        self.assertNotIn("<img src=x>", html)
        self.assertIn("&lt;img src=x&gt;", html)


class LecturePanne(ServeurLecture):
    """Base injoignable : pages de données en 503 sobre ; À propos reste lisible ; la racine admin garde son alerte."""

    lecture = True

    def creer_source(self):
        return PanneSource(self.root)

    def test_pages_de_donnees_503_sans_detail_technique(self):
        for chemin in ("/", "/departement/60", "/departement/60.csv"):
            with self.subTest(chemin=chemin):
                statut, en_tetes, corps = self.appel("GET", chemin)
                self.assertEqual(statut, 503)
                self.assertIn("Service indisponible", corps)
                self.assertIn("ne sont pas disponibles", corps)
                self.assertNotIn("Traceback", corps)
                self.assertEqual(en_tetes.get("X-Robots-Tag"), "noindex, nofollow")

    def test_a_propos_lisible_et_date_indisponible(self):
        statut, _, corps = self.appel("GET", "/a-propos")
        self.assertEqual(statut, 200)
        self.assertIn("Date de la dernière publication indisponible", corps)
        self.assertIn("Limites", corps)

    def test_racine_admin_garde_son_alerte(self):
        statut, _, corps = self.appel("GET", "/", auth=MOT_DE_PASSE)
        self.assertEqual(statut, 200)
        self.assertIn("Base de données indisponible", corps)


if __name__ == "__main__":
    unittest.main()
